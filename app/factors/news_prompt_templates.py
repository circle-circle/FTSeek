"""
FTSeek-US 新闻分析 LLM Prompt 模板

整合:
1. GeoEventPreFilter 地缘政治标记注入
2. 美股传导路径信息
3. 完整新闻分析框架

用法:
    from app.factors.news_prompt_templates import build_strategy_analysis_prompt
    prompt = build_strategy_analysis_prompt(
        title="Iran ceasefire deal reached",
        content="...",
        geo_scan={"severity": "moderate", "matched_patterns": ["ceasefire"], ...},
        watchlist_str="US.NVDA, US.AAPL, US.TSLA",
    )
"""

from app.factors.geo_pre_filter import GeoEventPreFilter


def build_geo_analysis_section(geo_scan: dict = None) -> str:
    """构建地缘政治事件分析段落，注入到主 prompt 中"""
    if not geo_scan or not geo_scan.get("is_geo_event"):
        return ""

    severity = geo_scan.get("severity", "none")
    matched = ", ".join(geo_scan.get("matched_patterns", []))
    flags = ", ".join(geo_scan.get("context_flags", []))
    paths = geo_scan.get("transmission_paths", [])

    lines = []
    lines.append("## ⚠️ 地缘政治事件检测 (预筛标记)")
    lines.append("")
    lines.append("预筛器已标记本新闻为地缘政治相关事件:")
    lines.append(f"- **严重程度**: {severity.upper()}")
    lines.append(f"- **匹配关键词**: {matched}")

    if flags:
        lines.append(f"- **上下文标记**: {flags}")
        lines.append("- **重要**: 预筛器只做关键词匹配，不理解语义。")
        lines.append("- 例如 \"ceasefire/peace deal\" 会被标记为 moderate，但实际是**利好**（降低不确定性、提振风险资产）。")
        lines.append("- **你必须基于完整新闻内容做出最终判断，不要受预筛标记误导。**")
    else:
        lines.append("- **重要**: 你必须基于完整新闻内容做出最终判断。")

    # 美股传导路径
    if paths:
        lines.append("")
        lines.append("## 📡 美股传导路径检测")
        lines.append("")
        lines.append("预筛器检测到以下美股传导路径:")
        
        from app.factors.geo_pre_filter import US_TRANSMISSION_PATHS
        for path_name in paths:
            path_info = US_TRANSMISSION_PATHS.get(path_name, {})
            lines.append(f"### {path_name}")
            if path_info.get("beneficiaries"):
                lines.append(f"- **受益方**: {', '.join(path_info['beneficiaries'][:8])}")
            if path_info.get("victims"):
                lines.append(f"- **受害方**: {', '.join(path_info['victims'][:8])}")
            if path_info.get("affects_all"):
                lines.append("- **全市场影响**: 是")
            if path_info.get("sector_impact"):
                for sector, impact in path_info["sector_impact"].items():
                    lines.append(f"- **{sector}**: {impact}")
        
        lines.append("")
        lines.append("以上传导路径仅供参考，你必须基于完整新闻内容、当前市场状况和 VIX 水平做出独立判断。")

    lines.append("")
    return "\n".join(lines)


def build_strategy_analysis_prompt(
    title: str,
    content: str,
    source: str = "",
    pub_time: str = "",
    geo_scan: dict = None,
    watchlist_str: str = "",
    market_data_str: str = "",
    event_name: str = "",
    event_sector: str = "",
    static_logic: str = "",
) -> str:
    """
    构建完整的 LLM 新闻分析 prompt
    
    关键: 如果 geo_scan 标记了地缘事件，会注入地缘分析段落
    """
    geo_section = build_geo_analysis_section(geo_scan)

    prompt = f"""# Role
你是 FTSeek-US 首席量化宏观分析师。你的任务是分析突发新闻，结合市场数据，生成可执行的交易策略。

{geo_section}

# Input
## 新闻
标题: {title}
内容: {content[:1500]}
来源: {source}
时间: {pub_time}

## 命中的事件类型
事件: {event_name}
板块: {event_sector}
静态逻辑: {static_logic}

## 相关股票池
{watchlist_str}

## 最新行情数据 (如可用)
{market_data_str}

# Analysis Logic (必须逐步执行)

## 第一步: 事件定性
- 这是宏观政策/地缘政治/行业周期/财报/并购/突发事件?
- 风险偏好方向: Risk-On (利好风险资产) 还是 Risk-Off (利好避险资产)?
- 情绪强度: 极强/强/中/弱?

{"- **地缘政治额外判断**:" if geo_scan and geo_scan.get("is_geo_event") else ""}
{"- 这是地缘冲突升级 (利空风险资产) 还是地缘缓和 (利好风险资产)?" if geo_scan and geo_scan.get("is_geo_event") else ""}
{"- 对相关资产类别 (原油/黄金/美元/股票) 的具体传导路径是什么?" if geo_scan and geo_scan.get("is_geo_event") else ""}

## 第二步: 美股标的映射
- 识别受此新闻直接影响的 US.XXXX 股票
- 判断影响方向: 利好 (BUY) / 利空 (SELL/SHORT) / 中性
- 评估影响持续时间和幅度

## 第三步: 二阶效应分析
- 直接影响 → 间接影响 → 市场情绪
- 例如: 中东冲突 → 油价上涨 → 航空股下跌 → 消费信心下降

## 第四步: 策略建议
- 交易方向: BUY / SELL / HOLD
- 置信度: 0.0-1.0
- 目标价格 (如适用)
- 止损建议
- 时间框架: 日内 / 1-3 天 / 1 周+

# Output Format (必须严格遵守 JSON)

```json
{{
    "event_classification": "...",
    "risk_bias": "Risk-On" or "Risk-Off",
    "sentiment_strength": "极强/强/中/弱",
    "affected_stocks": [
        {{"code": "US.XXXX", "direction": "BUY/SELL/SHORT", "confidence": 0.X, "reason": "..."}}
    ],
    "second_order_effects": ["...", "..."],
    "strategy": {{
        "action": "BUY/SELL/HOLD/SHORT",
        "confidence": 0.X,
        "timeframe": "intraday/swing/position",
        "stop_loss_pct": 0.XX,
        "take_profit_pct": 0.XX,
        "notes": "..."
    }}
}}
```

# Important Rules
1. 必须输出有效 JSON，不要输出额外的解释
2. 如果新闻不影响 watchlist 中的任何股票，confidence 设为 0
3. 不要编造数据或虚构不存在的新闻
4. 地缘事件中，区分"冲突升级"(利空)和"冲突缓和"(利好)
5. 考虑当前 VIX 水平和 SPY 趋势
"""
    return prompt
