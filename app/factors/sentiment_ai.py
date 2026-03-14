"""
AI 增强情绪因子

使用 AI API 进行情绪分析，替代简单词典法
"""
import logging
from typing import Optional, Dict, List
from .base import BaseFactor, FactorResult
from app.ai.base import AIService

logger = logging.getLogger("ftseek.factors.sentiment_ai")


class AISentimentFactor(BaseFactor):
    """AI 情绪因子"""
    
    name = "sentiment_ai"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        self.ai_service = AIService(provider=config.get('provider', 'bailian') if config else 'bailian')
        self.cache = {}
    
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """计算 AI 情绪因子"""
        news_list = data.get('news', [])
        social_posts = data.get('social', [])
        
        if not news_list and not social_posts:
            return FactorResult(
                code=code,
                factor_name='sentiment_ai',
                value=0.0,
                timestamp=data.get('timestamp', ''),
                metadata={'news_count': 0, 'social_count': 0, 'ai_provider': self.ai_service.provider.name}
            )
        
        # 分析新闻情绪
        news_scores = []
        for news in news_list[:10]:  # 限制最多 10 条
            text = f"{news.get('title', '')} {news.get('content', '')}"
            cache_key = f"sentiment_{hash(text)}"
            
            if cache_key in self.cache:
                score = self.cache[cache_key]
            else:
                response = self.ai_service.analyze_sentiment(text)
                if response.success:
                    score = response.data.get('sentiment_score', 0.0)
                    self.cache[cache_key] = score
                else:
                    score = 0.0
            
            news_scores.append(score)
        
        # 分析社交情绪
        social_scores = []
        for post in social_posts[:20]:  # 限制最多 20 条
            cache_key = f"sentiment_{hash(post)}"
            
            if cache_key in self.cache:
                score = self.cache[cache_key]
            else:
                response = self.ai_service.analyze_sentiment(post)
                if response.success:
                    score = response.data.get('sentiment_score', 0.0)
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
                'ai_provider': self.ai_service.provider.name,
                'cache_size': len(self.cache)
            }
        )
