#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Prompt 模板 - 新闻分析专用

用于:
- 命名实体识别 (NER)
- 影响链分析
- 情绪分析
- 交易信号生成
"""

# ============================================================
#  新闻实体识别 Prompt
# ============================================================
NEWS_ENTITY_RECOGNITION = """
你是一个专业的金融新闻分析师。请分析以下新闻，识别其中提及的实体。

要求:
1. 识别所有公司/股票名称
2. 识别行业/概念标签
3. 判断每个实体的情绪倾向

新闻标题：{title}
新闻内容：{content}

请以 JSON 格式返回，格式如下：
{{
    "entities": [
        {{"text": "公司名", "type": "company", "sentiment": "positive/negative/neutral", "confidence": 0.9}},
        {{"text": "行业名", "type": "industry", "sentiment": "positive/negative/neutral", "confidence": 0.8}}
    ]
}}

直接返回 JSON，不要额外说明。
"""

# ============================================================
#  影响链分析 Prompt
# ============================================================
IMPACT_CHAIN_ANALYSIS = """
你是一个专业的产业链分析师。请分析以下新闻对产业链的影响。

要求:
1. 识别新闻涉及的行业
2. 分析产业链上下游关系
3. 判断影响方向 (正面/负面)
4. 列出可能受益/受损的股票

新闻标题：{title}
新闻内容：{content}

请以 JSON 格式返回，格式如下：
{{
    "primary_industry": "主要影响行业",
    "upstream": ["上游行业 1", "上游行业 2"],
    "downstream": ["下游行业 1", "下游行业 2"],
    "impact_direction": "positive/negative",
    "beneficiaries": ["受益股票 1", "受益股票 2"],
    "negatively_affected": ["受损股票 1", "受损股票 2"],
    "reasoning": "分析逻辑简述"
}}

直接返回 JSON，不要额外说明。
"""

# ============================================================
#  事件类型分类 Prompt
# ============================================================
EVENT_CLASSIFICATION = """
你是一个专业的事件检测器。请分类以下新闻属于哪种事件类型。

可选类型:
- earnings: 财报/业绩
- guidance: 业绩指引/展望
- ma: 并购重组
- product: 新产品/技术
- regulation: 监管政策
- executive: 高管变动
- lawsuit: 诉讼/调查
- analyst: 分析师评级
- market_move: 市场异动
- macro: 宏观经济
- geopolitical: 地缘政治
- other: 其他

新闻标题：{title}
新闻内容：{content}

请以 JSON 格式返回：
{{
    "event_type": "类型名称",
    "confidence": 0.9,
    "reasoning": "分类理由"
}}

直接返回 JSON，不要额外说明。
"""

# ============================================================
#  交易信号生成 Prompt
# ============================================================
TRADING_SIGNAL_GENERATION = """
你是一个专业的交易信号生成器。请根据以下新闻生成交易建议。

要求:
1. 判断方向 (买入/卖出/观望)
2. 评估信号强度 (1-5 分)
3. 给出目标价位和止损位
4. 评估风险

新闻标题：{title}
新闻内容：{content}

当前价格：{current_price}

请以 JSON 格式返回：
{{
    "direction": "buy/sell/hold",
    "confidence": 0.8,
    "signal_strength": 4,
    "target_price": 550.0,
    "stop_loss": 480.0,
    "time_horizon": "intraday/1-3days/1week",
    "risk_assessment": "风险评估简述",
    "reasoning": "推荐理由"
}}

直接返回 JSON，不要额外说明。
"""

# ============================================================
#  持续时间预测 Prompt
# ============================================================
DURATION_PREDICTION = """
你是一个专业的市场影响分析师。请预测以下新闻事件的市场影响持续时间。

要求:
1. 评估事件严重程度
2. 预测影响持续时间 (分钟)
3. 给出退出时机建议

新闻标题：{title}
新闻内容：{content}

事件类型：{event_type}
情绪评分：{sentiment_score} (-1 到 1)

请以 JSON 格式返回：
{{
    "severity": "critical/high/normal/low",
    "expected_duration_minutes": 120,
    "exit_time_suggestion": "10:30 前退出",
    "confidence": 0.75,
    "reasoning": "理由简述"
}}

直接返回 JSON，不要额外说明。
"""

# ============================================================
#  跨市场套利分析 Prompt
# ============================================================
CROSS_MARKET_ANALYSIS = """
你是一个专业的跨市场套利分析师。请分析以下新闻对美港两地的影响差异。

要求:
1. 判断哪个市场会先反应
2. 评估两个市场的预期涨幅差异
3. 给出套利建议

新闻标题：{title}
新闻内容：{content}

相关股票:
- 美股：{us_code}
- 港股：{hk_code}

请以 JSON 格式返回：
{{
    "lead_market": "US/HK",
    "us_expected_move_pct": 3.5,
    "hk_expected_move_pct": 2.8,
    "arbitrage_signal": "long_us_short_hk/long_hk_short_us/none",
    "confidence": 0.75,
    "reasoning": "分析理由"
}}

直接返回 JSON，不要额外说明。
"""
