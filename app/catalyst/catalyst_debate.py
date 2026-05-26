"""
催化事件多模型辩论引擎 — 3视角并行辩论 + 共识聚合

设计原则:
- 复用 DebatePlanner 的并行执行模式 (ThreadPoolExecutor)
- 复用 call_llm() 统一路由 (feature-based, 主备自动降级)
- 3个不同家族模型确保独立视角
- 共识度 ≥ 0.7 → 强信号, 0.4-0.7 → 标准, < 0.4 → 弱/放弃

辩论角色:
  宏观策略师 (qwen3.7-max): 宏观传导链 + 市场情绪影响
  风控质疑官 (deepseek-v4-pro): 风险评估 + 最坏情景 + 反面论证
  行业情绪师 (kimi-k2.6): 行业竞争格局 + 历史类比 + 情绪反应

两种模式:
  full_debate: 2轮完整辩论 (盘前/盘后, 高severity)
  quick_debate: 1轮快速 (反向催化/盘中异动, 60s内)
"""

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from app.catalyst.models import CatalystEvent, CatalystVerdict

logger = logging.getLogger("ftseek_us.catalyst.debate")


# ============================================================================
#  辩论 Prompt 模板
# ============================================================================

_MACRO_PROMPT = """你是 FTSeek 美股量化系统的【宏观策略师】。

## 催化事件
- 类型: {event_type}
- 严重度: {severity}
- 标的: {tickers}
- 标题: {headline}
- 摘要: {summary}
- 来源: {source}

## 市场背景
- VIX: {vix}
- SPY趋势: {spy_trend}
- 市场阶段: {regime}

## 你的任务
分析此事件的宏观传导链:
1. 此事件如何通过宏观链条传导到美股？(监管→行业→个股)
2. 影响范围有多大？(仅个股 vs 板块 vs 全市场)
3. 影响时间窗口？(盘中/数日/长期)
4. 建议操作方向 + 强度 (0-100) + 信心度 (0.0-1.0)

返回纯JSON (不要markdown代码块):
{{"direction": "LONG/SHORT/AVOID", "strength": 65, "confidence": 0.72, "time_horizon": "intraday/swing/position", "reasoning": "50字内核心推理", "risk_factors": ["风险1", "风险2"], "suggested_sl_pct": -5.0, "suggested_tp_pct": 10.0}}"""

_RISK_PROMPT = """你是 FTSeek 美股量化系统的【风控质疑官】。你的职责是找漏洞和反面论证。

## 催化事件
- 类型: {event_type}
- 严重度: {severity}
- 标的: {tickers}
- 标题: {headline}
- 摘要: {summary}
- 来源: {source}

## 市场背景
- VIX: {vix}
- 市场阶段: {regime}

## 你的任务
作为风控官，你必须质疑每一个乐观判断:
1. 这个事件的最坏情景是什么？(最大跌幅/涨幅)
2. 有哪些理由可以认为这个事件影响被高估？
3. 历史上是否有类似事件最终"雷声大雨点小"？
4. 如果要反向操作 (与直觉相反), 理由是什么？
5. 建议操作 + 强度 (保守估计) + 信心度

返回纯JSON:
{{"direction": "LONG/SHORT/AVOID", "strength": 45, "confidence": 0.60, "time_horizon": "intraday/swing/position", "reasoning": "50字内风控视角", "risk_factors": ["风险1", "风险2"], "worst_case_pct": -15.0, "suggested_sl_pct": -3.0, "suggested_tp_pct": 6.0}}"""

_SENTIMENT_PROMPT = """你是 FTSeek 美股量化系统的【行业情绪分析师】。

## 催化事件
- 类型: {event_type}
- 严重度: {severity}
- 标的: {tickers}
- 标题: {headline}
- 摘要: {summary}
- 来源: {source}

## 市场背景
- VIX: {vix}
- 市场阶段: {regime}

## 你的任务
从行业情绪和历史类比角度分析:
1. 市场情绪会如何反应？(恐慌/乐观/中性)
2. 历史上最相似的3个事件是什么？事后股价怎么走？
3. 该标的所在板块会如何联动？
4. 建议操作方向 + 强度 + 信心度

返回纯JSON:
{{"direction": "LONG/SHORT/AVOID", "strength": 55, "confidence": 0.65, "time_horizon": "intraday/swing/position", "reasoning": "50字内情绪视角", "historical_analogs": ["事件1", "事件2"], "suggested_sl_pct": -4.0, "suggested_tp_pct": 8.0}}"""

