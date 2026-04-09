"""
NewsObserver - 新闻观察模块

功能:
- 交易前新闻检查
- 交易中新闻监控
- 交易后新闻复盘
- 作为风控参考源
"""
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib

logger = logging.getLogger("ftseek.v4.news_observer")
logger.propagate = False  # 防止日志重复


@dataclass
class NewsItem:
    """新闻数据"""
    id: str
    title: str
    content: str
    source: str
    url: str
    published_at: str
    sentiment: float = 0.0  # -1 (利空) 到 1 (利好)
    related_stocks: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at,
            "sentiment": self.sentiment,
            "related_stocks": self.related_stocks,
        }


class NewsObserver:
    """新闻观察器"""
    
    def __init__(self):
        self.news_cache: List[NewsItem] = []
        self.cache_duration = 3600  # 缓存 1 小时
        self.keywords = {
            "storage": ["存储", "DRAM", "NAND", "芯片", "半导体"],
            "earnings": ["财报", "业绩", "盈利", "营收"],
            "risk": ["调查", "诉讼", "处罚", "监管"],
        }
        self.logger = logging.getLogger("ftseek.v4.news_observer")
        self.logger.propagate = False  # 防止日志重复

    def check_before_trade(self, code: str) -> Dict[str, Any]:
        """
        交易前新闻检查
        
        Args:
            code: 股票代码
            
        Returns:
            检查结果 {has_negative, news_list, recommendation}
        """
        self.logger.info(f"交易前新闻检查：{code}")
        
        # 获取相关新闻
        related_news = self._get_related_news(code)
        
        # 检查负面新闻
        negative_news = [n for n in related_news if n.sentiment < -0.3]
        
        result = {
            "code": code,
            "check_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "has_negative": len(negative_news) > 0,
            "negative_count": len(negative_news),
            "news_list": [n.to_dict() for n in related_news[:5]],
            "recommendation": self._get_recommendation(related_news),
        }
        
        self.logger.info(
            f"交易前检查完成：{code}, "
            f"相关{len(related_news)}条，负面{len(negative_news)}条"
        )
        
        return result
    
    def monitor_during_trade(self, code: str) -> List[NewsItem]:
        """
        交易中新闻监控
        
        Args:
            code: 股票代码
            
        Returns:
            最新新闻列表
        """
        self.logger.debug(f"交易中新闻监控：{code}")
        return self._get_related_news(code, minutes=30)
    
    def review_after_trade(self, code: str, trade_date: str) -> Dict[str, Any]:
        """
        交易后新闻复盘
        
        Args:
            code: 股票代码
            trade_date: 交易日期
            
        Returns:
            复盘报告
        """
        self.logger.info(f"交易后新闻复盘：{code} ({trade_date})")
        
        # 获取当日新闻
        day_news = self._get_news_by_date(trade_date)
        related_news = [n for n in day_news if code in n.related_stocks]
        
        report = {
            "code": code,
            "trade_date": trade_date,
            "total_news": len(related_news),
            "positive": len([n for n in related_news if n.sentiment > 0.3]),
            "negative": len([n for n in related_news if n.sentiment < -0.3]),
            "neutral": len(related_news) - len([n for n in related_news if abs(n.sentiment) > 0.3]),
            "key_news": [n.to_dict() for n in related_news[:3]],
        }
        
        return report
    
    def _get_related_news(
        self,
        code: str,
        minutes: int = 60
    ) -> List[NewsItem]:
        """获取相关新闻"""
        # 实际使用时应从 API 获取
        # 这里返回缓存中的新闻
        cutoff = time.time() - (minutes * 60)
        
        related = []
        for news in self.news_cache:
            if news.published_at and code in news.related_stocks:
                related.append(news)
        
        return related
    
    def _get_news_by_date(self, date: str) -> List[NewsItem]:
        """获取指定日期新闻"""
        # 简化实现
        return self.news_cache
    
    def _get_recommendation(self, news_list: List[NewsItem]) -> str:
        """获取建议"""
        if not news_list:
            return "无相关新闻，可正常交易"
        
        avg_sentiment = sum(n.sentiment for n in news_list) / len(news_list)
        
        if avg_sentiment > 0.3:
            return "利好消息，可谨慎参与"
        elif avg_sentiment < -0.3:
            return "⚠️ 利空消息，建议观望"
        else:
            return "消息中性，按技术面执行"
    
    def add_news(self, news: NewsItem):
        """添加新闻到缓存"""
        self.news_cache.append(news)
        
        # 清理过期新闻
        cutoff = time.time() - self.cache_duration
        self.news_cache = [
            n for n in self.news_cache
            if n.published_at and datetime.fromisoformat(n.published_at).timestamp() > cutoff
        ]
        
        self.logger.debug(f"添加新闻：{news.title[:30]}...")
    
    def get_sentiment_summary(self, code: str) -> Dict[str, float]:
        """获取情绪摘要"""
        related = self._get_related_news(code)
        
        if not related:
            return {"sentiment": 0.0, "confidence": 0.0}
        
        avg = sum(n.sentiment for n in related) / len(related)
        
        return {
            "sentiment": avg,
            "confidence": min(1.0, len(related) / 10),  # 新闻越多置信度越高
        }
    
    def clear_cache(self):
        """清空缓存"""
        self.news_cache.clear()
        self.logger.info("新闻缓存已清空")


# 全局实例
news_observer: Optional[NewsObserver] = None


def get_news_observer() -> NewsObserver:
    """获取 NewsObserver 单例"""
    global news_observer
    if news_observer is None:
        news_observer = NewsObserver()
    return news_observer
