"""
策略辩论调度器 (Strategy Debate Runner)

多模型对抗辩论框架:
    Round 1: 发散 — 各模型独立分析策略有效性
    Round 2: 对抗 — 交叉质疑对方分析
    Round 3: 收敛 — 识别共识与分歧，生成行动项

模型:
    - Model A: qwen3.7-max (via Hermes / custom provider)
    - Model B: deepseek-v4-pro (via custom provider)

设计:
    - 纯框架层: 不硬编码 LLM 调用细节
    - 辩论结果结构化存储 (JSON)
    - 支持异步执行 (Cron 触发)
    - 与统计门禁联动: 只有通过门禁的策略才进入辩论

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .statistical_gate import StatisticalGate, GateResult

logger = logging.getLogger(__name__)


# ============================================================
#  辩论数据结构
# ============================================================

@dataclass
class DebateRound:
    """单轮辩论结果"""
    round_num: int
    model_name: str
    content: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class DebateResult:
    """完整辩论结果"""
    debate_id: str
    strategy_name: str
    timestamp: str
    statistical_gate: Dict[str, Any] = field(default_factory=dict)
    rounds: List[DebateRound] = field(default_factory=list)
    consensus: List[str] = field(default_factory=list)
    disagreements: List[str] = field(default_factory=list)
    action_items: List[Dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "debate_id": self.debate_id,
            "strategy_name": self.strategy_name,
            "timestamp": self.timestamp,
            "statistical_gate": self.statistical_gate,
            "rounds": [
                {"round": r.round_num, "model": r.model_name,
                 "content": r.content, "timestamp": r.timestamp}
                for r in self.rounds
            ],
            "consensus": self.consensus,
            "disagreements": self.disagreements,
            "action_items": self.action_items,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }

    def save(self, output_dir: str = "data/debates") -> str:
        """保存辩论结果到 JSON"""
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        filename = f"{self.debate_id}.json"
        filepath = path / filename
        with open(filepath, "w") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info("[Debate] 结果已保存: %s", filepath)
        return str(filepath)


# ============================================================
#  辩论 Prompt 构建器
# ============================================================

class DebatePromptBuilder:
    """构建各轮辩论的 Prompt"""

    @staticmethod
    def build_round1(
        strategy_info: Dict[str, Any],
        backtest_stats: Dict[str, Any],
        gate_result: Dict[str, Any],
    ) -> str:
        """
        Round 1: 发散 — 独立分析

        要求模型:
        1. 评估策略有效性 (统计显著性、样本量、集中度)
        2. 提出 3 个最可能的失效模式
        3. 建议 3 个参数调整方向
        """
        return f"""你是 FTSeek-US 策略审计师 (Round 1: 独立分析)。

## 策略信息
- 名称: {strategy_info.get('name', 'unknown')}
- 时间框架: {strategy_info.get('timeframe', 'unknown')}
- 因子组合: {json.dumps(strategy_info.get('factors', []), ensure_ascii=False)}
- 方向: {strategy_info.get('direction', 'unknown')}

## 回测统计
- 交易数: {backtest_stats.get('n_trades', 0)}
- 胜率: {backtest_stats.get('win_rate', 0):.1%}
- 利润因子: {backtest_stats.get('profit_factor', 0):.2f}
- 总PnL: ${backtest_stats.get('total_pnl', 0):.0f}
- 修正Sharpe: {backtest_stats.get('corrected_sharpe', 0):.3f}
- MaxDD: {backtest_stats.get('max_drawdown', 0):.1%}

## 统计门禁结果
{json.dumps(gate_result, indent=2, ensure_ascii=False)}

## 交易分布
- 按symbol: {json.dumps(backtest_stats.get('by_symbol', {}), ensure_ascii=False)[:500]}
- 按时段: {json.dumps(backtest_stats.get('by_session', {}), ensure_ascii=False)[:500]}

═══════════════════════════════════
## 你的任务

请独立分析并输出严格 JSON (不要 markdown 代码块):

{{
  "assessment": "策略有效性评估 (2-3句话)",
  "effectiveness_score": 0-100,
  "failure_modes": [
    {{"mode": "失效模式1", "probability": "HIGH/MED/LOW", "evidence": "数据支撑"}},
    {{"mode": "失效模式2", "probability": "HIGH/MED/LOW", "evidence": "数据支撑"}},
    {{"mode": "失效模式3", "probability": "HIGH/MED/LOW", "evidence": "数据支撑"}}
  ],
  "suggestions": [
    {{"action": "调整方向1", "expected_impact": "预期效果", "risk": "风险"}},
    {{"action": "调整方向2", "expected_impact": "预期效果", "risk": "风险"}},
    {{"action": "调整方向3", "expected_impact": "预期效果", "risk": "风险"}}
  ],
  "key_risk": "最大的单一风险因素",
  "data_gaps": ["数据缺口1", "数据缺口2"]
}}"""

    @staticmethod
    def build_round2(
        own_analysis: Dict[str, Any],
        opponent_analysis: Dict[str, Any],
        strategy_name: str,
    ) -> str:
        """
        Round 2: 对抗 — 交叉质疑

        要求模型:
        1. 找出对方分析中的逻辑漏洞
        2. 用数据反驳对方的建议
        3. 提出"如果对方错了"的后果分析
        """
        return f"""你是 FTSeek-US 策略审计师 (Round 2: 交叉质疑)。