# Round 2 交叉验证 prompt
_CROSS_VALIDATE_PROMPT = """你是 FTSeek 的 {role}。以下是三位分析师对同一事件的分析:

## 宏观策略师观点
{macro_view}

## 风控质疑官观点
{risk_view}

## 行业情绪师观点
{sentiment_view}

## 你的任务
审阅其他两位的观点后:
1. 你是否同意他们的关键判断？
2. 哪些观点最有说服力？
3. 你修正后的方向 + 强度 + 信心度

返回纯JSON:
{{"direction": "LONG/SHORT/AVOID", "strength": 60, "confidence": 0.70, "reasoning": "修正后的30字推理", "agreements": ["同意点1"], "disagreements": ["反对点1"]}}"""


class CatalystDebate:
    """催化事件辩论引擎"""
    
    def __init__(self, market_context: Optional[Dict] = None):
        """
        Args:
            market_context: 市场背景 {"vix": 16.5, "spy_trend": "UP", "regime": "NEUTRAL"}
        """
        self.market_context = market_context or {
            "vix": "N/A", "spy_trend": "N/A", "regime": "NEUTRAL"
        }
    
    def full_debate(self, event: CatalystEvent,
                    timeout_seconds: int = 180) -> CatalystVerdict:
        """完整辩论: 2轮 (Round 1 并行 + Round 2 交叉验证)
        
        用于: 盘前/盘后, severity >= HIGH
        """
        logger.info("[CatalystDebate] 开始完整辩论: %s %s", event.severity, event.headline[:40])
        start = time.time()
        
        # Round 1: 3模型并行
        r1_results = self._run_round1(event, timeout_seconds=timeout_seconds)
        elapsed_r1 = time.time() - start
        logger.info("[CatalystDebate] Round 1 完成 (%.0fs): %d/3 模型成功",
                     elapsed_r1, len(r1_results))
        
        if len(r1_results) < 2:
            logger.warning("[CatalystDebate] 不足2个模型返回, 降级为单模型评分")
            return self._fallback_verdict(event, r1_results)
        
        # 计算分歧度
        divergence = self._calculate_divergence(r1_results)
        logger.info("[CatalystDebate] 分歧度=%.2f", divergence)
        
        # Round 2: 分歧度 > 0.4 时触发交叉验证
        if divergence > 0.4:
            remaining_timeout = max(10, timeout_seconds - elapsed_r1)
            r2_results = self._run_round2(event, r1_results,
                                          timeout_seconds=remaining_timeout)
            if r2_results:
                r1_results = r2_results  # 使用修正后的结果
        
        # Arbiter: 聚合
        verdict = self._arbitrate(event, r1_results, divergence)
        total_elapsed = time.time() - start
        logger.info("[CatalystDebate] 辩论完成 (%.0fs): %s %s strength=%d consensus=%.2f",
                     total_elapsed, verdict.direction, verdict.symbol,
                     verdict.strength, verdict.consensus_level)
        
        return verdict
    
    def quick_debate(self, event: CatalystEvent,
                     timeout_seconds: int = 60) -> CatalystVerdict:
        """快速辩论: 仅 Round 1 (1轮并行, 无交叉验证)
        
        用于: 反向催化 (行情异动触发), 时间紧迫
        """
        logger.info("[CatalystDebate] 快速辩论: %s", event.headline[:40])
        
        r1_results = self._run_round1(event, timeout_seconds=timeout_seconds)
        
        if not r1_results:
            return self._fallback_verdict(event, [])
        
        divergence = self._calculate_divergence(r1_results)
        return self._arbitrate(event, r1_results, divergence)
    
    # ================================================================
    #  Round 1: 并行独立分析
    # ================================================================
    
    def _run_round1(self, event: CatalystEvent,
                    timeout_seconds: int = 180) -> List[Dict]:
        """3模型并行调用"""
        prompt_kwargs = {
            "event_type": event.event_type,
            "severity": event.severity,
            "tickers": ", ".join(event.tickers),
            "headline": event.headline,
            "summary": event.summary,
            "source": event.source,
            "vix": self.market_context.get("vix", "N/A"),
            "spy_trend": self.market_context.get("spy_trend", "N/A"),
            "regime": self.market_context.get("regime", "NEUTRAL"),
        }
        
        calls = [
            ("macro", "catalyst_macro", _MACRO_PROMPT.format(**prompt_kwargs)),
            ("risk", "catalyst_risk", _RISK_PROMPT.format(**prompt_kwargs)),
            ("sentiment", "catalyst_sentiment", _SENTIMENT_PROMPT.format(**prompt_kwargs)),
        ]
        
        results = {}
        
        def _call_model(role: str, feature: str, prompt: str):
            from app.llm.model_call import call_llm
            resp = call_llm(
                feature=feature,
                prompt=prompt,
                json_mode=True,
                timeout=min(timeout_seconds, 60),
            )
            if resp.ok and resp.data:
                return role, resp.data
            logger.warning("[CatalystDebate] %s 模型返回失败: %s", role, resp.error)
            return role, None
        
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="catalyst-debate") as pool:
            futures = {
                pool.submit(_call_model, role, feature, prompt): role
                for role, feature, prompt in calls
            }
            for future in as_completed(futures, timeout=timeout_seconds):
                try:
                    role, data = future.result()
                    if data:
                        results[role] = data
                except Exception as e:
                    role = futures[future]
                    logger.warning("[CatalystDebate] %s 超时/异常: %s", role, e)
        
        return list(results.values())
    
    # ================================================================
    #  Round 2: 交叉验证
    # ================================================================
    
    def _run_round2(self, event: CatalystEvent, r1_results: List[Dict],
                    timeout_seconds: int = 60) -> Optional[List[Dict]]:
        """交叉验证: 让最强的两个模型审阅对方观点"""
        if len(r1_results) < 2:
            return None
        
        # 序列化 Round 1 各视角
        views = []
        for r in r1_results:
            views.append(json.dumps(r, ensure_ascii=False)[:300])
        
        macro_view = views[0] if len(views) > 0 else "N/A"
        risk_view = views[1] if len(views) > 1 else "N/A"
        sentiment_view = views[2] if len(views) > 2 else "N/A"
        
        # 用 macro 模型做交叉验证 (最有综合分析能力)
        cross_prompt = _CROSS_VALIDATE_PROMPT.format(
            role="宏观策略师(交叉验证)",
            macro_view=macro_view,
            risk_view=risk_view,
            sentiment_view=sentiment_view,
        )
        
        try:
            from app.llm.model_call import call_llm
            resp = call_llm(
                feature="catalyst_macro",
                prompt=cross_prompt,
                json_mode=True,
                timeout=min(timeout_seconds, 45),
            )
            if resp.ok and resp.data:
                # 用交叉验证结果修正 Round 1
                updated = list(r1_results)
                updated.append(resp.data)  # 追加交叉验证结论
                logger.info("[CatalystDebate] Round 2 交叉验证完成")
                return updated
        except Exception as e:
            logger.warning("[CatalystDebate] Round 2 失败: %s", e)
        
        return None
    
    # ================================================================
    #  分歧度计算 (复用 DebatePlanner 逻辑)
    # ================================================================
    
    def _calculate_divergence(self, results: List[Dict]) -> float:
        """计算模型间分歧度 (0.0=完全一致, 1.0=完全分歧)"""
        if len(results) < 2:
            return 0.0
        
        directions = [r.get("direction", "NEUTRAL") for r in results]
        strengths = [r.get("strength", 50) for r in results]
        
        # 方向分歧: 不同方向的比例
        unique_dirs = set(directions)
        if len(unique_dirs) == 1:
            direction_divergence = 0.0
        elif "AVOID" in unique_dirs and len(unique_dirs) == 2:
            direction_divergence = 0.3  # AVOID vs LONG/SHORT 中等分歧
        else:
            direction_divergence = 0.7  # LONG vs SHORT 高分歧
        
        # 强度分歧: 标准差归一化
        if len(strengths) >= 2:
            mean_s = sum(strengths) / len(strengths)
            variance = sum((s - mean_s) ** 2 for s in strengths) / len(strengths)
            std_s = variance ** 0.5
            strength_divergence = min(std_s / 30.0, 1.0)  # 30为标准差上限
        else:
            strength_divergence = 0.0
        
        return 0.6 * direction_divergence + 0.4 * strength_divergence
    
    # ================================================================
    #  Arbiter: 聚合判定
    # ================================================================
    
    def _arbitrate(self, event: CatalystEvent, results: List[Dict],
                   divergence: float) -> CatalystVerdict:
        """聚合多模型结论 → CatalystVerdict"""
        if not results:
            return self._fallback_verdict(event, [])
        
        # 方向投票
        directions = [r.get("direction", "AVOID") for r in results]
        dir_counts = {}
        for d in directions:
            dir_counts[d] = dir_counts.get(d, 0) + 1
        
        # 多数方向
        final_direction = max(dir_counts, key=dir_counts.get)
        
        # 共识度 (0.0-1.0)
        consensus = dir_counts[final_direction] / len(directions)
        
        # 如果共识是 AVOID, 直接返回
        if final_direction == "AVOID":
            return CatalystVerdict(
                event_id=event.event_id,
                symbol=event.tickers[0] if event.tickers else "UNKNOWN",
                direction="AVOID",
                strength=0,
                confidence=0.0,
                consensus_level=consensus,
                reasoning="多模型建议回避",
                debated_at=time.time(),
            )
        
        # 强度: 取同方向模型的平均值, 然后按共识度调整
        same_dir_strengths = [
            r.get("strength", 50) for r in results
            if r.get("direction") == final_direction
        ]
        avg_strength = sum(same_dir_strengths) / len(same_dir_strengths) if same_dir_strengths else 50
        
        # 共识度调整
        if consensus >= 0.7:
            adjusted_strength = min(100, avg_strength * 1.2)
        elif consensus < 0.4:
            adjusted_strength = avg_strength * 0.5
        else:
            adjusted_strength = avg_strength
        
        # 信心度: 取平均
        confidences = [r.get("confidence", 0.5) for r in results]
        avg_confidence = sum(confidences) / len(confidences)
        
        # 止损/止盈: 取同方向模型的中位数
        sl_pcts = sorted([r.get("suggested_sl_pct", -0.05) for r in results])
        tp_pcts = sorted([r.get("suggested_tp_pct", 0.10) for r in results])
        median_sl = sl_pcts[len(sl_pcts) // 2]
        median_tp = tp_pcts[len(tp_pcts) // 2]
        
        # 时间窗口: 多数投票
        horizons = [r.get("time_horizon", "swing") for r in results]
        horizon_counts = {}
        for h in horizons:
            horizon_counts[h] = horizon_counts.get(h, 0) + 1
        final_horizon = max(horizon_counts, key=horizon_counts.get)
        
        # 风险因素: 合并去重
        all_risks = []
        for r in results:
            all_risks.extend(r.get("risk_factors", []))
        unique_risks = list(dict.fromkeys(all_risks))[:5]  # 最多5条
        
        # 推理摘要: 合并各模型 reasoning
        reasonings = [r.get("reasoning", "") for r in results if r.get("reasoning")]
        combined_reasoning = " | ".join(reasonings[:3])
        
        return CatalystVerdict(
            event_id=event.event_id,
            symbol=event.tickers[0] if event.tickers else "UNKNOWN",
            direction=final_direction,
            strength=int(adjusted_strength),
            confidence=round(avg_confidence, 2),
            consensus_level=round(consensus, 2),
            reasoning=combined_reasoning[:200],
            sl_pct=median_sl,
            tp_pct=median_tp,
            time_horizon=final_horizon,
            risk_warnings=unique_risks,
            model_votes=[
                {"role": "model_" + str(i), **r}
                for i, r in enumerate(results)
            ],
            debated_at=time.time(),
        )
    
    def _fallback_verdict(self, event: CatalystEvent,
                          results: List[Dict]) -> CatalystVerdict:
        """降级: 辩论失败时用事件默认方向"""
        # 从事件类型推断默认方向
        default_directions = {
            "REGULATORY": "SHORT", "DELISTING": "SHORT",
            "FDA_REJECTION": "SHORT", "EARNINGS_MISS": "SHORT",
            "DOWNGRADE": "SHORT", "BREAKING": "SHORT",
            "FDA_APPROVAL": "LONG", "EARNINGS_BEAT": "LONG",
            "UPGRADE": "LONG",
        }
        direction = default_directions.get(event.event_type, "AVOID")
        
        severity_strength = {"CRITICAL": 70, "HIGH": 55, "MEDIUM": 40}
        strength = severity_strength.get(event.severity, 30)
        
        return CatalystVerdict(
            event_id=event.event_id,
            symbol=event.tickers[0] if event.tickers else "UNKNOWN",
            direction=direction,
            strength=strength,
            confidence=0.4,  # 低信心 (无辩论验证)
            consensus_level=0.0,
            reasoning=f"辩论降级: {event.event_type} {event.severity} (无多模型验证)",
            debated_at=time.time(),
        )
