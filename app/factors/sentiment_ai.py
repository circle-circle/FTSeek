"""
AI 增强情绪因子

使用 AI API 进行情绪分析，自动从新闻采集器获取新闻
"""
import logging
from typing import Optional, Dict, List
from .base import BaseFactor, FactorResult

logger = logging.getLogger("ftseek.factors.sentiment_ai")


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
        
        # 3. AI 情绪分析
        from app.ai import AIService
        ai_service = AIService()
        
        news_scores = []
        for news in news_list[:self.max_news]:
            text = f"{news.get('title', '')} {news.get('content', '')}"
            
            # 检查缓存
            cache_key = f"sentiment_{hash(text)}"
            if hasattr(self, 'cache') and cache_key in self.cache:
                score = self.cache[cache_key]
            else:
                response = ai_service.analyze_sentiment(text)
                if response.success:
                    score = response.data.get('sentiment_score', 0.0)
                    if hasattr(self, 'cache'):
                        self.cache[cache_key] = score
                else:
                    score = 0.0
            
            news_scores.append(score)
        
        # 社交情绪
        social_scores = []
        for post in social_posts[:self.max_news * 2]:
            cache_key = f"sentiment_{hash(post)}"
            if hasattr(self, 'cache') and cache_key in self.cache:
                score = self.cache[cache_key]
            else:
                response = ai_service.analyze_sentiment(post)
                if response.success:
                    score = response.data.get('sentiment_score', 0.0)
                    if hasattr(self, 'cache'):
                        self.cache[cache_key] = score
                else:
                    score = 0.0
            
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
                'cache_size': len(getattr(self, 'cache', {})),
                'auto_fetched': False
            }
        )
