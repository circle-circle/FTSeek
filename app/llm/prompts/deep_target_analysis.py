"""深度目标分析 Prompt 模板 (v3)

用途：为作战计划中每个 target 生成 MU_research-deep.md 级别的专业多空辩论分析。

包含三套模板：
1. SYSTEM_PROMPT          — 深度分析师角色定义（多空辩论 + 因果链 + 风控分层 + 数据质量追踪）
2. USER_PROMPT_TEMPLATE   — 标的级 user prompt（占位符通过 .format() 填充）
3. FUNDAMENTAL_SEARCH_PROMPT — (已废弃，保留兼容) 基本面检索 prompt

v3 改进 (2026-06-17):
  - 删除"基于模型知识标注"指令 — 数据全部来自真实 API，不再由 LLM 编造
  - 所有基本面数据标注数据源 + 获取时间 (fetched_at)
  - 禁止 LLM 在分析中生成模板式免责废话
"""

# ─── 系统角色 ───
SYSTEM_PROMPT = """你是一位专业的美股投资研究分析师，擅长多空辩论和结构化投资论证。
你的分析必须遵循以下原则：

【结构原则】
1. 决策区优先：先给出执行摘要（1句话结论+核心动作），再展开论证过程
2. 多空对等：多头至少3条论点，空头至少2条论点，每条必须标注：
   - 强度(HIGH/MEDIUM/LOW)
   - 证据来源类型(technical/fundamental/news/sentiment/macro)
   - 具体数据引证（数字+日期+来源，不接受无数据的观点）

【分析原则】
3. 因果链至少3层传导（宏观→板块→个股 或 事件→估值→价格）
4. 包含历史参照（近期类似形态/场景的实际表现）
5. 风控分三层级（激进/中性/保守），每层有明确可观测的触发条件
6. 可执行的分步建仓计划（底仓→加仓→全仓的条件和时机）

【数据质量原则】
7. 所有基本面数据均来自真实API（Finnhub），已标注数据源和获取时间
8. 你的任务是基于已提供的数据分析，不要生成"数据可能过时"等模板式免责废话
9. 如果某维度数据缺失（标注❌），直接说明"该维度无数据"并量化对结论的影响
10. 结论确信度必须与数据完整性挂钩——数据缺失越多，确信度越低
"""


# ─── User prompt 模板（用 .format() 填充） ───
# 注意：内层 JSON 的 { } 已转义为 {{ }}，避免 .format 报错
USER_PROMPT_TEMPLATE = """
请对以下美股标的进行深度多空辩论分析：

## 标的信息
- 代码: {symbol}
- 当前方向建议: {direction}
- 当前价格: ${current_price}
- 入场区间: ${entry_low} - ${entry_high}
- 止损: ${stop_loss} | 止盈: ${take_profit}
- 初始置信度: {confidence}%

## 技术面数据
{technicals_text}

## 因子验证结果
{factor_validation_text}

## 基本面/新闻信息
{fundamental_text}

## 宏观环境
{macro_text}

## 持仓状态
{position_status}

---

请输出严格的JSON格式（不要markdown包裹），结构如下：
{{
    "executive_summary": "一句话结论+核心动作（如：质地一档但贵在择时——分批建仓1/3，留弹药待财报确认）",
    "data_quality": {{
        "technical": "完整/降级/缺失",
        "fundamental": "完整/降级/缺失",
        "news": "完整/降级/缺失",
        "macro": "完整/降级/缺失",
        "overall_completeness": "8项完整/3项降级/1项缺失 格式",
        "impact_note": "数据缺失对结论的影响说明"
    }},
    "bull_case": [
        {{"thesis": "论点描述(具体、有数据)", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证", "source_type": "technical/fundamental/news/sentiment/macro"}}
    ],
    "bear_case": [
        {{"thesis": "论点描述(具体、有数据)", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证", "source_type": "technical/fundamental/news/sentiment/macro"}}
    ],
    "verdict": {{
        "direction_confidence": "HIGH/MEDIUM/LOW",
        "key_disagreement": "多空最大分歧点",
        "adopted_from_bear": "从空头论点中采纳的风险警示",
        "data_adjusted": "是否因数据缺失而调低确信度"
    }},
    "causal_chain": "3步以上因果传导链(用→连接)",
    "historical_reference": "近期类似场景描述+结果",
    "fundamental_snapshot": {{
        "revenue_growth": "同比增长%或N/A",
        "pe_forward": "前瞻PE或N/A",
        "analyst_consensus": "买入/持有/卖出 + 目标价区间 或N/A",
        "next_catalyst": "下一个催化事件及日期 或N/A"
    }},
    "risk_tiers": {{
        "aggressive": {{"action": "激进操作", "trigger": "触发条件", "risk": "主要风险"}},
        "moderate": {{"action": "标准操作", "trigger": "触发条件", "risk": "主要风险"}},
        "conservative": {{"action": "保守操作", "trigger": "触发条件", "risk": "主要风险"}}
    }},
    "execution_plan": {{
        "steps": ["Step 1: ...", "Step 2: ...", "Step 3: ..."],
        "event_buffer": "重大事件前的仓位策略",
        "invalidation": "使整个交易逻辑失效的条件"
    }}
}}
"""


# ─── 基本面 WebSearch 模板 (已废弃 2026-06-17) ───
# 旧版: 通过 call_llm(feature="web_search_fundamental") 让 LLM 编基本面数据
# 新版: 直接调用 Finnhub REST API (见 deep_analysis_engine._fetch_real_fundamental)
# 保留此变量仅为兼容 import，不再被调用
FUNDAMENTAL_SEARCH_PROMPT = "(已废弃 — 使用 _fetch_real_fundamental 替代)"


__all__ = [
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "FUNDAMENTAL_SEARCH_PROMPT",
]