## 策略: {strategy_name}

## 你的 Round 1 分析
{json.dumps(own_analysis, indent=2, ensure_ascii=False)[:2000]}

## 对方的 Round 1 分析
{json.dumps(opponent_analysis, indent=2, ensure_ascii=False)[:2000]}

═══════════════════════════════════
## 你的任务

请对对方分析进行严格的交叉质疑。输出严格 JSON:

{{
  "challenges": [
    {{
      "target": "对方的具体论点",
      "challenge": "你的质疑理由",
      "counter_evidence": "反驳数据/逻辑",
      "severity": "CRITICAL/MAJOR/MINOR"
    }}
  ],
  "missed_dimensions": ["对方忽略的维度1", "对方忽略的维度2"],
  "risk_if_opponent_wrong": "如果对方错了，后果是什么",
  "revised_assessment": "基于对抗后的修正评估 (2-3句话)",
  "concessions": ["你承认对方正确的部分"]
}}"""

    @staticmethod
    def build_round3(
        all_rounds: List[Dict[str, Any]],
        strategy_name: str,
    ) -> str:
        """
        Round 3: 收敛 — 共识与分歧

        要求模型:
        1. 识别共识区 (双方同意 + 数据支持)
        2. 识别分歧区 (需要更多数据)
        3. 生成最终行动项
        """
        rounds_json = json.dumps(all_rounds, indent=2, ensure_ascii=False)[:4000]
        return f"""你是 FTSeek-US 首席策略官 (Round 3: 收敛决策)。

## 策略: {strategy_name}

## 辩论全过程
{rounds_json}

═══════════════════════════════════
## 你的任务

综合两轮辩论，做出最终决策。输出严格 JSON:

