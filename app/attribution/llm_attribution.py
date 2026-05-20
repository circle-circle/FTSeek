"""
LLM 归因引擎 — 多视角 LLM 归因替代纯规则归因

两个 LLM 视角 (每笔亏损交易并行调用 fast 模型):

1. DirectionAttributionAnalyst — 方向/时机/市场状态判断
   输入: {entry/exit价格, P&L, 盘前计划方向, 实际走势, 信号上下文}
   核心问题: "方向错了还是时机错了？市场状态判断是否正确？"

2. ExecutionContextAnalyst — 执行对齐/计划遵从度
   输入: {执行数据, 作战计划匹配, 时间阶段, 出场分析}
   核心问题: "执行是否对齐计划？计划说'等确认'但直接入场了？"
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.attribution.llm")


@dataclass
class LLMAttributionResult:
    """LLM 归因结果"""
    cause_category: str = "unknown"       # direction_wrong / timing_poor / execution_deviation / plan_gap
    root_cause: str = ""                  # 根因描述
    lesson_text: str = ""                 # 可操作教训
    improvement_action: str = ""          # 改进动作
    confidence: float = 0.0               # LLM 自评置信度
    key_finding: str = ""                 # 关键发现 (一句话)
    raw_response: str = ""                # LLM 原始响应


DIRECTION_ATTRIBUTION_PROMPT = """你是 FTSeek 的**方向归因分析师 (Direction Attribution Analyst)**。

你的任务是分析一笔港股交易，判断亏损的根本原因。

【分析框架】
1. 方向判断是否正确？如果方向对但亏损了，是时机问题还是止损太紧？
2. 市场状态判断是否正确？趋势判断是否与实际情况一致？
3. 入场时机是否合理？是在趋势确认后入场，还是追高/抄底？
4. 这笔交易的决策逻辑是否存在根本性缺陷？

【输入数据】
{trade_data}

【输出要求】
必须输出严格 JSON:
{{
  "cause_category": "direction_wrong|timing_poor|market_misread|signal_noise|reasonable_loss",
  "root_cause": "一句话根因 (中文，不超过40字)",
  "lesson_text": "可操作的教训 (中文，不超过80字)",
  "improvement_action": "具体改进动作 (中文，不超过60字)",
  "confidence": 0.0-1.0,
  "key_finding": "最关键的一个发现"
}}

如果交易本身是合理的 (方向对、执行对、只是市场不利)，cause_category 填 "reasonable_loss"。
不要为了找原因而强行归因。诚实比聪明更重要。

【示例输出】
{{
  "cause_category": "timing_poor",
  "root_cause": "信号方向正确但入场过早，未等 ADX>25 确认趋势",
  "lesson_text": "在震荡市中，即使方向判断正确，过早入场也会被止损出局。应等待趋势确认信号后再入场。",
  "improvement_action": "增加 ADX 确认过滤，要求 ADX>25 且 DI+>DI- 后再开多仓",
  "confidence": 0.85,
  "key_finding": "入场时机比方向判断更重要"
}}"""

EXECUTION_ATTRIBUTION_PROMPT = """你是 FTSeek 的**执行归因分析师 (Execution Attribution Analyst)**。

你的任务是分析一笔交易执行是否对齐了作战计划。

【分析框架】
1. 入场是否符合作战计划的条件？计划说等什么信号？实际等了吗？
2. 仓位是否符合计划限制？有没有超仓或欠仓？
3. 出场是否符合止损/止盈规则？是规则触发还是情绪化操作？
4. 是否有计划外的交易？为什么会发生？

【输入数据】
{trade_data}

【输出要求】
必须输出严格 JSON:
{{
  "cause_category": "execution_aligned|execution_deviation|plan_missing|overtrade|undertrade|manual_override",
  "root_cause": "一句话根因 (中文，不超过40字)",
  "lesson_text": "可操作的教训 (中文，不超过80字)",
  "improvement_action": "具体改进动作 (中文，不超过60字)",
  "confidence": 0.0-1.0,
  "key_finding": "最关键的一个发现"
}}

