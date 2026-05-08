"""
PremarkDebateAgent — 盘前多模型辩论 Agent

基于 FTSeek-US MultiModelDebateRunner 已验证架构移植到港股。
3 模型并发调用 DashScope API:
  - qwen3.6-plus: 多头分析师
  - deepseek-v4-pro: 空头审计师
  - MiniMax-M2.5: 独立风险评估师

使用方式:
    from app.harness.premarket_debate import PremarkDebateAgent
    agent = PremarkDebateAgent()
    result = await agent.run(candidate_pool, market_context)
"""

import os
import json
import time
import logging
import threading
import requests
from typing import Any, Dict, List, Optional
from datetime import datetime

from .base import Agent, AgentConfig

logger = logging.getLogger("harness.premarket_debate")

__all__ = ["PremarkDebateAgent"]


# ============================================================
#  模型角色配置
# ============================================================

MODEL_ROLES = [
    {"model": "qwen3.6-plus",     "role": "bull",     "label": "多头分析师"},
    {"model": "deepseek-v4-pro",  "role": "bear",     "label": "空头审计师"},
    {"model": "MiniMax-M2.5",     "role": "auditor",  "label": "独立风险评估师"},
]

ROLE_LABELS = {
    "bull": "多头分析师",
    "bear": "空头审计师",
    "auditor": "独立风险评估师",
}


# ============================================================
#  Prompt 模板
# ============================================================

BULL_SYSTEM_PROMPT = """你是 FTSeek 港股系统的多头分析师。
你的任务是：基于提供的候选股票池和市场数据，给出做多建议。

## 分析框架
1. 从候选池中挑选 2-3 只最有做多价值的股票
2. 对每只股票给出：做多逻辑、技术面信号、催化剂、目标价
3. 给出整体市场情绪判断 (BULLISH / NEUTRAL / BEARISH)

## 输出格式 (必须严格 JSON)
{
  "market_sentiment": "BULLISH",
  "top_picks": [
    {"code": "HK.09988", "reason": "...", "target_price": 150.0, "confidence": 0.8}
  ],
  "key_catalysts": ["...", "..."],
  "risk_factors": ["..."],
  "summary": "一句话总结"
}

## 强制规范
1. 仅输出 JSON，不要额外文字
2. 股票代码必须带 HK. 前缀
3. confidence 范围 0.0-1.0
4. 如果数据不足以判断，返回空列表而非编造
"""

BEAR_SYSTEM_PROMPT = """你是 FTSeek 港股系统的空头审计师。
你的任务是：质疑所有做多逻辑，找出风险和弱点。

## 分析框架
1. 审查候选股票池的做多逻辑，找出漏洞
2. 指出当前市场环境下的下行风险
3. 列出需要回避的标的和理由
4. 给出整体风险判断

## 输出格式 (必须严格 JSON)
{
  "risk_level": "MEDIUM",
  "bearish_cases": [
    {"code": "HK.09988", "reason": "...", "risk_score": 0.7}
  ],
  "market_weaknesses": ["...", "..."],
  "avoid_list": ["...", "..."],
  "summary": "一句话总结风险核心"
}

## 强制规范
1. 仅输出 JSON，不要额外文字
2. 股票代码必须带 HK. 前缀
3. risk_score 范围 0.0-1.0，越高越危险
4. 即使没有明显做空信号，也要客观列出潜在风险
"""

AUDITOR_SYSTEM_PROMPT = """你是 FTSeek 港股系统的独立风险评估师。
你的任务是：基于多头和空头的观点，做出中立综合评估。

## 分析框架
1. 评估多空双方的论点质量
2. 给出中立评分 (0=极度悲观, 100=极度乐观)
3. 提出平衡的交易建议

## 输出格式 (必须严格 JSON)
{
  "neutral_score": 55,
  "bull_strength": "多头论点中 X 条有效",
  "bear_strength": "空头论点中 X 条有效",
  "balanced_view": "中立综合评估",
  "action_recommendation": "CAUTIOUS_BULLISH | NEUTRAL | CAUTIOUS_BEARISH",
  "position_size_pct": 0.3,
  "summary": "一句话总结"
}

## 强制规范
1. 仅输出 JSON，不要额外文字
2. neutral_score 范围 0-100
3. action_recommendation 必须是枚举值之一
4. position_size_pct 建议仓位占净值比例 (0.0-1.0)
"""


# ============================================================
#  PremarkDebateAgent
# ============================================================

