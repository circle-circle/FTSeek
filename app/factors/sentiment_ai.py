"""
AI 增强情绪因子

使用 AI API 进行情绪分析，自动从新闻采集器获取新闻

P1 修复 (2026-04-11):
- 集成批量处理器，减少API调用次数
- 支持异步批量分析，降低延迟
- 提高缓存命中率
"""
import logging
from typing import Optional, Dict, List
from .base import BaseFactor, FactorResult

logger = logging.getLogger("ftseek.factors.sentiment_ai")
logger.propagate = False  # 防止日志重复


class AISentimentFactor(BaseFactor):
    """AI 情绪因子"""
    
    name = "sentiment_ai"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        self.auto_fetch_news = config.get('auto_fetch_news', True) if config else True
        self.lookback_hours = config.get('lookback_hours', 24) if config else 24
        self.max_news = config.get('max_news', 10) if config else 10
    
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """计算 AI 情绪因子"""
        # 1. 自动获取新闻 (如果启用)
        if self.auto_fetch_news and not data.get('news'):
            try:
                from app.factors.news_integration import get_news_sentiment_analyzer
                analyzer = get_news_sentiment_analyzer()
                news_result = analyzer.analyze_stock(
                    code=code,
                    lookback_hours=self.lookback_hours,
                    max_news=self.max_news
                )
                
                # 使用分析结果
                if news_result['news_count'] > 0:
                    logger.info(f"自动获取新闻：{code} - {news_result['news_count']} 条")
                    return FactorResult(
                        code=code,
                        factor_name='sentiment_ai',
                        value=news_result['sentiment_score'],
                        timestamp=news_result['analyzed_at'],
                        metadata={
                            'news_count': news_result['news_count'],
                            'positive_count': news_result['positive_count'],
                            'negative_count': news_result['negative_count'],
                            'neutral_count': news_result['neutral_count'],
                            'ai_provider': news_result['ai_provider'],
                            'cache_hit': news_result['cache_hit'],
                            'auto_fetched': True
                        }
                    )
            except Exception as e:
                logger.warning(f"自动获取新闻失败：{e}，使用备用方案")
        
        # 2. 使用传入的新闻数据 (备用方案)
        news_list = data.get('news', [])
        social_posts = data.get('social', [])
        
        if not news_list and not social_posts:
            logger.debug(f"无新闻数据：{code}")
            return FactorResult(
                code=code,
                factor_name='sentiment_ai',
                value=0.0,
                timestamp=data.get('timestamp', ''),
                metadata={'news_count': 0, 'social_count': 0, 'auto_fetched': False}
            )
        
        # 3. AI 情绪分析 (P1 修复: 使用批量处理器)
        from app.factors.news_batch_processor import news_batch_processor, NewsItem
        
        news_scores = []
        for news in news_list[:self.max_news]:
            text = f"{news.get('title', '')} {news.get('content', '')}"
            
            # P1 修复: 优先使用批量处理器
            score = news_batch_processor.analyze_single(
                code=code,
                title=news.get('title', ''),
                content=news.get('content', '')
            )
            news_scores.append(score)
        
        # 社交情绪 (保持原有逻辑)
        social_scores = []
        for post in social_posts[:self.max_news * 2]:
            # 社交内容也使用批量处理器
            score = news_batch_processor.analyze_single(
                code=code,
                title="Social Post",
                content=post
            )
            social_scores.append(score)
        
        # 综合评分 (新闻 60% + 社交 40%)
        news_avg = sum(news_scores) / len(news_scores) if news_scores else 0.0
        social_avg = sum(social_scores) / len(social_scores) if social_scores else 0.0
        
        score = news_avg * 0.6 + social_avg * 0.4
        
        return FactorResult(
            code=code,
            factor_name='sentiment_ai',
            value=score,
            timestamp=data.get('timestamp', ''),
            metadata={
                'news_sentiment': news_avg,
                'social_sentiment': social_avg,
                'news_count': len(news_scores),
                'social_count': len(social_scores),
                'ai_provider': 'bailian',
                'batch_processor_stats': news_batch_processor.get_stats(),
                'auto_fetched': False
            }
        )
