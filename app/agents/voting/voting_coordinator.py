from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import time
import hashlib
import json
import logging
from dataclasses import dataclass

from .voting_config import VotingConfig, ModelRole
from .model_agent import ModelAgent, ModelScore
from .voting_aggregator import VotingAggregator
from ..debate.debate_engine import DebateEngine
from ..debate.debate_models import DebateSummary

# P0 清理 2026-04-25: 移除已废弃的 agent 导入
# TraderAgent, RiskAgent, FinalDecision 等已废弃
# 使用简单的占位类保持向后兼容



@dataclass
class VetoResult:
    """简化的否决结果"""
    vetoed: bool = False
    reasons: list = None
    risk_score: float = 0.0



# P1 新增: 决策记录器 — 打通投票→回测→反哺链路
from app.storage.decision_recorder import get_decision_recorder

# 本模块仅用于盘前/盘后 LLM 多模型投票。盘中由 app.pipeline.signal_pipeline._generate_technical_signal() 纯技术面驱动。
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# P1 修复: LLM 投票结果缓存
# ------------------------------------------------------------------

@dataclass
class VotingCacheEntry:
    """投票缓存条目"""
    result: Dict[str, Any]
    market_hash: str  # 行情快照哈希
    price_snapshot: Dict[str, float]  # 各标的价格快照
    timestamp: float
    hit_count: int = 0


