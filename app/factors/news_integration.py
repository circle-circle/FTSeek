"""
新闻数据集成模块

自动从新闻采集器获取新闻，供 AI 分析使用
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek.factors.news_integration")

# 导入新闻采集器
try:
    from app.news_push.collector import get_news_collector, NewsItem
    NEWS_COLLECTOR_AVAILABLE = True
except ImportError:
    NEWS_COLLECTOR_AVAILABLE = False
    logger.warning("新闻采集器未安装，使用模拟模式")

# 导入 AI 服务
from app.ai import AIService


class NewsSentimentAnalyzer:
    """新闻情绪分析器"""
    
    def __init__(self):
        if NEWS_COLLECTOR_AVAILABLE:
            self.collector = get_news_collector()
        else:
            self.collector = None
        
        self.ai = AIService()
        self.cache = {}
        self.cache_ttl = 1800  # 30 分钟缓存
        
        logger.info("新闻情绪分析器已初始化")
    
    def analyze_stock(self, 
                     code: str, 
                     lookback_hours: int = 24,
                     max_news: int = 20) -> Dict:
        """
        分析股票情绪
        
        Args:
            code: 股票代码 (HK.00700)
            lookback_hours: 回溯时间 (小时)
            max_news: 最多分析新闻数
        
        Returns:
            {
                'sentiment_score': 0.75,  # -1 到 1
                'news_count': 10,
                'positive_count': 8,
                'negative_count': 2,
                'neutral_count': 0,
                'top_news': [...],
                'ai_provider': 'bailian',
                'cache_hit': False
            }
        """
        # 检查缓存
        cache_key = f"{code}_{lookback_hours}"
        if cache_key in self.cache:
            cached_result, timestamp = self.cache[cache_key]
            if (datetime.now() - timestamp).total_seconds() < self.cache_ttl:
                logger.debug(f"使用缓存情绪分析：{code}")
                cached_result['cache_hit'] = True
                return cached_result
        
        # 1. 获取新闻
        if self.collector:
            news_list = self._fetch_news(code, lookback_hours)
        else:
            news_list = []
            logger.warning("新闻采集器不可用，使用空新闻列表")
        
        if not news_list:
            logger.warning(f"未获取到新闻：{code}")
            result = {
                'sentiment_score': 0.0,
                'news_count': 0,
                'positive_count': 0,
                'negative_count': 0,
                'neutral_count': 0,
                'top_news': [],
                'ai_provider': 'bailian',
                'cache_hit': False
            }
            self.cache[cache_key] = (result, datetime.now())
            return result
        
        # 2. AI 情绪分析
        scores = []
        analyzed_news = []
        
        for news in news_list[:max_news]:
            # 构建分析文本
            text = f"{news.get('title', '')} {news.get('summary', '')} {news.get('content', '')}"
            
            if not text.strip():
                continue
            
            # 检查新闻缓存
            news_cache_key = f"news_sentiment_{hash(text)}"
            if news_cache_key in self.cache:
                score, _ = self.cache[news_cache_key]
            else:
                # 调用 AI API 分析
                response = self.ai.analyze_sentiment(text)
                
                if response.success:
                    score = response.data.get('sentiment_score', 0.0)
                    # 缓存结果
                    self.cache[news_cache_key] = (score, datetime.now())
                else:
                    logger.warning(f"AI 分析失败：{news.get('title', '')[:50]}")
                    score = 0.0
            
            scores.append(score)
            
            # 记录分析结果
            analyzed_news.append({
                'title': news.get('title', '')[:100],
                'summary': news.get('summary', '')[:200],
                'source': news.get('source', ''),
                'published_at': news.get('published_at', ''),
                'sentiment_score': score,
                'url': news.get('url', '')
            })
        
        # 3. 统计结果
        positive_count = sum(1 for s in scores if s > 0.2)
        negative_count = sum(1 for s in scores if s < -0.2)
        neutral_count = len(scores) - positive_count - negative_count
        
        result = {
            'sentiment_score': sum(scores) / len(scores) if scores else 0.0,
            'news_count': len(scores),
            'positive_count': positive_count,
            'negative_count': negative_count,
            'neutral_count': neutral_count,
            'top_news': analyzed_news[:5],  # 只返回前 5 条
            'ai_provider': 'bailian',
            'cache_hit': False,
            'lookback_hours': lookback_hours,
            'analyzed_at': datetime.now().isoformat()
        }
        
        # 缓存结果
        self.cache[cache_key] = (result, datetime.now())
        
        logger.info(f"新闻情绪分析完成：{code} - {len(scores)} 条新闻，得分 {result['sentiment_score']:.2f}")
        
        return result
    
    def _fetch_news(self, code: str, lookback_hours: int) -> List[Dict]:
        """获取新闻"""
        if not self.collector:
            return []
        
        try:
            # 从新闻采集器获取
            cutoff_time = datetime.now() - timedelta(hours=lookback_hours)
            
            # 获取所有新闻
            all_news = self.collector.fetch_all()
            
            # 过滤时间
            filtered_news = []
            for news in all_news:
                try:
                    published_at = datetime.fromisoformat(news.get('published_at', ''))
                    if published_at >= cutoff_time:
                        filtered_news.append(news)
                except:
                    # 时间解析失败，保留新闻
                    filtered_news.append(news)
            
            # 按优先级排序
            filtered_news.sort(key=lambda x: x.get('priority', 5))
            
            logger.info(f"获取新闻：{code} - {len(filtered_news)} 条 (最近{lookback_hours}小时)")
            
            return filtered_news
        
        except Exception as e:
            logger.error(f"获取新闻失败：{e}")
            return []
    
    def get_sentiment_trend(self, code: str, days: int = 7) -> Dict:
        """
        获取情绪趋势
        
        Args:
            code: 股票代码
            days: 天数
        
        Returns:
            {
                'trend': 'improving' | 'worsening' | 'stable',
                'daily_scores': [...],
                'avg_score': 0.5
            }
        """
        daily_scores = []
        
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            # 这里应该从历史数据中获取
            # 简化实现：使用当前分析
            result = self.analyze_stock(code, lookback_hours=24)
            daily_scores.append({
                'date': date.strftime('%Y-%m-%d'),
                'score': result['sentiment_score']
            })
        
        # 计算趋势
        if len(daily_scores) >= 2:
            recent_avg = sum(s['score'] for s in daily_scores[:3]) / 3
            older_avg = sum(s['score'] for s in daily_scores[3:]) / max(1, len(daily_scores) - 3)
            
            if recent_avg > older_avg + 0.1:
                trend = 'improving'
            elif recent_avg < older_avg - 0.1:
                trend = 'worsening'
            else:
                trend = 'stable'
        else:
            trend = 'stable'
        
        return {
            'trend': trend,
            'daily_scores': daily_scores,
            'avg_score': sum(s['score'] for s in daily_scores) / len(daily_scores) if daily_scores else 0
        }
    
    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()
        logger.info("新闻情绪分析缓存已清空")


# 全局单例
_analyzer = None

def get_news_sentiment_analyzer() -> NewsSentimentAnalyzer:
    """获取新闻情绪分析器单例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = NewsSentimentAnalyzer()
    return _analyzer


# 命令行工具
if __name__ == '__main__':
    import sys
    
    analyzer = get_news_sentiment_analyzer()
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 news_integration.py HK.00700 [hours]")
        sys.exit(1)
    
    code = sys.argv[1]
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    
    print(f"分析股票：{code}")
    print(f"回溯时间：{hours}小时")
    print()
    
    result = analyzer.analyze_stock(code, lookback_hours=hours)
    
    print(f"情绪得分：{result['sentiment_score']:.2f}")
    print(f"新闻数量：{result['news_count']}")
    print(f"正面：{result['positive_count']} | 负面：{result['negative_count']} | 中性：{result['neutral_count']}")
    print()
    
    if result['top_news']:
        print("Top 新闻:")
        for i, news in enumerate(result['top_news'], 1):
            sentiment_emoji = '🟢' if news['sentiment_score'] > 0.2 else '🔴' if news['sentiment_score'] < -0.2 else '⚪'
            print(f"  {i}. {sentiment_emoji} {news['title'][:80]}...")
            print(f"     得分：{news['sentiment_score']:.2f} | 来源：{news['source']}")
