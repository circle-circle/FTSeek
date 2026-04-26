"""
金融新闻推送系统 (Phase 3 增强版)

基于 Situation-aggregation-monitoring 项目改造
保留多源新闻采集能力，新增钉钉推送、规则过滤、频率控制

Phase 3 新增功能:
- 动态缓存 TTL (基于市场波动率)
- 情绪得分衰减
- 突发新闻快速通道

模块:
- collector: 多源新闻采集器 (支持动态 TTL)
- rule_engine: 推送规则引擎
- rate_limiter: 频率控制器
- qwen_analyzer: Qwen 大模型分析
- notifier: 钉钉推送器
- service: 推送服务 (整合所有模块)
- breaking_news: 突发新闻快速通道

用法:
  from app.news_push import get_service

  service = get_service()
  service.run_once()  # 执行一次
  # 或
  service.start(interval=60)  # 持续运行
"""

from app.news_push.collector import get_collector, NewsItem, NewsCollector
from app.news_push.collector import get_news_collector  # 别名
from app.news_push.rule_engine import get_rule_engine, RuleEngine
from app.news_push.rate_limiter import get_rate_limiter, RateLimiter
from app.news_push.qwen_analyzer import get_qwen_analyzer, QwenAnalyzer, AnalysisResult
from app.news_push.notifier import get_notifier, NewsNotifier
from app.news_push.service import get_service, NewsPushService
# Phase 3: 突发新闻快速通道
from app.news_push.breaking_news import (
    get_breaking_news_detector,
    get_fast_track_processor,
    BreakingNewsDetector,
    FastTrackProcessor,
    BreakingNewsAlert,
)
# Phase 4: 新闻汇总与盘中更新 (2026-04-13 新增)
from app.news_push.news_summarizer import get_summarizer, NewsSummarizer
from app.news_push.intraday_updater import get_intraday_updater, IntradayNewsUpdater

__all__ = [
    # 采集
    'get_collector', 'NewsItem', 'NewsCollector',
    'get_news_collector',

    # 规则
    'get_rule_engine', 'RuleEngine',

    # 限流
    'get_rate_limiter', 'RateLimiter',

    # 分析
    'get_qwen_analyzer', 'QwenAnalyzer', 'AnalysisResult',

    # 推送
    'get_notifier', 'NewsNotifier',

    # 服务
    'get_service', 'NewsPushService',

    # Phase 3 新增
    'get_breaking_news_detector', 'BreakingNewsDetector',
    'get_fast_track_processor', 'FastTrackProcessor',
    'BreakingNewsAlert',
    
    # Phase 4 新增 (2026-04-13)
    'get_summarizer', 'NewsSummarizer',
    'get_intraday_updater', 'IntradayNewsUpdater',
]

__version__ = '1.1.0'  # Phase 3 版本