class PremarkDebateAgent(Agent):
    """
    盘前多模型辩论 Agent

    并发调用 3 个 LLM 模型做多/空/中立分析，
    通过确定性 Arbiter 规则综合收敛为最终作战计划。
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        if config is None:
            config = AgentConfig(
                name="premarket-debate",
                timeout_seconds=120,
                retry_count=1,
                metadata={
                    "role": "盘前辩论协调器",
                    "capabilities": [
                        "multi_model_debate",
                        "premarket_analysis",
                        "arbiter_synthesis",
                    ],
                },
            )

        super().__init__(config)

        # API 配置 (从环境变量读取，与 FTSeek-US agent_bridge 一致)
        self.api_key = os.getenv("BAILIAN_API_KEY", "")
        self.base_url = os.getenv(
            "BAILIAN_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        # 可配置参数 (默认值)
        self.timeout: int = config.timeout_seconds
        self.max_rounds: int = 1  # 1=仅独立分析, 2=含交叉反驳
        self.stagger_delay: float = 0.5  # 模型调用间隔 (防限流)

        if not self.api_key:
            self.logger.warning(
                "[PremarkDebate] BAILIAN_API_KEY 未配置，LLM 调用将失败"
            )

    # ------------------------------------------------------------------
    #  公共接口
    # ------------------------------------------------------------------

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行盘前辩论

        Args:
            input_data: {
                "candidate_pool": [{"code": "HK.09988", "name": "...", ...}],
                "market_context": {"hsi_trend": "...", "us_overnight": ...},
                "date": "2026-05-06",
            }

        Returns:
            辩论结果字典
        """
        pool = input_data.get("candidate_pool", [])
        context = input_data.get("market_context", {})
        date = input_data.get("date", datetime.now().strftime("%Y-%m-%d"))

        if not self.api_key:
            return self._fallback_result("BAILIAN_API_KEY 未配置")

        self.logger.info(
            f"[PremarkDebate] 开始辩论 | date={date} | "
            f"候选池={len(pool)}只 | timeout={self.timeout}s"
        )

        # ── 构建 Prompt ──
        prompt = self._build_prompt(pool, context, date)

        # ── Round 1: 并发调用 3 个模型 ──
        round1_results, r1_success = self._run_round(
            prompt, self.timeout, round_num=1
        )

        if r1_success == 0:
            return self._fallback_result("所有模型 Round 1 调用失败")

        self.logger.info(
            f"[PremarkDebate] Round 1 完成: {r1_success}/{len(MODEL_ROLES)} 成功"
        )

        # ── Arbiter 综合 (确定性规则) ──
        arbiter_result = self._deterministic_arbiter(round1_results, pool)

        # ── 确定模式 ──
        mode = "debate" if r1_success == 3 else "partial"

        return {
            "mode": mode,
            "success_count": r1_success,
            "total_models": len(MODEL_ROLES),
            "results": round1_results,
            "arbiter_conclusion": arbiter_result,
            "date": date,
            "execution_time": time.time() - getattr(self, "_start_time", time.time()),
        }

    # ------------------------------------------------------------------
    #  并发调用
    # ------------------------------------------------------------------

    def _run_round(self, prompt: str, timeout: int, round_num: int) -> tuple:
        """执行一轮并行模型调用"""
        results = {}
        success_count = 0
        lock = threading.Lock()
        self._start_time = time.time()

        def call_one(cfg):
            nonlocal success_count

            # 防限流：错开启动
            if self.stagger_delay > 0:
                idx = MODEL_ROLES.index(cfg)
                time.sleep(idx * self.stagger_delay)

            role_prompt = self._get_role_system_prompt(cfg["role"])
            try:
                result = self._call_dashscope(
                    model=cfg["model"],
                    system_prompt=role_prompt,
                    user_prompt=prompt,
                    timeout=timeout,
                )
                if result:
                    with lock:
                        results[cfg["role"]] = result
                        results[f"{cfg['role']}_model"] = cfg["model"]
                        success_count += 1
                    self.logger.info(
                        f"[PremarkDebate] R{round_num} "
                        f"{cfg['label']} ({cfg['model']}) 成功"
                    )
                else:
                    self.logger.warning(
                        f"[PremarkDebate] R{round_num} "
                        f"{cfg['label']} ({cfg['model']}) 返回空"
                    )
            except Exception as e:
                self.logger.warning(
                    f"[PremarkDebate] R{round_num} "
                    f"{cfg['label']} ({cfg['model']}) 失败: {e}"
                )

        threads = []
        for cfg in MODEL_ROLES:
            t = threading.Thread(
                target=call_one,
                args=(cfg,),
                daemon=True,
                name=f"premark-r{round_num}-{cfg['role']}",
            )
            t.start()
            threads.append(t)

        # 统一超时
        deadline = time.time() + timeout
        for t in threads:
            remaining = max(0, deadline - time.time())
            if remaining > 0:
                t.join(timeout=remaining)

        return results, success_count

    def _call_dashscope(
        self, model: str, system_prompt: str, user_prompt: str, timeout: int
    ) -> Optional[Dict]:
        """直接 HTTP 调用 DashScope API"""
        if not self.api_key:
            return None

        max_tokens = 8192 if "deepseek" in model.lower() else 4096

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=timeout,
            )

            if resp.status_code == 429:
                self.logger.warning(
                    f"[PremarkDebate] API 限流 (429), 模型={model}"
                )
                return None

            if resp.status_code == 404:
                self.logger.warning(
                    f"[PremarkDebate] 模型不存在 (404): {model}"
                )
                return None

            resp.raise_for_status()
            data = resp.json()

            choices = data.get("choices")
            if not choices:
                self.logger.warning(
                    f"[PremarkDebate] API 返回空 choices: {data}"
                )
                return None

            text = choices[0].get("message", {}).get("content", "").strip()

            # 解析 JSON (容错处理)
            return self._parse_json(text)

        except requests.exceptions.Timeout:
            self.logger.warning(f"[PremarkDebate] 超时: model={model}")
            return None
        except Exception as e:
            self.logger.warning(f"[PremarkDebate] 调用失败: model={model}, err={e}")
            return None

    def _parse_json(self, text: str) -> Optional[Dict]:
        """从 LLM 返回文本中提取 JSON"""
        if not text:
            return None

        # 去除 markdown 代码块
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取第一个 { 到最后一个 }
            try:
                start = text.index("{")
                end = text.rindex("}") + 1
                return json.loads(text[start:end])
            except Exception:
                self.logger.warning(
                    f"[PremarkDebate] JSON 解析失败，原始文本: {text[:200]}"
                )
                return None

    # ------------------------------------------------------------------
    #  Prompt 构建
    # ------------------------------------------------------------------

    def _build_prompt(
        self, pool: List[Dict], context: Dict, date: str
    ) -> str:
        """构建辩论输入 Prompt (v2: 富途指标 + 跨市场推理框架)"""
        pool_str = json.dumps(pool[:10], ensure_ascii=False, indent=2)

        # ── 格式化富途指标 ──
        indicator_parts = []
        futu = context.get("futu_indicators", {})
        if futu:
            indicator_parts.append("## 市场环境指标 (富途实时数据)")
            for name, val in futu.items():
                if isinstance(val, dict):
                    chg = val.get("change_pct", 0)
                    indicator_parts.append(
                        f"- {name} ({val.get('code')}): {val.get('last_price', 0):.2f} ({chg:+.2%})"
                    )
                else:
                    indicator_parts.append(f"- {name}: {val}")

        # ── 格式化候选快照 ──
        snapshots = context.get("candidate_snapshots", [])
        if snapshots:
            indicator_parts.append("\n## 候选标的快照 (富途实时)")
            for s in snapshots:
                indicator_parts.append(
                    f"- {s['code']}: {s['last_price']:.2f} "
                    f"({s['change_pct']:+.2%}) "
                    f"量比={s['volume_ratio']:.1f}x "
                    f"换手={s['turnover']:.2%} "
                    f"振幅={s['amplitude_pct']:.2%}"
                )

        context_str = "\n".join(indicator_parts) if indicator_parts else "无实时数据"

        return f"""## FTSeek 港股盘前分析
日期: {date}

## 候选股票池
{pool_str}

## 市场数据
{context_str}

## 跨市场推理指引 (重要!)
请基于以下逻辑框架自行推导跨市场影响, **不要编造数据**:

1. **A股联动**: 上证/沪深300/创业板前日走势 → 港股开盘情绪 (同涨同跌概率 60%+)
2. **汇率影响**: USD/HKD 偏离 7.8 联系汇率 → 资金流向信号
3. **行业映射**: 若候选池含中概股 (阿里/京东/腾讯等), 需考虑美股对应 ADR 前夜表现
4. **地缘/宏观新闻**: 油价→能源股(中海油), 芯片管制→半导体(中芯), 政策→互联网
5. **AH 溢价**: 若 A 股大幅领先 H 股, 港股可能有补涨/补跌空间

⚠️ 强制约束:
- 仅基于已提供的指标推理, 不要引用未提供的数据
- 如果缺少某项数据 (如美股 ADR), 明确说明"数据缺失"而非猜测
- 所有推论需标注置信度 (高/中/低)

## 任务
请基于以上数据，按你的角色要求给出分析。
"""

    def _get_role_system_prompt(self, role: str) -> str:
        """获取角色 System Prompt"""
        if role == "bull":
            return BULL_SYSTEM_PROMPT
        elif role == "bear":
            return BEAR_SYSTEM_PROMPT
        elif role == "auditor":
            return AUDITOR_SYSTEM_PROMPT
        return ""

    # ------------------------------------------------------------------
    #  Arbiter (确定性规则综合)
    # ------------------------------------------------------------------

    def _deterministic_arbiter(
        self, results: Dict, pool: List[Dict]
    ) -> Dict:
        """
        确定性 Arbiter — 不依赖额外 LLM 调用

        基于 3 个模型的输出做规则综合:
        1. 收集多头 top_picks 和空头 avoid_list
        2. 计算每只标的的共识分数
        3. 生成最终作战计划
        """
        bull = results.get("bull", {})
        bear = results.get("bear", {})
        auditor = results.get("auditor", {})

        # ── 收集多头推荐 ──
        bull_picks = {}
        for pick in bull.get("top_picks", []):
            code = pick.get("code", "")
            if code:
                bull_picks[code] = {
                    "reason": pick.get("reason", ""),
                    "confidence": pick.get("confidence", 0.5),
                    "target_price": pick.get("target_price", 0),
                }

        # ── 收集空头风险 ──
        bear_risks = {}
        for case in bear.get("bearish_cases", []):
            code = case.get("code", "")
            if code:
                bear_risks[code] = {
                    "reason": case.get("reason", ""),
                    "risk_score": case.get("risk_score", 0.5),
                }

        # ── 收集回避列表 ──
        avoid_set = set(bear.get("avoid_list", []))

        # ── 计算共识分数 ──
        all_codes = set(list(bull_picks.keys()) + list(bear_risks.keys()))
        consensus = []
        for code in all_codes:
            bull_conf = bull_picks.get(code, {}).get("confidence", 0)
            bear_risk = bear_risks.get(code, {}).get("risk_score", 0)
            # 净信心 = 多头信心 - 空头风险
            net_confidence = bull_conf - bear_risk
            is_avoid = code in avoid_set

            consensus.append({
                "code": code,
                "net_confidence": round(net_confidence, 2),
                "bull_confidence": bull_conf,
                "bear_risk": bear_risk,
                "is_avoid": is_avoid,
                "reason_bull": bull_picks.get(code, {}).get("reason", ""),
                "reason_bear": bear_risks.get(code, {}).get("reason", ""),
            })

        # 按净信心排序
        consensus.sort(key=lambda x: x["net_confidence"], reverse=True)

        # ── 综合市场情绪 ──
        market_sentiment = bull.get("market_sentiment", "NEUTRAL")
        risk_level = bear.get("risk_level", "MEDIUM")
        auditor_score = auditor.get("neutral_score", 50)
        action_rec = auditor.get("action_recommendation", "NEUTRAL")
        position_pct = auditor.get("position_size_pct", 0.3)

        return {
            "consensus_ranking": consensus[:5],  # 前 5 只
            "market_sentiment": market_sentiment,
            "risk_level": risk_level,
            "auditor_score": auditor_score,
            "action_recommendation": action_rec,
            "suggested_position_pct": position_pct,
            "avoid_codes": list(avoid_set),
            "bull_summary": bull.get("summary", ""),
            "bear_summary": bear.get("summary", ""),
            "auditor_summary": auditor.get("summary", ""),
        }

    # ------------------------------------------------------------------
    #  Fallback
    # ------------------------------------------------------------------

    def _fallback_result(self, reason: str) -> Dict:
        """LLM 全部失败时的兜底结果"""
        self.logger.warning(f"[PremarkDebate] 降级到规则模式: {reason}")
        return {
            "mode": "fallback",
            "success_count": 0,
            "total_models": len(MODEL_ROLES),
            "results": {},
            "arbiter_conclusion": {
                "consensus_ranking": [],
                "market_sentiment": "NEUTRAL",
                "risk_level": "UNKNOWN",
                "auditor_score": 50,
                "action_recommendation": "NEUTRAL",
                "suggested_position_pct": 0.2,
                "avoid_codes": [],
                "bull_summary": f"LLM 不可用: {reason}",
                "bear_summary": "同上",
                "auditor_summary": "降级模式",
            },
            "fallback_reason": reason,
        }
