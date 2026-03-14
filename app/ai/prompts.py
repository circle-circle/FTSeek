"""
AI 提示词工程

金融分析领域的优化提示词模板
"""

# 情绪分析提示词
SENTIMENT_ANALYSIS = """
分析以下文本的情绪倾向，返回 -1 到 1 之间的分数：
- 1: 非常正面
- 0: 中性  
- -1: 非常负面

只返回数字，不要其他内容。

文本：{text}

分数："""

# 市场预测提示词
MARKET_PREDICTION = """
基于以下信息预测股票短期走势 (1-5 个交易日)：

股票代码：{code}

技术指标:
- RSI: {rsi}
- MACD: {macd}
- 均线趋势：{ma_trend}
- 布林带位置：{bb_position}

资金流:
- MFI: {mfi}
- 趋势：{flow_trend}

情绪评分：{sentiment}

请返回 JSON 格式：
{{
  "direction": "上涨/下跌/震荡",
  "confidence": 0-100,
  "reason": "50 字以内理由"
}}"""

# 因子提取提示词
FEATURE_EXTRACTION = """
从以下文本中提取关键金融因子：

文本：{text}

提取：
1. 利好因素 (最多 3 个)
2. 利空因素 (最多 3 个)
3. 不确定性因素

格式：
利好：[因素 1, 因素 2, ...]
利空：[因素 1, 因素 2, ...]
不确定：[因素 1, ...]"""

# 风险评估提示词
RISK_ASSESSMENT = """
评估以下投资标的的风险等级：

标的：{code}
当前价格：{price}
波动率：{volatility}
基本面：{fundamentals}

风险等级：低/中/高
主要风险点：[列出 3 个]
风控建议：[1-2 条]"""

# 报告生成提示词
REPORT_GENERATION = """
基于以下交易数据生成日报：

日期：{date}
交易次数：{trade_count}
胜率：{win_rate}
总收益：{total_return}
最大回撤：{max_drawdown}

包含：
1. 交易总结 (100 字)
2. 亮点 (1-2 个)
3. 改进建议 (1-2 条)"""
