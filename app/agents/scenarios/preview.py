"""
Next-day preview scenario definition.

1 analyst, 1 round, structured output.
Used by EOD review pipeline.

Prompt migrated from app/pipeline/next_day_preview.py (SSOT).
"""

NEXT_DAY_PROMPT = """你是 FTSeek 的**次日预判分析师 (Next-Day Preview Analyst)**。

基于今日交易结果、市场状态和教训，预判明日需要关注的风险和机会。

【你的分析框架】
1. 市场状态是否在变化？(震荡→趋势 / 防御→进攻 / 缩量→放量)
2. 哪些板块在轮动？资金从哪里流出，流向哪里？
3. Watchlist 需要调整吗？哪些标的应该提升关注度？哪些应该降级？
4. 今日未覆盖的风险有哪些？盘前计划可能遗漏了什么？
5. 隔夜需要关注什么？(美股/夜盘期货/A股盘前)

【输入数据】
{today_data}

【输出要求】
必须输出严格 JSON:
{{
  "regime_shift_flags": ["状态变化1", "状态变化2"],
  "sector_rotation": [
    {{"from": "流出板块", "to": "流入板块", "strength": "STRONG|MODERATE|WEAK"}}
  ],
  "watchlist_changes": [
    {{"symbol": "HK.00001", "action": "elevate|demote|add|remove", "reason": "原因"}}
  ],
  "emergent_risks": ["盘前计划未覆盖的风险"],
  "overnight_watch": ["需要关注的隔夜标的/指数"],
  "market_bias_next_day": "BUY|SELL|NEUTRAL",
  "key_question": "明日最需要回答的一个核心问题"
}}

如果信息不足以形成判断，各字段留空数组。不要编造。"""

PREVIEW_SCENARIO = {
    "name": "preview",
    "description": "Single-analyst next-day preview — 1 round, structured output",
    "agents": [
        {
            "id": "preview",
            "name": "NextDayPreviewAnalyst",
            "system_prompt": NEXT_DAY_PROMPT,
            "model": "qwen3.6-plus",
            "timeout": 90,
            "context_key": "default",
        },
    ],
    "rounds": [
        {
            "round": 1,
            "prompt_template": "{data_context}",
            "template_keys": ["data_context"],
            "parallel": False,
        },
    ],
    "synthesis": None,
    "context_keys": ["default"],
    "rounds_count": 1,
}
