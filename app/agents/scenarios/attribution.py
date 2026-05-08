"""
Trade attribution scenario definition.

2 analysts (DirectionAttribution + ExecutionAttribution),
1 round, merge results. Used by midday mini review and EOD review.

Prompts migrated from app/attribution/llm_attribution.py (SSOT).
"""

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
{
  "cause_category": "direction_wrong|timing_poor|market_misread|signal_noise|reasonable_loss",
  "root_cause": "一句话根因 (中文，不超过40字)",
  "lesson_text": "可操作的教训 (中文，不超过80字)",
  "improvement_action": "具体改进动作 (中文，不超过60字)",
  "confidence": 0.0-1.0,
  "key_finding": "最关键的一个发现"
}

如果交易本身是合理的 (方向对、执行对、只是市场不利)，cause_category 填 "reasonable_loss"。
不要为了找原因而强行归因。诚实比聪明更重要。"""

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
{
  "cause_category": "execution_aligned|execution_deviation|plan_missing|overtrade|undertrade|manual_override",
  "root_cause": "一句话根因 (中文，不超过40字)",
  "lesson_text": "可操作的教训 (中文，不超过80字)",
  "improvement_action": "具体改进动作 (中文，不超过60字)",
  "confidence": 0.0-1.0,
  "key_finding": "最关键的一个发现"
}"""

ATTRIBUTION_SCENARIO = {
    "name": "attribution",
    "description": "2-analyst trade attribution (direction + execution) — 1 round, merge",
    "agents": [
        {
            "id": "direction",
            "name": "DirectionAttributionAnalyst",
            "system_prompt": DIRECTION_ATTRIBUTION_PROMPT,
            "model": "qwen3.6-flash",
            "timeout": 60,
            "context_key": "default",
        },
        {
            "id": "execution",
            "name": "ExecutionAttributionAnalyst",
            "system_prompt": EXECUTION_ATTRIBUTION_PROMPT,
            "model": "qwen3.6-flash",
            "timeout": 60,
            "context_key": "default",
        },
    ],
    "rounds": [
        {
            "round": 1,
            "prompt_template": "{data_context}",
            "template_keys": ["data_context"],
            "parallel": True,
        },
    ],
    "synthesis": None,
    "context_keys": ["default"],
    "rounds_count": 1,
}
