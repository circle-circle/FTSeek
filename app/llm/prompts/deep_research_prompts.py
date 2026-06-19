"""Deep Research Prompt 模板集

对标 MU_research-deep.md / HOOD-2026-06-11-deep.md 的论证深度和结构化输出。

包含:
1. DEBATE_SYSTEM / DEBATE_USER — 多空辩论 prompt（4 分析师报告 → 多头/空头论点 + 裁决）
2. SYNTHESIS_SYSTEM / SYNTHESIS_USER — 综合决策 prompt（辩论结果 → 执行摘要 + 建仓方案 + 风控）
"""

# ═══════════════════════════════════════════════════════════
# Stage 7: 多空辩论
# ═══════════════════════════════════════════════════════════

DEBATE_SYSTEM = """你是一位资深多空辩论主持人，负责从4个专业分析师的报告中提炼最强多头和空头论点。

【原则】
1. 每条论点必须具体、有数据支撑，标注证据强度(HIGH/MEDIUM/LOW)和来源类型
2. 多头至少3条，空头至少3条
3. 裁决必须基于证据权重，不是折中
4. 明确指出多空最大分歧点
5. 从空头论点中提取值得采纳的风险警示
6. 所有基本面数据来自真实API（Finnhub），已标注来源和时间。不要生成"数据可能过时"等免责废话
7. 如果某维度数据缺失，直接说"该维度无数据"，不要编造或猜测

【输出格式】严格JSON，不要markdown包裹"""

DEBATE_USER = """## 标的: {symbol} · 当前价: ${current_price}
⚠️ 方向未预设——请基于4个分析师的证据独立判断做多/做空/观望。

## 技术面分析报告
{technical_report}

## 基本面分析报告
{fundamental_report}

## 新闻分析报告
{news_report}

## 情绪分析报告
{sentiment_report}

---

请输出以下JSON结构:
{{
    "bull_case": [
        {{"thesis": "论点(具体有数据)", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证(数字+日期)", "source_type": "technical/fundamental/news/sentiment"}},
        ...至少3条
    ],
    "bear_case": [
        {{"thesis": "论点(具体有数据)", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证(数字+日期)", "source_type": "technical/fundamental/news/sentiment"}},
        ...至少3条
    ],
    "verdict": {{
        "recommended_direction": "LONG/SHORT/NEUTRAL",
        "direction_confidence": "HIGH/MEDIUM/LOW",
        "key_disagreement": "多空最大分歧点(1句话)",
        "adopted_from_bear": "从空头论点中采纳的风险警示",
        "strongest_bull": "最强多头论点(1句话引用)",
        "strongest_bear": "最强空头论点(1句话引用)"
    }},
    "causal_chain": "3步以上因果传导链(用→连接)",
    "historical_reference": "近期类似场景描述+结果"
}}"""


# ═══════════════════════════════════════════════════════════
# Stage 8: 综合决策
# ═══════════════════════════════════════════════════════════

SYNTHESIS_SYSTEM = """你是一位严谨的投资组合经理，负责将多空辩论结果转化为可执行的投资决策。

【原则】
1. 执行摘要必须1句话给出明确结论和核心动作
2. 分批建仓方案必须有具体的价格区间和触发条件
3. 风控分三层(激进/中性/保守)，每层有可观测的量化触发条件
4. 关键风险触发表必须可直接执行（触发→动作）
5. 价格关键位必须引用实际数据（均线/支撑阻力/布林带）
6. 数据完整性必须诚实标注，缺失数据降低确信度
7. 所有基本面数据来自真实API，已标注来源和时间。禁止生成"基本面基于模型训练知识""数据可能过时"等模板废话
8. impact_note 只描述实际缺失的维度，不要写"综合来看数据不完整"等空话

【输出格式】严格JSON，不要markdown包裹"""