如果执行完全对齐计划，cause_category 填 "execution_aligned"。
不要为了找问题而强行找问题。诚实比聪明更重要。

【示例输出】
{{
  "cause_category": "execution_deviation",
  "root_cause": "计划要求等 MACD 金叉确认，但实际在 MACD 死叉时提前入场",
  "lesson_text": "严格遵守作战计划的入场条件，不因'感觉要涨'而提前入场。计划外的入场往往带来更大的滑点和风险。",
  "improvement_action": "增加入场条件校验，入场前检查 MACD/RSI/ADX 是否满足计划要求",
  "confidence": 0.90,
  "key_finding": "执行纪律比判断方向更难"
}}"""

PLAN_GAP_PROMPT = """你是 FTSeek 的**计划差距分析师 (Plan Gap Analyst)**。

分析今日作战计划与实际交易之间的差距。

【作战计划目标】
{plan_targets}

【实际交易标的】
{traded_symbols}

【持仓标的】
{position_symbols}

【输出要求】
输出严格 JSON:
{{
  "planned_and_traded": ["被交易的计划标的"],
  "planned_but_missed": ["计划中但未交易的标的及可能原因"],
  "traded_off_plan": ["计划外交易的标的及可能原因"],
  "gap_root_cause": "计划与实际差距的根本原因 (中文，不超过60字)",
  "plan_quality_assessment": "计划本身的质量评估 (GOOD|FAIR|POOR)",
  "suggestion": "改进建议 (中文，不超过80字)"
}}

