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

# ─── 动态校准块 ───
def _calibration_block() -> str:
    """动态计算校准数据行 (2026-08-25, DEB-20260825 第11轮).

    根因: 原 SYSTEM_PROMPT 硬编码 "T+1 命中率 41%(n=203, 截至2026-08-21)" —
    校准数字不刷新会从"诚实校准"退化为"过时误导"(用户要求链路无误导).
    口径: 近30日已结算交易的费用后净期望 (net = pnl_net 优先, 否则 realized_pnl-fees_est;
    与 daily_loss_halt_calibration.py / DEB 第4轮费用审计金标准一致), 多空分列对称呈现.
    查询失败或无样本 → 返回中性表述 (不硬编码任何具体历史数字).
    """
    try:
        import sqlite3 as _sq
        from datetime import datetime as _dt, timedelta as _td
        from pathlib import Path as _Path
        _db = _Path(__file__).resolve().parent.parent.parent.parent / "data" / "ftseek_us.db"
        _cut = (_dt.now() - _td(days=30)).strftime("%Y-%m-%d")
        _conn = _sq.connect(str(_db), timeout=2)
        _rows = _conn.execute(
            "SELECT UPPER(direction), COUNT(*), "
            "COALESCE(SUM(COALESCE(pnl_net, realized_pnl - COALESCE(fees_est,0))),0) "
            "FROM trade_records WHERE exit_time IS NOT NULL AND trade_date >= ? "
            "AND strategy_id NOT IN ('manual','manual_sync','reconciler','futu_sync','broker_reconcile') "
            "GROUP BY UPPER(direction)",
            (_cut,),
        ).fetchall()
        _conn.close()
        _by_dir = {str(d): (int(n), float(p)) for d, n, p in _rows if d}
        _total_n = sum(n for n, _ in _by_dir.values())
        if not _total_n:
            return ("近期无已结算样本 — 按掷硬币基准(50%)校准确信度, 不允许因叙事生动性上调")
        _parts = []
        for _d in sorted(_by_dir):
            _n, _p = _by_dir[_d]
            _parts.append(f"{_d}: {_n}笔 净{_p:+.0f}$ 均{_p / _n:+.2f}$/笔")
        return (f"近30日已结算{_total_n}笔费用后实绩 — {'; '.join(_parts)}。"
                f"这是唯一真实的盈亏度量(胜率不是分水岭)")
    except Exception:
        return ("实时校准统计暂不可用 — 按掷硬币基准(50%)校准确信度, "
                "不允许因叙事生动性上调")


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

【自我认知原则 (动态校准, 2026-08-25 改造; 原"截至2026-08-21"硬编码已删除 —
 过时的校准数字比没有更误导; DEB-20260822 §九 → DEB-20260825 第11轮)】
11. 校准: 你的方向判断历史命中率约50%(≈掷硬币, 干净窗口 n=321 实测)。
    实时盈亏校准: __DYNAMIC_CALIBRATION__。
    direction_confidence 必须当作校准概率使用, 数据缺失越多越向中性收敛,
    不允许因叙事生动性上调。
12. 对称: bear_case 与 bull_case 的证据强度门槛必须一致——每条空头论点须满足与
    多头论点完全相同的数据引证标准, 反之亦然 (多空对称, 无方向偏见)。
13. 差异化: 若你的结论与上述历史模式一致(如再次给出弱证据的日级方向判断),
    必须在 verdict.key_disagreement 中说明本次与历史失败模式的具体差异, 否则降级确信度。
"""

# 组装: 占位符在模块加载时替换为动态校准数据 (查询失败有中性兜底, 不阻断导入)
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("__DYNAMIC_CALIBRATION__", _calibration_block())


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