SYNTHESIS_USER = """## 标的: {symbol} · 当前价: ${current_price}
## 辩论推荐方向: {debate_direction}
## 入场区间: ${entry_low} - ${entry_high} · 止损: ${stop_loss} · 止盈: ${take_profit}
## 初始置信度: {confidence}%

## 多空辩论结果
{debate_result}

## 技术面指标汇总
{indicators_summary}

## 基本面快照
{fundamental_summary}

## 新闻催化
{news_summary}

## 情绪数据
{sentiment_summary}

## 数据质量
- 技术面: {tech_quality}
- 基本面: {fund_quality}
- 新闻: {news_quality}
- 情绪: {sentiment_quality}

## 该标的历史交易表现
{trade_history}

---

⚠️ action 判定规则（严格遵守）:
- BUY: 辩论推荐LONG + 确信度HIGH/MEDIUM + 数据≥3维完整 → 做多建仓
- SELL: 辩论推荐SHORT + 确信度HIGH/MEDIUM + 数据≥3维完整 → 做空
- HOLD: 已有持仓 + 方向不明确但无触发离场条件 → 持有观望
- AVOID: 辩论方向NEUTRAL 或 确信度LOW 或 数据≤2维完整 或 历史胜率<30% → 不交易

conviction 校准规则:
- 辩论 HIGH + 数据4/4 + 历史胜率>50% → 70-90
- 辩论 MEDIUM + 数据3/4 → 50-70
- 辩论 LOW 或 数据≤2/4 或 历史胜率<30% → 20-45
- 矛盾信号(如技术做多+情绪做空) → conviction 必须<60

请输出以下JSON结构:
{{
    "verdict": {{
        "action": "BUY/HOLD/SELL/AVOID",
        "action_detail": "具体操作描述(如：分批建仓，事件前不满仓)",
        "conviction": 0-100,
        "data_quality_score": "X/Y (完整维度/总维度)"
    }},
    "executive_summary": "一句话结论+核心动作(如：质地一档但贵在择时——分批建仓1/3，留弹药待财报确认)",
    "build_plan": {{
        "batch_1": {{"pct": "1/3", "zone": "$XXX-$XXX", "condition": "触发条件"}},
        "batch_2": {{"pct": "1/3", "zone": "$XXX-$XXX", "condition": "触发条件"}},
        "batch_3": {{"pct": "1/3", "zone": "$XXX-$XXX", "condition": "触发条件"}},
        "event_buffer": "重大事件前的仓位策略"
    }},
    "risk_tiers": {{
        "aggressive": {{"action": "操作", "trigger": "可观测触发条件", "risk": "主要风险"}},
        "moderate": {{"action": "操作", "trigger": "可观测触发条件", "risk": "主要风险"}},
        "conservative": {{"action": "操作", "trigger": "可观测触发条件", "risk": "主要风险"}}
    }},
    "risk_triggers": [
        {{"condition": "具体触发条件", "action": "对应动作"}},
        ...至少3个
    ],
    "key_levels": {{
        "current": 价格,
        "hard_stop": 硬止损价,
        "resistance_1": 第一阻力,
        "resistance_2": 第二阻力,
        "support_1": 第一支撑,
        "support_2": 第二支撑,
        "extreme_down": 极端下行情景
    }},
    "data_quality": {{
        "technical": "完整/降级/缺失",
        "fundamental": "完整/降级/缺失",
        "news": "完整/降级/缺失",
        "sentiment": "完整/降级/缺失",
        "overall": "X项完整/Y项降级/Z项缺失",
        "impact_note": "数据缺失对结论的影响"
    }}
}}"""


# ═══════════════════════════════════════════════════════════
# 格式化辅助函数
# ═══════════════════════════════════════════════════════════

