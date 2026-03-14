"""
金融新闻推送系统

基于 Situation-aggregation-monitoring 项目改造
保留多源新闻采集能力，新增钉钉推送、规则过滤、频率控制

模块:
- collector: 多源新闻采集器
- rule_engine: 推送规则引擎
- rate_limiter: 频率控制器
- qwen_analyzer: Qwen 大模型分析
- notifier: 钉钉推送器
- service: 推送服务 (整合所有模块)

用法:
  from app.news_push import get_service
  
  service = get_service()
  service.run_once()  # 执行一次
  # 或
  service.start(interval=60)  # 持续运行
"""

from app.news_push.collector import get_collector, NewsItem, NewsCollector
from app.news_push.rule_engine import get_rule_engine, RuleEngine
from app.news_push.rate_limiter import get_rate_limiter, RateLimiter
from app.news_push.qwen_analyzer import get_qwen_analyzer, QwenAnalyzer, AnalysisResult
from app.news_push.notifier import get_notifier, NewsNotifier
from app.news_push.service import get_service, NewsPushService

__all__ = [
    # 采集
    'get_collector', 'NewsItem', 'NewsCollector',
    
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
]

__version__ = '1.0.0'