如果大部分计划标的被正确执行，plan_quality_assessment 应为 GOOD。"""


def _format_trade_for_direction(trade: Dict, battle_plan: Dict) -> str:
    """将交易数据格式化为方向归因的输入"""
    code = trade.get("code", "")
    pnl = trade.get("pnl", trade.get("realized_pnl", 0))
    entry_px = trade.get("entry_price", 0)
    exit_px = trade.get("exit_price", 0)
    qty = trade.get("quantity", 0)
    entry_time = trade.get("entry_time", "")
    exit_time = trade.get("exit_time", "")
    direction = trade.get("direction", trade.get("action", "UNKNOWN"))
    exit_reason = trade.get("exit_reason", "")
    signal = trade.get("signal_summary", {}) or {}
    time_phase = trade.get("time_phase", {}) or {}

    parts = [
        f"标的: {code}",
        f"方向: {direction}",
        f"数量: {qty}",
        f"入场价: {entry_px} | 出场价: {exit_px}",
        f"入场时间: {entry_time} | 出场时间: {exit_time}",
        f"盈亏: {pnl}",
        f"出场原因: {exit_reason}",
    ]

    if signal:
        parts.append(f"信号评分: {signal.get('signal_score', 'N/A')}")
        parts.append(f"AI情绪: {signal.get('ai_sentiment', 'N/A')}")
        parts.append(f"决策理由: {signal.get('decision_reason', 'N/A')}")
        parts.append(f"市场状态: {signal.get('market_regime', 'N/A')}")

    if time_phase:
        parts.append(f"入场阶段: {time_phase.get('entry_phase', 'N/A')}")
        parts.append(f"出场阶段: {time_phase.get('exit_phase', 'N/A')}")
        parts.append(f"持仓时长: {time_phase.get('hold_minutes', 'N/A')}分钟")

    # 作战计划匹配
    plan_targets = battle_plan.get("targets", [])
    plan_match = None
    for t in plan_targets:
        if t.get("symbol") == code:
            plan_match = t
            break

    if plan_match:
        parts.append(f"计划方向: {plan_match.get('direction', 'N/A')}")
        parts.append(f"计划紧迫度: {plan_match.get('urgency', 'N/A')}")
        parts.append(f"计划触发条件: {plan_match.get('key_trigger', 'N/A')[:80]}")
    else:
        parts.append("作战计划匹配: 无 (计划外交易)")

    return "\n".join(parts)


def _format_trade_for_execution(trade: Dict, battle_plan: Dict) -> str:
    """将交易数据格式化为执行归因的输入"""
    code = trade.get("code", "")
    pnl = trade.get("pnl", trade.get("realized_pnl", 0))
    qty = trade.get("quantity", 0)
    entry_time = trade.get("entry_time", "")
    exit_time = trade.get("exit_time", "")
    exit_analysis = trade.get("exit_analysis", {}) or {}
    time_phase = trade.get("time_phase", {}) or {}

    parts = [
        f"标的: {code}",
        f"数量: {qty}",
        f"入场时间: {entry_time} | 出场时间: {exit_time}",
        f"盈亏: {pnl}",
        f"出场类型: {exit_analysis.get('type', 'N/A')}",
        f"出场详情: {exit_analysis.get('detail', 'N/A')}",
        f"出场质量: {exit_analysis.get('quality', 'N/A')}",
    ]

    if time_phase:
        parts.append(f"入场阶段: {time_phase.get('entry_phase', 'N/A')}")
        parts.append(f"持仓时长: {time_phase.get('hold_minutes', 'N/A')}分钟")

    # 作战计划匹配
    plan_targets = battle_plan.get("targets", [])
    plan_match = None
    for t in plan_targets:
        if t.get("symbol") == code:
            plan_match = t
            break

    if plan_match:
        parts.append(f"计划方向: {plan_match.get('direction', 'N/A')}")
        parts.append(f"计划仓位上限: {plan_match.get('max_position_pct', 'N/A')}")
        parts.append(f"计划触发条件: {plan_match.get('key_trigger', 'N/A')[:80]}")
        invalidation = plan_match.get("invalidation_reason", "")
        if invalidation:
            parts.append(f"计划失效条件: {invalidation[:80]}")
    else:
        parts.append("作战计划匹配: 无 (计划外交易)")

    return "\n".join(parts)


async def run_direction_attribution(trade: Dict, battle_plan: Dict,
                                     config: Dict = None) -> LLMAttributionResult:
    """方向归因 (通过 agent_bridge — P2 2026-05-06)"""
    try:
        from app.llm.agent_bridge import get_ftseek_agent_bridge
        bridge = get_ftseek_agent_bridge("hk")

        payload = {
            "trade": json.dumps(trade, ensure_ascii=False, default=str)[:2000],
            "battle_plan": json.dumps(battle_plan, ensure_ascii=False)[:1000],
        }

        result = bridge.enqueue_task("attribution_direction", payload)
        if result:
            return LLMAttributionResult(
                cause_category=result.get("cause_category", "unknown"),
                root_cause=result.get("root_cause", ""),
                lesson_text=result.get("lesson_text", ""),
                improvement_action=result.get("improvement_action", ""),
                confidence=float(result.get("confidence", 0)),
                key_finding=result.get("key_finding", ""),
                raw_response=json.dumps(result, ensure_ascii=False),
            )
    except Exception as e:
        logger.warning(f"[Attribution] agent_bridge 失败，降级: {e}")

    # 降级: 使用 UnifiedLLMService (2026-05-21: 升级到 qwen-plus 提升归因质量)
    trade_text = _format_trade_for_direction(trade, battle_plan)
    messages = [
        {"role": "system", "content": DIRECTION_ATTRIBUTION_PROMPT},
        {"role": "user", "content": trade_text},
    ]
    from app.llm.unified_service import get_unified_llm, UnifiedLLMService
    svc = get_unified_llm(default_model="qwen-plus", default_timeout=60, max_retries=2)
    raw = await svc.call(messages, model="qwen-plus", timeout=60, task_type="direction_attribution")
    data = svc.extract_json(raw) if raw else {}

    return LLMAttributionResult(
        cause_category=data.get("cause_category", "unknown"),
        root_cause=data.get("root_cause", ""),
        lesson_text=data.get("lesson_text", ""),
        improvement_action=data.get("improvement_action", ""),
        confidence=float(data.get("confidence", 0)),
        key_finding=data.get("key_finding", ""),
        raw_response=raw,
    )


async def run_execution_attribution(trade: Dict, battle_plan: Dict,
                                     config: Dict = None) -> LLMAttributionResult:
    """执行归因 (fast 模型)"""
    trade_text = _format_trade_for_execution(trade, battle_plan)
    messages = [
        {"role": "system", "content": EXECUTION_ATTRIBUTION_PROMPT},
        {"role": "user", "content": trade_text},
    ]
    from app.llm.unified_service import get_unified_llm, UnifiedLLMService
    svc = get_unified_llm(default_model="qwen-plus", default_timeout=60, max_retries=2)
    raw = await svc.call(messages, model="qwen-plus", timeout=60, task_type="execution_attribution")
    data = svc.extract_json(raw) if raw else {}

    return LLMAttributionResult(
        cause_category=data.get("cause_category", "unknown"),
        root_cause=data.get("root_cause", ""),
        lesson_text=data.get("lesson_text", ""),
        improvement_action=data.get("improvement_action", ""),
        confidence=float(data.get("confidence", 0)),
        key_finding=data.get("key_finding", ""),
        raw_response=raw,
    )


async def run_plan_gap_analysis(plan: Dict, trades: List[Dict],
                                 positions: List[Dict],
                                 config: Dict = None) -> Dict:
    """作战计划 vs 实际执行差距分析 (fast 模型)"""
    plan_targets = plan.get("targets", [])
    plan_symbols_text = "\n".join(
        f"- {t.get('symbol', '?')}: {t.get('direction', '?')} "
        f"紧迫度={t.get('urgency', '?')} 触发={t.get('key_trigger', '')[:60]}"
        for t in plan_targets
    ) if plan_targets else "(无计划目标)"

    traded_text = "\n".join(
        f"- {t.get('code', '?')}: P&L={t.get('pnl', t.get('realized_pnl', 0)):.0f} "
        f"{t.get('direction', t.get('action', '?'))}"
        for t in trades
    ) if trades else "(无交易)"

    positions_text = "\n".join(
        f"- {p.get('code', '?')}: {p.get('quantity', 0)}股 未实现={p.get('unrealized_pnl', 0):.0f}"
        for p in (positions or [])[:10]
    ) if positions else "(无持仓)"

    prompt = PLAN_GAP_PROMPT.format(
        plan_targets=plan_symbols_text,
        traded_symbols=traded_text,
        position_symbols=positions_text,
    )
    messages = [
        {"role": "system", "content": "你是计划差距分析师。只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    from app.llm.unified_service import get_unified_llm, UnifiedLLMService
    svc = get_unified_llm(default_model="qwen-plus", default_timeout=60, max_retries=2)
    return await svc.call_and_parse(messages, model="qwen-plus", timeout=60, task_type="plan_gap_analysis")


def run_attribution_sync(trade: Dict, battle_plan: Dict,
                          config: Dict = None) -> Dict:
    """同步执行单笔交易的 LLM 归因 (方向+执行并行)

    Returns:
        {"direction": LLMAttributionResult, "execution": LLMAttributionResult}
    """
    async def _run():
        dir_result, exec_result = await asyncio.gather(
            run_direction_attribution(trade, battle_plan, config),
            run_execution_attribution(trade, battle_plan, config),
        )
        return {"direction": dir_result, "execution": exec_result}

    from app.llm.unified_service import UnifiedLLMService
    return UnifiedLLMService.run_sync(_run(), timeout=120)


def run_plan_gap_sync(plan: Dict, trades: List[Dict],
                       positions: List[Dict],
                       config: Dict = None) -> Dict:
    """同步执行计划差距分析"""
    from app.llm.unified_service import UnifiedLLMService
    return UnifiedLLMService.run_sync(
        run_plan_gap_analysis(plan, trades, positions, config),
        timeout=60,
    )