class VotingCache:
    """
    LLM 投票结果缓存

    缓存策略：
    - 按 symbol 缓存
    - 行情变动 < 0.3% 且技术指标无信号切换时复用
    - 最长 TTL 30 分钟，超时强制刷新
    """

    CACHE_TTL = 1800  # 30 分钟
    PRICE_CHANGE_THRESHOLD = 0.003  # 0.3%

    def __init__(self):
        self.entries: Dict[str, VotingCacheEntry] = {}

    def _compute_market_hash(self, market_data: Dict[str, Any]) -> str:
        """计算行情快照哈希"""
        key_fields = {
            "symbol": market_data.get("symbol", ""),
            "rsi": market_data.get("rsi"),
            "macd_signal": market_data.get("macd_signal"),
            "regime": market_data.get("regime"),
        }
        return hashlib.md5(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()[:8]

    def get(self, symbol: str, market_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        尝试从缓存获取投票结果

        Returns:
            缓存结果 or None（缓存未命中/过期/行情变动大）
        """
        entry = self.entries.get(symbol)
        if not entry:
            return None

        # TTL 检查
        age = time.time() - entry.timestamp
        if age > self.CACHE_TTL:
            logger.debug(f"[投票缓存] {symbol} TTL 过期 ({age:.0f}s)")
            del self.entries[symbol]
            return None

        # 行情变动检查
        current_price = market_data.get("price", 0)
        cached_price = entry.price_snapshot.get(symbol, 0)

        if current_price > 0 and cached_price > 0:
            change_pct = abs(current_price - cached_price) / cached_price
            if change_pct > self.PRICE_CHANGE_THRESHOLD:
                logger.debug(
                    f"[投票缓存] {symbol} 行情变动 {change_pct*100:.2f}% > 0.3%，刷新"
                )
                del self.entries[symbol]
                return None

        # 技术指标信号检查
        current_hash = self._compute_market_hash(market_data)
        if current_hash != entry.market_hash:
            logger.debug(f"[投票缓存] {symbol} 技术信号变化，刷新")
            del self.entries[symbol]
            return None

        # 缓存命中
        entry.hit_count += 1
        logger.debug(
            f"[投票缓存] {symbol} 命中 (已复用 {entry.hit_count} 次, 缓存年龄 {age:.0f}s)"
        )
        return entry.result

    def put(self, symbol: str, result: Dict[str, Any], market_data: Dict[str, Any]):
        """写入缓存"""
        price = market_data.get("price", 0)
        self.entries[symbol] = VotingCacheEntry(
            result=result,
            market_hash=self._compute_market_hash(market_data),
            price_snapshot={symbol: price} if price > 0 else {},
            timestamp=time.time(),
        )

    def clear(self, symbol: Optional[str] = None):
        """清除缓存"""
        if symbol:
            self.entries.pop(symbol, None)
        else:
            self.entries.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total_hits = sum(e.hit_count for e in self.entries.values())
        return {
            "size": len(self.entries),
            "total_hits": total_hits,
            "ttl": self.CACHE_TTL,
        }

class VotingCoordinator:
    """投票协调器 - 2模型架构 2026-04-17"""

    # 2模型差异化提示词 (2026-04-17)
    # 去掉了 GLM-5 (超时频繁)，Qwen 现在承担宏观+技术综合分析
    _ROLE_PROMPTS: Dict[str, str] = {

        "qwen": """【综合策略专家】你负责宏观事件传导链、二阶推理和技术分析综合判断。

【核心能力 - 美股特异性强化】:
- **宏观共振优先**: 必须结合 SPY/QQQ 大盘趋势判断。若个股技术看涨但大盘走跌，需大幅降低置信度 (Confidence) 和强度 (Strength)。
- **机构/技术主导**: 重点分析技术指标与量价配合 (RSI/MACD/Volume/MA趋势)。
- **Livermore 大盘状态**: 严格遵循 "顺势而为" 原则，VIX 过高时拒绝做多。

✅ 你关注的:
• 大盘基准 (SPY/QQQ) 趋势及关键均线 (MA50/MA200) 支撑/阻力
• 宏观政策变化 (利率/关税/财政刺激) 及对市场流动性的影响
• 板块轮动逻辑 (资金从 A 板块流向 B 板块)
• 技术指标共振 (趋势 + 动量 + 成交量)

❌ 你不看的:
• 纯情绪宣泄 (交给 MiniMax)
• 个股微观基本面 (短线不依赖)

【重要共振原则】:
- 若 SPY 趋势向下，即使个股技术形态良好，最大 Strength 不得超过 60 (看多)。
- 若 VIX > 25，降低所有做多信号的置信度 (Confidence * 0.8)。
- 返回纯 JSON，不要使用 ```json 代码块，不要有任何额外文本。
返回格式：{"strength": 65, "confidence": 0.72, "direction": "LONG", "reasoning": "降息预期+MACD金叉，但SPY承压，降低评分"}""",

        "minimax": """【情绪分析专家】你 ONLY 关注新闻情绪和市场情绪信号。

✅ 你关注的:
• 新闻情绪分 (正向/负向/中性)
• VIX 恐慌指数水平
• 大盘整体情绪 (标普 500/纳指涨跌)
• 市场热度 (成交量/关注度)

❌ 你不看的:
• 技术指标 (交给 GLM)
• 宏观传导链 (交给 Qwen)
• 个股基本面估值 (短线不依赖)

评分标准:
- 0-30: 强烈看空 (重大利空 + VIX 飙升)
- 31-45: 看空 (情绪偏空)
- 46-55: 中性 (情绪不明/消息面平静)
- 56-70: 看多 (情绪偏多)
- 71-100: 强烈看多 (重大利好 + 情绪高涨)

【重要】情绪不明时给中性分 (46-55)，高 VIX(>30) 时降低置信度。
【重要】返回纯 JSON，不要使用 ```json 代码块，不要有任何额外文本。
返回格式：{"strength": 65, "confidence": 0.72, "direction": "LONG", "reasoning": "新闻情绪正向，VIX 降至 18"}""",

    }

    
    def __init__(
        self,
        config: Optional[VotingConfig] = None,
        timeout_seconds: int = 90,
        enable_debate: bool = True,
        enable_cache: bool = True,
    ):
        self.config = config or VotingConfig.get_default_config()
        self.timeout = timeout_seconds
        self.aggregator = VotingAggregator(self.config)
        self.model_agents = self._create_model_agents()

        # P1 修复: LLM 投票结果缓存
        self.enable_cache = enable_cache
        self.vote_cache = VotingCache() if enable_cache else None
        
        # P0优化: 辩论引擎 - 快速模式 (1轮,非3轮)
        self.enable_debate = enable_debate
        if enable_debate:
            self.debate_engine = DebateEngine(
                max_rounds=1,  # 2模型架构: 1轮快速辩论
                bull_model="bailian/MiniMax-M2.5",  # MiniMax情绪分析
                bear_model="bailian/qwen-max",  # Qwen综合推理
                fast_mode=True,
            )
            pass  # Removed legacy agents
    
    def _create_model_agents(self) -> Dict[ModelRole, ModelAgent]:
        """创建模型 Agent（各角色使用专属差异化提示词）"""
        agents = {}
        
        for role, model_name in self.config.model_mapping.items():
            # 优先使用角色专属提示词，fallback 到通用提示词
            prompt = self._ROLE_PROMPTS.get(role.value, self._create_generic_prompt(role))
            agents[role] = ModelAgent(role.value, model_name, prompt)
        
        return agents
    
    def _create_generic_prompt(self, role: ModelRole) -> str:
        """通用提示词（兜底）"""
        description = self.config.model_descriptions.get(role, "综合分析")
        return f"""你是一个专业的股票分析师，专注于{description}。
评分 0-100：0-30 强烈看空 | 31-45 看空 | 46-55 中性 | 56-70 看多 | 71-100 强烈看多
【重要】返回纯 JSON，不要使用 ```json 代码块，不要有任何额外文本。
返回格式：{{"strength": <整数>, "confidence": <0.0-1.0>, "direction": "<LONG 或 SHORT>", "reasoning": "<30 字内>"}}"""
    
    def run_voting(
        self,
        market_data: Dict[str, Any],
        external_votes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """盘前/盘后 LLM 多模型投票。盘中请使用 app.pipeline.signal_pipeline._generate_technical_signal()"""
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"

        # P1 缓存检查
        if self.enable_cache and self.vote_cache:
            cached = self.vote_cache.get(symbol, market_data)
            if cached:
                logger.info(f"[投票] {symbol} 使用缓存结果 (节省 3 次 LLM API 调用)")
                # 更新时间戳但复用其他字段
                cached["timestamp"] = time.time()
                cached["from_cache"] = True
                return cached

        scores = self._collect_scores_parallel(market_data)

        # P0 优化 (2026-04-17): 注入外部投票 (LLM 策略引擎)
        # P1 修复 (2026-04-17): ModelScore 使用 model_id= 而非 model=, direction 转字符串
        if external_votes:
            for ev in external_votes:
                model_name = ev.get("model", "external")
                direction_raw = ev.get("direction", 0)
                # direction: int 1 → "LONG", int -1 → "SHORT", int 0 → "NEUTRAL"
                if isinstance(direction_raw, int):
                    direction_str = "LONG" if direction_raw > 0 else ("SHORT" if direction_raw < 0 else "NEUTRAL")
                else:
                    direction_str = str(direction_raw).upper()

                score = ModelScore(
                    model_id=model_name,
                    strength=ev.get("strength", 50),
                    confidence=ev.get("confidence", 0.5),
                    direction=direction_str,
                    reasoning=ev.get("reasoning", ""),
                )
                scores[model_name] = score
                logger.info(
                    f"[投票] 注入外部投票 {model_name}: "
                    f"strength={score.strength}, confidence={score.confidence:.2f}, "
                    f"direction={score.direction}"
                )

        result = self.aggregator.aggregate(scores, market_data)

        # 添加元数据
        result["symbol"] = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"
        result["timestamp"] = time.time()

        # 方案 E 优化：添加动态仓位建议
        result["position_suggestion"] = self.calculate_position_suggestion(result)

        # 方案 E 优化：添加市场状态评估
        result["market_regime"] = self.assess_market_regime(market_data)

        # P1 新增: 决策记录 — 打通断点1 (投票→数据库)
        self._record_decision_to_db(result, market_data, external_votes)

        # P1 修复: 写入缓存
        if self.enable_cache and self.vote_cache:
            self.vote_cache.put(symbol, result, market_data)

        return result

    def _record_decision_to_db(
        self,
        result: Dict[str, Any],
        market_data: Dict[str, Any],
        external_votes: Optional[List[Dict[str, Any]]] = None,
    ):
        """将投票决策写入数据库 (断点1)"""
        try:
            recorder = get_decision_recorder()
            symbol = result.get("symbol") or market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"

            # 准备 LLM 投票信息
            llm_vote = None
            if external_votes:
                for ev in external_votes:
                    if ev.get("model") == "llm_strategy":
                        llm_vote = {
                            "strength": ev.get("strength", 50),
                            "weight": self.config.LLM_EXTERNAL_VOTE_WEIGHT,
                        }
                        break

            # 转换 model_scores 为 list of dict
            model_scores = result.get("model_scores", [])
            # Ensure direction is string (handle int from LLM)
            for ms in model_scores:
                d = ms.get("direction", "NEUTRAL")
                if isinstance(d, int):
                    ms["direction"] = "LONG" if d > 0 else "SHORT" if d < 0 else "NEUTRAL"

            decision_id = recorder.record_decision(
                symbol=symbol,
                voting_result={**result, "model_scores": model_scores},
                market_data={
                    "regime": (
                        result.get("market_regime")
                        if isinstance(result.get("market_regime"), str)
                        else (result.get("market_regime") or {}).get("regime")
                    ),
                    "vix": market_data.get("vix"),
                },
                llm_vote=llm_vote,
            )
            # 将 decision_id 写回 result 供下游使用
            result["decision_id"] = decision_id
            logger.info(f"[DecisionRecorder] 决策已记录: {decision_id}")
        except Exception as e:
            logger.warning(f"[DecisionRecorder] 记录决策失败 (不影响主流程): {e}")
    
    def calculate_position_suggestion(self, voting_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据投票结果动态计算仓位建议 - 方案 E 新增
        
        Returns:
            {
                "suggested_position": float,  # 建议仓位 (0.0-1.0)
                "confidence_level": str,      # 信心等级
                "reasoning": str,             # 仓位建议理由
            }
        """
        confidence = voting_result.get("final_confidence", 0.5)
        strength = voting_result.get("final_strength", 50)
        consensus = voting_result.get("consensus_level", 0.5)
        
        # 基础仓位
        base_position = self.config.POSITION_BASE
        
        # 根据信心等级调整仓位
        if confidence >= 0.70:
            position = self.config.POSITION_HIGH_CONFIDENCE  # 15%
            confidence_level = "HIGH"
        elif confidence >= 0.60:
            position = self.config.POSITION_MEDIUM_CONFIDENCE  # 12%
            confidence_level = "MEDIUM"
        elif confidence >= self.config.CONFIDENCE_THRESHOLD:  # 0.55
            position = self.config.POSITION_LOW_CONFIDENCE  # 8%
            confidence_level = "LOW"
        else:
            position = self.config.POSITION_MINIMUM  # 5%
            confidence_level = "MINIMAL"
        
        # 根据一致性调整
        if consensus < 0.55:
            position *= 0.8  # 一致性低，降低仓位
        elif consensus > 0.80:
            position *= 1.1  # 一致性高，小幅提升仓位
        
        # 根据信号强度调整 (极端信号降低仓位，可能是反转信号)
        if strength < 25 or strength > 75:
            position *= 0.9  # 极端信号，谨慎
        
        # 限制仓位范围
        position = max(0.0, min(0.20, position))  # 最大 20%
        
        return {
            "suggested_position": round(position, 3),
            "confidence_level": confidence_level,
            "reasoning": f"信心{confidence:.2f}, 一致性{consensus:.2f}, 强度{strength}",
        }
    
    def assess_market_regime(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估市场状态 - 方案 E 新增
        
        Returns:
            {
                "regime": str,           # 市场状态
                "vix_level": str,        # VIX 水平
                "suggested_strategy": str,  # 建议策略
            }
        """
        vix = market_data.get("vix", 20.0)
        spy_ma200_ratio = market_data.get("spy_ma200_ratio", 1.0)
        
        # VIX 水平评估
        if vix < self.config.VIX_LOW_THRESHOLD:  # <20
            vix_level = "LOW"
            regime = "LOW_VOLATILITY"
            suggested_strategy = "正常策略，可适度提升仓位"
        elif vix < self.config.VIX_MID_THRESHOLD:  # 20-25
            vix_level = "NORMAL"
            regime = "NORMAL"
            suggested_strategy = "标准策略"
        elif vix < self.config.VIX_HIGH_THRESHOLD:  # 25-30
            vix_level = "HIGH"
            regime = "HIGH_VOLATILITY"
            suggested_strategy = "降低仓位，提高风控标准"
        else:  # >30
            vix_level = "EXTREME"
            regime = "EXTREME_VOLATILITY"
            suggested_strategy = "最小仓位，仅高确定性机会"
        
        # 大盘趋势评估
        if spy_ma200_ratio > 1.05:
            trend = "BULL"
        elif spy_ma200_ratio < 0.95:
            trend = "BEAR"
        else:
            trend = "SIDEWAYS"
        
        return {
            "regime": regime,
            "vix_level": vix_level,
            "vix": vix,
            "trend": trend,
            "spy_ma200_ratio": spy_ma200_ratio,
            "suggested_strategy": suggested_strategy,
        }
    
    def _collect_scores_parallel(
        self,
        market_data: Dict[str, Any],
    ) -> List[ModelScore]:
        """并行收集所有模型评分

        P0 修复 (2026-04-17):
        - 增加 LLM 调用失败降级策略
        - 当超过半数模型失败时，触发降级模式
        - 降级模式：使用纯技术指标评分 + 发送钉钉告警
        """
        scores = []
        failed_models = []
        total_models = len(self.model_agents)

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(agent.generate_score, market_data): role
                for role, agent in self.model_agents.items()
            }

            for future in futures:
                role = futures[future]
                try:
                    score = future.result(timeout=self.timeout)
                    scores.append(score)
                except FuturesTimeoutError:
                    failed_models.append(role.value)
                    logger.error(f"[投票] {role.value} 模型评分超时")
                except Exception as e:
                    failed_models.append(role.value)
                    logger.error(f"[投票] {role.value} 模型评分失败：{e}")

        # P0 降级：超过半数模型失败 → 切换纯技术模式
        if len(failed_models) > total_models / 2:
            logger.critical(
                f"[投票·降级] {len(failed_models)}/{total_models} 模型失败 "
                f"({', '.join(failed_models)})，切换纯技术模式"
            )
            # 注入降级标记
            technical_score = self._generate_technical_fallback(market_data)
            scores.append(technical_score)
            # 发送告警
            self._send_llm_failure_alert(failed_models, market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN")

        return scores

    def _generate_technical_fallback(
        self, market_data: Dict[str, Any]
    ) -> ModelScore:
        """纯技术指标多维评分（盘中 TRADING phase 专用）

        修复 2026-04-25: 原硬编码 confidence=0.3 与门禁 0.5 矛盾，
        导致所有盘中信号 100% 被拦截。改为多维评分 + 动态置信度。
        """
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"
        rsi = market_data.get("rsi", 50)
        macd_signal = market_data.get("macd_signal", 0)
        price = market_data.get("price", 0)
        ma20 = market_data.get("ma20", 0)
        ma50 = market_data.get("ma50", 0)
        volume_ratio = market_data.get("volume_ratio", 1.0)

        # ── 多维技术评分 ──
        bull_signals = 0
        bear_signals = 0
        total_checks = 0

        # 1. RSI 评分
        if rsi < 30:
            bull_signals += 2; total_checks += 2
        elif rsi < 40:
            bull_signals += 1; total_checks += 1
        elif rsi > 70:
            bear_signals += 2; total_checks += 2
        elif rsi > 60:
            bear_signals += 1; total_checks += 1
        else:
            total_checks += 1

        # 2. MACD 信号线
        if macd_signal > 0:
            bull_signals += 1; total_checks += 1
        elif macd_signal < 0:
            bear_signals += 1; total_checks += 1
        else:
            total_checks += 1

        # 3. 均线趋势
        if ma20 > 0 and ma50 > 0:
            if price > ma20 > ma50:
                bull_signals += 1; total_checks += 1
            elif price < ma20 < ma50:
                bear_signals += 1; total_checks += 1
            else:
                total_checks += 1
        else:
            total_checks += 1

        # 4. 量比（放量增强信号可信度）
        if volume_ratio > 1.5:
            total_checks += 1  # 仅加分项，不改变方向

        # 计算强度
        if total_checks > 0:
            bull_ratio = bull_signals / total_checks
            bear_ratio = bear_signals / total_checks
        else:
            bull_ratio = bear_ratio = 0

        if bull_ratio > bear_ratio:
            strength = 50 + int(bull_ratio * 40)  # 51-90
            direction = "LONG"
        elif bear_ratio > bull_ratio:
            strength = 50 - int(bear_ratio * 40)  # 10-49
            direction = "SHORT"
        else:
            strength = 50
            direction = "NEUTRAL"

        # ── 动态置信度 (0.35 ~ 0.85) ──
        signal_agreement = abs(bull_ratio - bear_ratio)  # 0=无共识, 1=完全一致
        confidence = 0.35 + signal_agreement * 0.45  # 基础范围 0.35-0.80

        # 量比增强
        if volume_ratio > 2.0:
            confidence = min(confidence * 1.15, 0.85)

        return ModelScore(
            model_id="technical_fallback",
            strength=max(10, min(90, strength)),
            confidence=round(confidence, 2),
            direction=direction,
            reasoning=f"技术面评分: bull={bull_ratio:.2f} bear={bear_ratio:.2f} "
                      f"RSI={rsi:.0f} MACD={macd_signal:.2f} VR={volume_ratio:.1f}",
        )

    def _send_llm_failure_alert(self, failed_models: List[str], symbol: str):
        """发送 LLM 失败告警"""
        try:
            from app.notifier.dingtalk import send_dingtalk_message
            msg = (
                f"🚨 FTSeek-US LLM 降级告警\n\n"
                f"📌 标的: {symbol}\n"
                f"❌ 失败模型: {', '.join(failed_models)}\n"
                f"🔧 已切换: 纯技术指标降级模式\n"
                f"⚠️ 置信度降至 0.3，建议人工审查"
            )
            send_dingtalk_message(msg)
        except Exception as e:
            logger.error(f"[投票] 发送 LLM 失败告警异常: {e}")
