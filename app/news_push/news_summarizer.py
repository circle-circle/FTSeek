#!/usr/bin/env python3
"""
新闻汇总与主题聚类引擎 (Phase 1)

功能:
- 批量新闻聚合
- 按主题聚类（政策/财报/宏观/突发事件/板块）
- 按相关股票分组
- 计算主题情绪趋势（rising/falling/stable）
- 生成结构化摘要JSON

用法:
    from app.news_push.news_summarizer import NewsSummarizer
    summarizer = NewsSummarizer()
    summary = summarizer.summarize(hours=12)
"""
import logging
import time
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("ftseek.news_push.summarizer")
logger.propagate = False


# 主题关键词定义
TOPIC_KEYWORDS = {
    'policy': {
        'name': '政策监管',
        'keywords': [
            '政策', '监管', '央行', '利率', '财政', '货币政策', '降准', '加息',
            'policy', 'regulation', 'central bank', 'interest rate', 'fiscal',
            'monetary', 'cut', 'hike', 'fed', 'pboc'
        ]
    },
    'earnings': {
        'name': '财报业绩',
        'keywords': [
            '财报', '盈利', '营收', '净利润', '每股收益', '业绩', '季报', '年报',
            'earnings', 'revenue', 'profit', 'net income', 'eps', 'quarterly',
            'annual', 'guidance', 'beat', 'miss'
        ]
    },
    'macro': {
        'name': '宏观经济',
        'keywords': [
            'GDP', '通胀', 'CPI', 'PMI', '非农', '就业', '失业', '经济',
            'growth', 'inflation', 'consumer price', 'producer price',
            'employment', 'unemployment', 'recession', 'expansion'
        ]
    },
    'breaking': {
        'name': '突发事件',
        'keywords': [
            '突发', '紧急', '公告', '制裁', '收购', '合并', 'IPO', '退市',
            'breaking', 'urgent', 'announcement', 'sanction', 'acquisition',
            'merger', 'ipo', 'delist', 'investigation', 'scandal'
        ]
    },
    'sector': {
        'name': '板块行业',
        'keywords': [
            '板块', '行业', '概念', '科技', '金融', '消费', '医药', '新能源',
            'sector', 'industry', 'tech', 'finance', 'consumer', 'healthcare',
            'energy', 'semiconductor', 'AI', 'electric vehicle'
        ]
    },
    'geopolitics': {
        'name': '地缘政治',
        'keywords': [
            '贸易战', '关税', '制裁', '外交', '冲突', '战争', '选举',
            'trade war', 'tariff', 'sanction', 'diplomatic', 'conflict',
            'war', 'election', 'tension'
        ]
    },
}


