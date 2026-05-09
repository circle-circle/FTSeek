"""
金融新闻采集模块 (精简版)

保留核心采集能力，已移除死模块 (service/rule_engine/rate_limiter/notifier/
qwen_analyzer/hourly_push/breaking_news/intraday_updater)。

活跃模块:
- collector: 多源新闻采集器 (NewsItem dataclass + RSS 采集)
- config: RSS 源配置 + 股票别名映射
- news_summarizer: 新闻摘要生成 (供 AI 市场上下文使用)

注意: 通知功能已迁移到 app.notifier.dingtalk
"""

from app.news_push.collector import get_collector, NewsItem, NewsCollector
from app.news_push.collector import get_news_collector  # 别名
from app.news_push.news_summarizer import get_summarizer, NewsSummarizer

__all__ = [
    'get_collector', 'NewsItem', 'NewsCollector',
    'get_news_collector',
    'get_summarizer', 'NewsSummarizer',
]

__version__ = '2.0.0'  # 精简版
