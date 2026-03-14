"""
情绪因子模块

基于简单词典法计算情绪评分 (无外部 API 依赖)
"""
import logging
from typing import Optional, Dict, List
from .base import BaseFactor, FactorResult

logger = logging.getLogger("ftseek.factors.sentiment")

POSITIVE_WORDS = {
    '增长', '上涨', '突破', '利好', '超预期', '盈利', '复苏', '强劲',
    '乐观', '创新高', '买入', '推荐', '目标价上调', '业绩大增'
}

NEGATIVE_WORDS = {
    '下跌', '下滑', '亏损', '利空', '低于预期', '衰退', '疲软',
    '悲观', '新低', '卖出', '下调', '风险', '警告', '诉讼'
}


class SentimentFactors(BaseFactor):
    """情绪因子"""
    
    name = "sentiment"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        self.positive_words = self.config.get('positive_words', POSITIVE_WORDS)
        self.negative_words = self.config.get('negative_words', NEGATIVE_WORDS)
    
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """计算情绪因子"""
        news_list = data.get('news', [])
        social_posts = data.get('social', [])
        
        news_sentiment = self._calc_news_sentiment(news_list) if news_list else 0.0
        social_sentiment = self._calc_social_sentiment(social_posts) if social_posts else 0.0
        
        score = news_sentiment * 0.6 + social_sentiment * 0.4
        
        return FactorResult(
            code=code,
            factor_name='sentiment',
            value=score,
            timestamp=data.get('timestamp', ''),
            metadata={
                'news_sentiment': news_sentiment,
                'social_sentiment': social_sentiment,
                'news_count': len(news_list) if news_list else 0,
                'social_count': len(social_posts) if social_posts else 0
            }
        )
    
    def _calc_news_sentiment(self, news_list: List[Dict]) -> float:
        """计算新闻情绪"""
        if not news_list:
            return 0.0
        
        scores = []
        for news in news_list:
            title = news.get('title', '')
            content = news.get('content', '')
            text = f"{title} {content}"
            score = self._sentiment_score(text)
            scores.append(score)
        
        return sum(scores) / len(scores) if scores else 0.0
    
    def _calc_social_sentiment(self, posts: List[str]) -> float:
        """计算社交媒体情绪"""
        if not posts:
            return 0.0
        
        scores = [self._sentiment_score(post) for post in posts]
        return sum(scores) / len(scores) if scores else 0.0
    
    def _sentiment_score(self, text: str) -> float:
        """计算单条文本的情绪评分"""
        if not text:
            return 0.0
        
        positive_count = sum(1 for word in self.positive_words if word in text)
        negative_count = sum(1 for word in self.negative_words if word in text)
        
        total = positive_count + negative_count
        if total == 0:
            return 0.0
        
        score = (positive_count - negative_count) / total
        return max(-1.0, min(1.0, score))


def sentiment_score(text: str) -> float:
    """便捷函数：计算文本情绪评分"""
    factor = SentimentFactors()
    return factor._sentiment_score(text)