class NewsSummarizer:
    """新闻汇总与主题聚类引擎"""
    
    def __init__(self, collector=None):
        """
        Args:
            collector: NewsCollector实例，如为None则自动获取
        """
        self.collector = collector
        if not self.collector:
            try:
                from app.news_push.collector import get_collector
                self.collector = get_collector()
            except Exception as e:
                logger.warning(f"新闻采集器初始化失败: {e}")
                self.collector = None
        
        logger.info("NewsSummarizer 初始化完成")
    
    def summarize(self, hours: int = 12, max_news: int = 100) -> Dict:
        """
        生成新闻汇总
        
        Args:
            hours: 回溯时间（小时）
            max_news: 最多分析新闻数
        
        Returns:
            {
                'timestamp': '2026-04-13T08:30:00',
                'total_news': 45,
                'time_range': {'start': '...', 'end': '...'},
                'topics': {
                    'policy': {
                        'name': '政策监管',
                        'count': 12,
                        'sentiment_trend': 'rising',
                        'avg_score': 0.65,
                        'top_news': [...],
                        'related_stocks': [...]
                    }
                },
                'stock_focus': {
                    'HK.00700': {
                        'news_count': 8,
                        'sentiment': 0.72,
                        'trend': 'rising',
                        'key_events': ['财报超预期', '纳入港股通']
                    }
                },
                'breaking_news': [...],
                'overall_sentiment': 0.45,
                'sentiment_trend': 'rising'
            }
        """
        start_time = time.time()
        
        # 1. 获取新闻列表
        news_list = self._fetch_news(hours, max_news)
        if not news_list:
            logger.warning(f"未获取到新闻（回溯{hours}小时）")
            return self._empty_summary()
        
        logger.info(f"获取到 {len(news_list)} 条新闻（回溯{hours}小时）")
        
        # 2. 按主题聚类
        topics = self._cluster_by_topic(news_list)
        
        # 3. 按股票分组
        stock_focus = self._cluster_by_stock(news_list)
        
        # 4. 提取突发新闻
        breaking_news = self._extract_breaking_news(news_list)
        
        # 5. 计算整体情绪
        overall_sentiment = self._calculate_overall_sentiment(news_list)
        
        # 6. 构建汇总
        summary = {
            'timestamp': datetime.now().isoformat(),
            'total_news': len(news_list),
            'time_range': {
                'start': (datetime.now() - timedelta(hours=hours)).isoformat(),
                'end': datetime.now().isoformat(),
            },
            'topics': topics,
            'stock_focus': stock_focus,
            'breaking_news': breaking_news,
            'overall_sentiment': overall_sentiment['avg_score'],
            'sentiment_trend': overall_sentiment['trend'],
            'generation_time_ms': int((time.time() - start_time) * 1000),
        }
        
        logger.info(
            f"新闻汇总生成完成: {len(news_list)}条新闻, "
            f"{len(topics)}个主题, {len(stock_focus)}只焦点股, "
            f"耗时{summary['generation_time_ms']}ms"
        )
        
        return summary
    
    def _fetch_news(self, hours: int, max_news: int) -> List:
        """获取新闻列表"""
        if not self.collector:
            logger.warning("新闻采集器不可用")
            return []
        
        try:
            # 调用采集器获取所有新闻
            news_list = self.collector.fetch_all()
            
            # 按时间过滤
            cutoff_time = datetime.now() - timedelta(hours=hours)
            filtered = []
            
            for news in news_list:
                try:
                    pub_time = self._parse_news_time(news)
                    if pub_time and pub_time >= cutoff_time:
                        filtered.append(news)
                except Exception as e:
                    logger.debug(f"新闻时间解析失败: {e}")
                    continue
            
            # 按优先级排序（优先级数字越小越重要）
            filtered.sort(key=lambda x: getattr(x, 'priority', 5))
            
            # 限制数量
            return filtered[:max_news]
            
        except Exception as e:
            logger.error(f"获取新闻失败: {e}")
            return []
    
    def _cluster_by_topic(self, news_list: List) -> Dict:
        """按主题聚类"""
        topic_clusters = defaultdict(list)
        
        for news in news_list:
            # 获取新闻文本
            title = getattr(news, 'title', '') or ''
            content = getattr(news, 'content', '') or ''
            text = f"{title} {content}".lower()
            
            # 匹配主题（可能匹配多个）
            matched_topics = []
            for topic_id, topic_info in TOPIC_KEYWORDS.items():
                for keyword in topic_info['keywords']:
                    if keyword.lower() in text:
                        matched_topics.append(topic_id)
                        break
            
            # 如果未匹配到任何主题，归为'other'
            if not matched_topics:
                matched_topics = ['other']
            
            # 添加到所有匹配的主题
            for topic_id in matched_topics:
                topic_clusters[topic_id].append(news)
        
        # 计算每个主题的统计信息
        topics_result = {}
        for topic_id, news_items in topic_clusters.items():
            if topic_id == 'other':
                topic_name = '其他'
            else:
                topic_name = TOPIC_KEYWORDS[topic_id]['name']
            
            # 计算情绪趋势
            trend = self._identify_trend(news_items)
            
            # 计算平均情绪得分
            scores = []
            for news in news_items:
                score = getattr(news, 'sentiment_score', 0.0)
                scores.append(score)
            avg_score = sum(scores) / len(scores) if scores else 0.0
            
            # 提取Top新闻（按优先级）
            top_news = sorted(news_items, key=lambda x: getattr(x, 'priority', 5))[:5]
            top_news_titles = [getattr(n, 'title', '') for n in top_news]
            
            # 收集相关股票
            related_stocks = set()
            for news in news_items:
                stocks = getattr(news, 'related_stocks', [])
                related_stocks.update(stocks)
            
            topics_result[topic_id] = {
                'name': topic_name,
                'count': len(news_items),
                'sentiment_trend': trend,
                'avg_score': round(avg_score, 2),
                'top_news': top_news_titles,
                'related_stocks': list(related_stocks)[:10],  # 限制10只
            }
        
        # 按数量排序
        topics_result = dict(
            sorted(topics_result.items(), key=lambda x: x[1]['count'], reverse=True)
        )
        
        return topics_result
    
    def _cluster_by_stock(self, news_list: List) -> Dict:
        """按相关股票分组"""
        stock_news = defaultdict(list)
        
        for news in news_list:
            stocks = getattr(news, 'related_stocks', [])
            for stock in stocks:
                stock_news[stock].append(news)
        
        # 构建焦点股信息（仅保留3条以上新闻的股票）
        stock_focus = {}
        for stock, news_items in stock_news.items():
            if len(news_items) < 3:
                continue
            
            # 计算情绪
            scores = [getattr(n, 'sentiment_score', 0.0) for n in news_items]
            avg_sentiment = sum(scores) / len(scores)
            
            # 识别趋势
            trend = self._identify_trend(news_items)
            
            # 提取关键事件（Top新闻标题）
            sorted_news = sorted(news_items, key=lambda x: getattr(x, 'priority', 5))
            key_events = [getattr(n, 'title', '') for n in sorted_news[:5]]
            
            stock_focus[stock] = {
                'news_count': len(news_items),
                'sentiment': round(avg_sentiment, 2),
                'trend': trend,
                'key_events': key_events,
            }
        
        # 按新闻数量排序
        stock_focus = dict(
            sorted(stock_focus.items(), key=lambda x: x[1]['news_count'], reverse=True)
        )
        
        return stock_focus
    
    def _identify_trend(self, news_items: List) -> str:
        """
        识别情绪趋势
        
        Returns:
            'rising' / 'falling' / 'stable'
        """
        if len(news_items) < 3:
            return 'stable'
        
        # 按时间排序
        sorted_news = sorted(
            news_items,
            key=lambda x: self._parse_news_time(x) or datetime.min
        )
        
        # 分割前后半段
        mid = len(sorted_news) // 2
        first_half = sorted_news[:mid]
        second_half = sorted_news[mid:]
        
        # 计算平均情绪
        first_scores = [getattr(n, 'sentiment_score', 0.0) for n in first_half]
        second_scores = [getattr(n, 'sentiment_score', 0.0) for n in second_half]
        
        first_avg = sum(first_scores) / len(first_scores) if first_scores else 0
        second_avg = sum(second_scores) / len(second_scores) if second_scores else 0
        
        # 判断趋势
        diff = second_avg - first_avg
        if diff > 0.3:
            return 'rising'
        elif diff < -0.3:
            return 'falling'
        else:
            return 'stable'
    
    def _extract_breaking_news(self, news_list: List) -> List[Dict]:
        """提取突发新闻"""
        breaking = []
        
        for news in news_list:
            is_breaking = getattr(news, 'is_breaking', False)
            priority = getattr(news, 'priority', 5)
            
            # 突发新闻或P1/P2优先级
            if is_breaking or priority <= 2:
                breaking.append({
                    'title': getattr(news, 'title', ''),
                    'source': getattr(news, 'source', ''),
                    'priority': priority,
                    'published_at': getattr(news, 'published_at', ''),
                    'url': getattr(news, 'url', ''),
                })
        
        # 按优先级排序
        breaking.sort(key=lambda x: x['priority'])
        
        return breaking[:10]  # 最多10条
    
    def _calculate_overall_sentiment(self, news_list: List) -> Dict:
        """计算整体情绪"""
        scores = [getattr(n, 'sentiment_score', 0.0) for n in news_list]
        
        if not scores:
            return {'avg_score': 0.0, 'trend': 'stable'}
        
        avg_score = sum(scores) / len(scores)
        
        # 简化趋势计算（基于最新10条）
        latest_10 = news_list[:10]
        trend = self._identify_trend(latest_10)
        
        return {
            'avg_score': round(avg_score, 2),
            'trend': trend,
        }
    
    def _parse_news_time(self, news) -> Optional[datetime]:
        """解析新闻发布时间"""
        try:
            pub_at = getattr(news, 'published_at', '')
            if not pub_at:
                return None
            
            # 尝试多种格式
            for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                try:
                    return datetime.strptime(pub_at[:19], fmt)
                except ValueError:
                    continue
            
            # 尝试ISO格式
            return datetime.fromisoformat(pub_at.replace('Z', '+00:00').replace('+00:00', ''))
        except Exception:
            return None
    
    def _empty_summary(self) -> Dict:
        """返回空摘要"""
        return {
            'timestamp': datetime.now().isoformat(),
            'total_news': 0,
            'time_range': {
                'start': datetime.now().isoformat(),
                'end': datetime.now().isoformat(),
            },
            'topics': {},
            'stock_focus': {},
            'breaking_news': [],
            'overall_sentiment': 0.0,
            'sentiment_trend': 'stable',
            'generation_time_ms': 0,
        }
    
    def format_for_dingtalk(self, summary: Dict) -> str:
        """
        格式化为钉钉Markdown消息
        
        Args:
            summary: summarize()返回的摘要
        
        Returns:
            Markdown格式字符串
        """
        lines = [
            "### 📰 新闻汇总分析",
            "",
            f"**时间范围**: {summary['time_range']['start'][:16]} ~ {summary['time_range']['end'][:16]}",
            f"**新闻总数**: {summary['total_news']} 条",
            f"**整体情绪**: {summary['overall_sentiment']:+.2f} ({summary['sentiment_trend']})",
            "",
        ]
        
        # 突发新闻
        if summary['breaking_news']:
            lines.append("#### 🚨 突发新闻")
            for i, news in enumerate(summary['breaking_news'][:5], 1):
                lines.append(f"{i}. **{news['title']}** (来源: {news['source']})")
            lines.append("")
        
        # 主题分布
        if summary['topics']:
            lines.append("#### 📊 主题分布")
            for topic_id, topic_info in summary['topics'].items():
                emoji = {'rising': '📈', 'falling': '📉', 'stable': '➡️'}.get(
                    topic_info['sentiment_trend'], '➡️'
                )
                lines.append(
                    f"- **{topic_info['name']}**: {topic_info['count']}条 "
                    f"情绪{topic_info['avg_score']:+.2f} {emoji}{topic_info['sentiment_trend']}"
                )
            lines.append("")
        
        # 焦点股
        if summary['stock_focus']:
            lines.append("#### 🎯 焦点个股")
            for stock, info in list(summary['stock_focus'].items())[:5]:
                emoji = {'rising': '📈', 'falling': '📉', 'stable': '➡️'}.get(
                    info['trend'], '➡️'
                )
                lines.append(
                    f"- **{stock}**: {info['news_count']}条新闻 "
                    f"情绪{info['sentiment']:+.2f} {emoji}{info['trend']}"
                )
            lines.append("")
        
        return '\n'.join(lines)


# 单例模式
_instance = None

def get_summarizer() -> NewsSummarizer:
    """获取NewsSummarizer单例"""
    global _instance
    if _instance is None:
        _instance = NewsSummarizer()
    return _instance


if __name__ == '__main__':
    # 测试
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    summarizer = get_summarizer()
    summary = summarizer.summarize(hours=12)
    
    print("\n" + "="*80)
    print("新闻汇总测试结果")
    print("="*80)
    print(f"新闻总数: {summary['total_news']}")
    print(f"整体情绪: {summary['overall_sentiment']:+.2f} ({summary['sentiment_trend']})")
    print(f"主题数: {len(summary['topics'])}")
    print(f"焦点股: {len(summary['stock_focus'])}")
    print(f"突发新闻: {len(summary['breaking_news'])}")
    print(f"生成耗时: {summary['generation_time_ms']}ms")
    
    print("\n钉钉格式输出:")
    print(summarizer.format_for_dingtalk(summary))