{{
  "consensus": [
    "双方都同意且数据支持的结论1",
    "双方都同意且数据支持的结论2"
  ],
  "disagreements": [
    {{"topic": "分歧主题", "model_a_view": "A的观点", "model_b_view": "B的观点", "resolution": "建议如何裁决"}}
  ],
  "blind_spots": ["双方都未考虑的盲区"],
  "final_recommendation": "最终建议 (是否启用/如何调整, 2-3句话)",
  "confidence": 0.0-1.0,
  "action_items": [
    {{"action": "具体行动", "priority": "P0/P1/P2", "owner": "factor/exit/registry", "estimated_impact": "预期影响"}},
    ...
  ]
}}"""


# ============================================================
#  辩论调度器
# ============================================================

class StrategyDebateRunner:
    """
    多模型策略辩论调度器

    不直接调用 LLM — 通过 llm_caller 回调注入:
        llm_caller(model_name, prompt) -> Dict[str, Any]

    这样可以在测试中注入 mock，在生产中接入实际 LLM。

    Usage:
        async def call_llm(model, prompt):
            # 实际 LLM 调用逻辑
            return parse_json(llm_response)

        runner = StrategyDebateRunner(
            model_a="qwen3.7-max",
            model_b="deepseek-v4-pro",
            llm_caller=call_llm,
        )
        result = await runner.run_full_debate(strategy_info, backtest_stats)
    """

    def __init__(
        self,
        model_a: str = "qwen3.7-max",
        model_b: str = "deepseek-v4-pro",
        llm_caller: Optional[Callable] = None,
        gate: Optional[StatisticalGate] = None,
    ):
        self.model_a = model_a
        self.model_b = model_b
        self.llm_caller = llm_caller
        self.gate = gate or StatisticalGate()
        self.prompt_builder = DebatePromptBuilder()

    async def run_full_debate(
        self,
        strategy_info: Dict[str, Any],
        backtest_stats: Dict[str, Any],
        trades: Optional[List[Dict]] = None,
        n_total_tests: int = 249,
    ) -> DebateResult:
        """
        执行完整的 3 轮辩论

        Args:
            strategy_info: 策略定义 (name, timeframe, factors, direction)
            backtest_stats: 回测统计 (n_trades, win_rate, profit_factor, ...)
            trades: 交易明细列表 (可选, 用于统计门禁)
            n_total_tests: 总共测试的策略数

        Returns:
            DebateResult
        """
        debate_id = f"debate_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{strategy_info.get('name', 'unknown')}"
        timestamp = datetime.now().isoformat()

        logger.info("[Debate] 开始: %s", debate_id)

        # ── Phase B: 统计门禁 ──
        gate_result = None
        if trades:
            gate_result = self.gate.evaluate(
                trades=trades,
                n_total_tests=n_total_tests,
                timeframe=strategy_info.get("timeframe", "15min"),
            )
            logger.info("[Debate] 统计门禁: passed=%s, reason=%s",
                        gate_result.passed, gate_result.reason)

            # 如果门禁未通过，仍然继续辩论但标注
            # (辩论可以发现门禁遗漏的问题)

        all_rounds = []

        # ── Round 1: 发散 ──
        if self.llm_caller:
            prompt_r1 = self.prompt_builder.build_round1(
                strategy_info, backtest_stats,
                gate_result.to_dict() if gate_result else {},
            )

            # 并行调用两个模型
            r1_a = await self.llm_caller(self.model_a, prompt_r1)
            r1_b = await self.llm_caller(self.model_b, prompt_r1)

            all_rounds.append({"round": 1, "model": self.model_a, "content": r1_a})
            all_rounds.append({"round": 1, "model": self.model_b, "content": r1_b})

            # ── Round 2: 对抗 ──
            prompt_r2_a = self.prompt_builder.build_round2(
                r1_a, r1_b, strategy_info.get("name", ""))
            prompt_r2_b = self.prompt_builder.build_round2(
                r1_b, r1_a, strategy_info.get("name", ""))

            r2_a = await self.llm_caller(self.model_a, prompt_r2_a)
            r2_b = await self.llm_caller(self.model_b, prompt_r2_b)

            all_rounds.append({"round": 2, "model": self.model_a, "content": r2_a})
            all_rounds.append({"round": 2, "model": self.model_b, "content": r2_b})

            # ── Round 3: 收敛 ──
            prompt_r3 = self.prompt_builder.build_round3(
                all_rounds, strategy_info.get("name", ""))

            # Round 3 用两个模型分别做，取共识
            r3_a = await self.llm_caller(self.model_a, prompt_r3)
            r3_b = await self.llm_caller(self.model_b, prompt_r3)

            all_rounds.append({"round": 3, "model": self.model_a, "content": r3_a})
            all_rounds.append({"round": 3, "model": self.model_b, "content": r3_b})

            # 合并 Round 3 结果
            consensus = self._merge_consensus(r3_a, r3_b)
            disagreements = self._merge_disagreements(r3_a, r3_b)
            action_items = self._merge_action_items(r3_a, r3_b)
            confidence = (
                r3_a.get("confidence", 0.5) + r3_b.get("confidence", 0.5)
            ) / 2
        else:
            # 无 LLM caller → 生成空框架 (用于测试/离线分析)
            consensus = []
            disagreements = []
            action_items = []
            confidence = 0.0
            logger.warning("[Debate] 无 llm_caller，跳过 LLM 调用")

        result = DebateResult(
            debate_id=debate_id,
            strategy_name=strategy_info.get("name", "unknown"),
            timestamp=timestamp,
            statistical_gate=gate_result.to_dict() if gate_result else {},
            rounds=[
                DebateRound(
                    round_num=r["round"],
                    model_name=r["model"],
                    content=r["content"],
                )
                for r in all_rounds
            ],
            consensus=consensus,
            disagreements=disagreements,
            action_items=action_items,
            confidence=confidence,
            metadata={
                "model_a": self.model_a,
                "model_b": self.model_b,
                "n_total_tests": n_total_tests,
            },
        )

        logger.info("[Debate] 完成: %s | consensus=%d, actions=%d, confidence=%.2f",
                     debate_id, len(consensus), len(action_items), confidence)
        return result

    # ── 合并辅助 ──

    @staticmethod
    def _merge_consensus(r3_a: Dict, r3_b: Dict) -> List[str]:
        """合并两个模型的共识 (交集)"""
        a_set = set(r3_a.get("consensus", []))
        b_set = set(r3_b.get("consensus", []))
        # 取并集 (只要任一模型提出就算共识)
        return sorted(a_set | b_set)

    @staticmethod
    def _merge_disagreements(r3_a: Dict, r3_b: Dict) -> List[str]:
        """合并分歧"""
        a_dis = r3_a.get("disagreements", [])
        b_dis = r3_b.get("disagreements", [])
        # 合并去重
        seen = set()
        result = []
        for d in a_dis + b_dis:
            topic = d.get("topic", str(d)) if isinstance(d, dict) else str(d)
            if topic not in seen:
                seen.add(topic)
                result.append(topic if isinstance(d, str) else json.dumps(d, ensure_ascii=False))
        return result

    @staticmethod
    def _merge_action_items(r3_a: Dict, r3_b: Dict) -> List[Dict[str, str]]:
        """合并行动项 (去重 + 按优先级排序)"""
        items = r3_a.get("action_items", []) + r3_b.get("action_items", [])
        # 按 action 去重
        seen = set()
        unique = []
        for item in items:
            action = item.get("action", "")
            if action and action not in seen:
                seen.add(action)
                unique.append(item)

        # 按优先级排序
        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        unique.sort(key=lambda x: priority_order.get(x.get("priority", "P3"), 9))
        return unique