def format_analyst_report(name: str, data: dict) -> str:
    """将分析师结果格式化为 prompt 输入文本"""
    if not data or data.get("status") == "missing":
        return f"({name}数据缺失)"

    lines = [f"【{name}】状态: {data.get('status', 'unknown')}"]

    # 技术面
    if name == "技术面":
        verdict = data.get("overall_verdict", {})
        if verdict:
            lines.append(f"综合判断: {verdict.get('summary', 'N/A')}")
        trend = data.get("trend", {})
        if trend:
            lines.append(f"趋势: {trend.get('alignment', 'N/A')}")
        momentum = data.get("momentum", {})
        if momentum:
            lines.append(f"动量: {momentum.get('summary', 'N/A')}")
        volatility = data.get("volatility", {})
        if volatility:
            lines.append(f"波动: {volatility.get('summary', 'N/A')}")
        volume = data.get("volume", {})
        if volume:
            lines.append(f"量价: {volume.get('summary', 'N/A')}")
        kl = data.get("key_levels", {})
        if kl:
            lines.append(f"关键价位: 52w高{kl.get('week52_high')} / 52w低{kl.get('week52_low')} / swing高{kl.get('swing_high_20d')} / swing低{kl.get('swing_low_20d')}")

    # 基本面
    elif name == "基本面":
        fin = data.get("financials", {})
        if fin:
            parts = []
            if fin.get("market_cap") and fin["market_cap"] != "N/A":
                parts.append(f"市值={fin['market_cap']}")
            if fin.get("pe_ttm") and fin["pe_ttm"] != "N/A":
                parts.append(f"PE(TTM)={fin['pe_ttm']}")
            if fin.get("forward_pe") and fin["forward_pe"] != "N/A":
                parts.append(f"Forward PE={fin['forward_pe']}")
            if fin.get("pb") and fin["pb"] != "N/A":
                parts.append(f"PB={fin['pb']}")
            if fin.get("roe") and fin["roe"] != "N/A":
                parts.append(f"ROE={fin['roe']}")
            if fin.get("eps_ttm") and fin["eps_ttm"] != "N/A":
                parts.append(f"EPS(TTM)={fin['eps_ttm']}")
            if fin.get("revenue_yoy") and fin["revenue_yoy"] != "N/A":
                parts.append(f"营收YoY={fin['revenue_yoy']}")
            if fin.get("gross_margin") and fin["gross_margin"] != "N/A":
                parts.append(f"毛利率={fin['gross_margin']}")
            if fin.get("net_margin") and fin["net_margin"] != "N/A":
                parts.append(f"净利率={fin['net_margin']}")
            if fin.get("beta") and fin["beta"] != "N/A":
                parts.append(f"Beta={fin['beta']}")
            if parts:
                lines.append(f"财务: {' | '.join(parts)}")
            # 数据时效性
            if fin.get("fetched_at"):
                lines.append(f"数据获取时间: {fin['fetched_at']} (来源: {fin.get('source', 'API')})")
        val = data.get("valuation", {})
        if val and val != fin:
            val_parts = []
            if val.get("market_cap") and val["market_cap"] != "N/A":
                val_parts.append(f"市值={val['market_cap']}")
            if val.get("pe_ttm") and val["pe_ttm"] != "N/A":
                val_parts.append(f"PE={val['pe_ttm']}")
            if val.get("forward_pe") and val["forward_pe"] != "N/A":
                val_parts.append(f"Forward PE={val['forward_pe']}")
            if val.get("pb") and val["pb"] != "N/A":
                val_parts.append(f"PB={val['pb']}")
            if val.get("beta") and val["beta"] != "N/A":
                val_parts.append(f"Beta={val['beta']}")
            if val_parts:
                lines.append(f"估值: {' | '.join(val_parts)}")
        cluster = data.get("analyst_cluster", {})
        if cluster:
            total = cluster.get("total_analysts", 0)
            if total > 0:
                lines.append(f"分析师评级: 买入{cluster.get('buy_pct', 'N/A')} 持有{cluster.get('hold_pct', 'N/A')} 卖出{cluster.get('sell_pct', 'N/A')} ({total}家, 趋势={cluster.get('trend', 'stable')})")
            elif cluster.get("cluster"):
                # 旧格式兼容 (target price cluster from FMP)
                c = cluster["cluster"]
                lines.append(f"目标价簇: ${c['low']}-${c['high']} (中位${c['median']}, {c['count']}家)")
                lines.append(f"当前位置: {cluster.get('position_in_cluster', 'N/A')}, 上行{cluster.get('upside_pct', 'N/A')}%")
        lines.append(f"结论: {data.get('conclusion', 'N/A')}")

    # 新闻
    elif name == "新闻":
        news = data.get("news_timeline", [])
        if news:
            lines.append(f"近期新闻({len(news)}条):")
            for n in news[:5]:
                lines.append(f"  · [{n.get('sentiment', 'neutral')}] {n.get('headline', '')[:80]}")
        catalysts = data.get("catalysts", [])
        if catalysts:
            lines.append("催化事件:")
            for c in catalysts:
                lines.append(f"  · {c.get('event', '')} ({c.get('date', '?')}) [{c.get('importance', 'MEDIUM')}]")
        macro = data.get("macro_calendar", [])
        if macro:
            lines.append("宏观日历:")
            for m in macro[:3]:
                lines.append(f"  · {m.get('date', '')}: {m.get('event', '')} [{m.get('impact', 'medium')}]")
        sector = data.get("sector_context", "")
        if sector:
            lines.append(f"板块: {sector}")
        lines.append(f"结论: {data.get('conclusion', 'N/A')}")

    # 情绪
    elif name == "情绪":
        st = data.get("stocktwits", {})
        if st:
            lines.append(f"StockTwits: 多{st.get('bullish', 0)}/空{st.get('bearish', 0)}, 比例{st.get('bull_bear_ratio', 'N/A')}, 关注{st.get('watchlist_count', 0)}人")
        reddit = data.get("reddit", {})
        if reddit:
            lines.append(f"Reddit: {reddit.get('post_count', 0)}帖")
            for t in reddit.get("top_topics", [])[:2]:
                lines.append(f"  · {t[:60]}")
        lines.append(f"方向: {data.get('direction', '中性')} · 拥挤度: {data.get('crowding', '未知')}")
        lines.append(f"结论: {data.get('conclusion', 'N/A')}")

    return "\n".join(lines)


