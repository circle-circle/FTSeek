"""一手信息源 — 港交所披露易 + 证监会官网

设计: 继承 app.news_v2.sources.base.SourceAdapter
自动接入 NewsPipelineV2 质量门禁 (去重+相关性+重要性)
"""