def format_indicators_summary(tech_data: dict) -> str:
    """格式化指标摘要文本"""
    if not tech_data or tech_data.get("status") == "missing":
        return "(指标数据缺失)"

    indicators = tech_data.get("indicators", {})
    trend = tech_data.get("trend", {})
    momentum = tech_data.get("momentum", {})
    volatility = tech_data.get("volatility", {})
    volume = tech_data.get("volume", {})

    lines = []
    if trend:
        lines.append(f"趋势: {trend.get('alignment', 'N/A')}")
        lines.append(f"  10EMA={indicators.get('close_10_ema', 'N/A')} / 50SMA={indicators.get('close_50_sma', 'N/A')} / 200SMA={indicators.get('close_200_sma', 'N/A')}")
    if momentum:
        lines.append(f"动量: {momentum.get('summary', 'N/A')}")
    if volatility:
        lines.append(f"波动: ATR={indicators.get('atr', 'N/A')}(≈{volatility.get('atr_pct', 'N/A')}%/日) · {volatility.get('vol_level', '')}波动")
        lines.append(f"  布林: 中{indicators.get('boll_mid', 'N/A')}/上{indicators.get('boll_upper', 'N/A')}/下{indicators.get('boll_lower', 'N/A')}")
    if volume:
        lines.append(f"量价: {volume.get('summary', 'N/A')}")

    kl = tech_data.get("key_levels", {})
    if kl:
        lines.append(f"关键价位: 52w高{kl.get('week52_high')} / 52w低{kl.get('week52_low')}")
        lines.append(f"  swing高{kl.get('swing_high_20d')} / swing低{kl.get('swing_low_20d')} / YTD {kl.get('ytd_change', 'N/A')}%")

    return "\n".join(lines)


__all__ = [
    "DEBATE_SYSTEM", "DEBATE_USER",
    "SYNTHESIS_SYSTEM", "SYNTHESIS_USER",
    "format_analyst_report",
    "format_indicators_summary",
]
