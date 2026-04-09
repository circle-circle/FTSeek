"""
突发新闻快速通道 (Breaking News Fast Track)

功能:
- 突发新闻实时检测
- 高优先级新闻快速处理
- 跳过缓存直接推送
- 波动率联动响应
"""
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import hashlib

logger = logging.getLogger("ftseek.news_push.breaking_news")
logger.propagate = False

# 突发新闻关键词 (按严重程度分级)
CRITICAL_KEYWORDS = [
    # 中文
    '突发', '紧急', '重大', '公告', '停牌', '复牌', '调查', '制裁',
    '财报', '业绩', '盈利预警', '亏损', '破产', '重组', '收购',
    'IPO', '退市', '处罚', '诉讼', '违约', '爆雷',
    # 英文
    'breaking', 'urgent', 'emergency', 'announcement',
    'earnings', 'bankruptcy', 'acquisition', 'merger',
    'investigation', 'sanction', 'lawsuit', 'default',
    'ipo', 'delist', 'suspension', 'resumption',
]

# 高优先级股票 (这些股票的新闻更容易被标记为突发)
PRIORITY_STOCKS = {
    'HK.00700': ['腾讯', 'Tencent', '微信'],
    'HK.09988': ['阿里', 'Alibaba', '淘宝'],
    'HK.09992': ['泡泡玛特', 'Pop Mart'],
    'US.AAPL': ['苹果', 'Apple', 'iPhone'],
    'US.TSLA': ['特斯拉', 'Tesla', '马斯克'],
    'US.NVDA': ['英伟达', 'NVIDIA', 'AI 芯片'],
}


@dataclass
class BreakingNewsAlert:
    """突发新闻警报"""
    news_id: str
    title: str
    content: str
    source: str
    priority: int  # 1=最高，5=最低
    severity: str  # critical/high/normal
    detected_at: str = field(default_factory=lambda: datetime.now().isoformat())
    related_stocks: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    sentiment_score: float = 0.0
    is_pushed: bool = False
    push_time: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            'news_id': self.news_id,
            'title': self.title,
            'content': self.content,
            'source': self.source,
            'priority': self.priority,
            'severity': self.severity,
            'detected_at': self.detected_at,
            'related_stocks': self.related_stocks,
            'keywords': self.keywords,
            'sentiment_score': self.sentiment_score,
            'is_pushed': self.is_pushed,
            'push_time': self.push_time,
        }


class BreakingNewsDetector:
    """
    突发新闻检测器
    
    实时检测突发新闻并触发快速推送
    """
    
    def __init__(self, config: Dict = None):
        """
        初始化检测器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        
        # 检测阈值
        self.keyword_threshold = 1  # 至少匹配 1 个关键词
        self.priority_stocks = self.config.get('priority_stocks', PRIORITY_STOCKS)
        
        # 去重记录
        self._seen_news: Dict[str, datetime] = {}
        self._dedup_window = 3600  # 1 小时去重窗口
        
        # 统计
        self._total_detected = 0
        self._total_pushed = 0
        
        logger.info("突发新闻检测器已初始化")
    
    def detect(self, news_item) -> Optional[BreakingNewsAlert]:
        """
        检测新闻是否为突发新闻
        
        Args:
            news_item: NewsItem 对象
        
        Returns:
            BreakingNewsAlert 或 None
        """
        # 检查是否已经处理过
        news_id = getattr(news_item, 'id', None)
        if news_id and news_id in self._seen_news:
            if self._is_within_dedup_window(news_id):
                logger.debug(f"突发新闻去重：{news_id}")
                return None
        
        # 获取新闻文本
        title = getattr(news_item, 'title', '')
        content = getattr(news_item, 'content', '')
        text = f"{title} {content}".lower()
        
        # 关键词匹配
        matched_keywords = []
        for kw in CRITICAL_KEYWORDS:
            if kw.lower() in text:
                matched_keywords.append(kw)
        
        if len(matched_keywords) < self.keyword_threshold:
            return None
        
        # 匹配股票
        related_stocks = self._match_stocks(text)
        
        # 计算优先级
        priority = self._calculate_priority(
            matched_keywords=matched_keywords,
            related_stocks=related_stocks,
            source=getattr(news_item, 'source', ''),
            category=getattr(news_item, 'category', ''),
        )
        
        # 计算严重程度
        severity = self._calculate_severity(matched_keywords, priority)
        
        # 创建警报
        alert = BreakingNewsAlert(
            news_id=news_id or self._generate_id(title),
            title=title,
            content=content,
            source=getattr(news_item, 'source', 'unknown'),
            priority=priority,
            severity=severity,
            related_stocks=related_stocks,
            keywords=matched_keywords,
            sentiment_score=getattr(news_item, 'sentiment_score', 0.0),
        )
        
        # 记录
        if news_id:
            self._seen_news[news_id] = datetime.now()
        self._total_detected += 1
        
        logger.info(
            f"检测到突发新闻：{title[:50]}... | "
            f"优先级：{priority} | 严重程度：{severity} | "
            f"相关股票：{related_stocks}"
        )
        
        return alert
    
    def _is_within_dedup_window(self, news_id: str) -> bool:
        """检查是否在去重窗口内"""
        if news_id not in self._seen_news:
            return False
        
        last_seen = self._seen_news[news_id]
        return (datetime.now() - last_seen).total_seconds() < self._dedup_window
    
    def _match_stocks(self, text: str) -> List[str]:
        """匹配相关股票"""
        matched = []
        
        # 尝试从 STOCK_ALIASES 匹配
        try:
            from app.news_push.config import STOCK_ALIASES
            for code, aliases in STOCK_ALIASES.items():
                for alias in aliases:
                    if alias.lower() in text:
                        matched.append(code)
                        break
        except ImportError:
            pass
        
        # 尝试从 priority_stocks 匹配
        for code, keywords in self.priority_stocks.items():
            for kw in keywords:
                if kw.lower() in text and code not in matched:
                    matched.append(code)
                    break
        
        return list(set(matched))
    
    def _calculate_priority(self, matched_keywords: List[str],
                           related_stocks: List[str],
                           source: str,
                           category: str) -> int:
        """
        计算优先级 (1-5, 1 为最高)
        
        规则:
        - 包含'突发'、'紧急'等词：优先级 1
        - 包含'财报'、'收购'等词：优先级 2
        - 匹配优先股票：优先级 -1
        - 快讯类来源：优先级 -1
        """
        priority = 3  # 默认优先级
        
        # 关键词严重程度
        critical_kw = ['突发', '紧急', 'emergency', 'breaking', 'urgent']
        high_kw = ['财报', '业绩', '收购', '重组', 'earnings', 'acquisition']
        
        if any(kw in matched_keywords for kw in critical_kw):
            priority = 1
        elif any(kw in matched_keywords for kw in high_kw):
            priority = 2
        
        # 优先股票
        if related_stocks:
            priority -= 1
        
        # 快讯来源
        if category == 'flash' or source in ['cls', 'blockbeats']:
            priority -= 1
        
        return max(1, min(5, priority))
    
    def _calculate_severity(self, matched_keywords: List[str],
                           priority: int) -> str:
        """
        计算严重程度
        
        Returns:
            'critical' | 'high' | 'normal'
        """
        critical_kw = ['破产', '调查', '制裁', '违约', '爆雷', 'bankruptcy', 'sanction']
        
        if any(kw in matched_keywords for kw in critical_kw) or priority == 1:
            return 'critical'
        elif priority <= 2:
            return 'high'
        else:
            return 'normal'
    
    def _generate_id(self, title: str) -> str:
        """生成新闻 ID"""
        return hashlib.md5(title.encode('utf-8')).hexdigest()[:16]
    
    def cleanup_old_news(self):
        """清理旧的去重记录"""
        now = datetime.now()
        to_remove = []
        
        for news_id, timestamp in self._seen_news.items():
            if (now - timestamp).total_seconds() > self._dedup_window:
                to_remove.append(news_id)
        
        for news_id in to_remove:
            del self._seen_news[news_id]
        
        if to_remove:
            logger.debug(f"清理 {len(to_remove)} 条旧的去重记录")
    
    def get_statistics(self) -> Dict:
        """获取统计信息"""
        return {
            'total_detected': self._total_detected,
            'total_pushed': self._total_pushed,
            'dedup_cache_size': len(self._seen_news),
        }


class FastTrackProcessor:
    """
    快速通道处理器
    
    处理突发新闻的快速推送
    """
    
    def __init__(self, dingtalk_webhook: str = None,
                 dingtalk_secret: str = None):
        """
        初始化处理器
        
        Args:
            dingtalk_webhook: 钉钉 webhook URL
            dingtalk_secret: 钉钉密钥
        """
        self.dingtalk_webhook = dingtalk_webhook
        self.dingtalk_secret = dingtalk_secret
        
        # 推送限流
        self._last_push_time: Dict[str, datetime] = {}
        self.push_cooldown = 60  # 同一股票 60 秒冷却
        
        logger.info("快速通道处理器已初始化")
    
    def process(self, alert: BreakingNewsAlert) -> bool:
        """
        处理突发新闻警报
        
        Args:
            alert: 突发新闻警报
        
        Returns:
            是否推送成功
        """
        # 检查冷却
        for stock in alert.related_stocks:
            if stock in self._last_push_time:
                if (datetime.now() - self._last_push_time[stock]).total_seconds() < self.push_cooldown:
                    logger.debug(f"冷却中，跳过推送：{stock}")
                    return False
        
        # 推送
        success = self._push(alert)
        
        if success:
            # 记录推送时间
            for stock in alert.related_stocks:
                self._last_push_time[stock] = datetime.now()
            alert.is_pushed = True
            alert.push_time = datetime.now().isoformat()
        
        return success
    
    def _push(self, alert: BreakingNewsAlert) -> bool:
        """推送到钉钉"""
        if not self.dingtalk_webhook:
            logger.warning("未配置钉钉 webhook，跳过推送")
            return False
        
        try:
            import requests
            
            # 构建消息
            severity_colors = {
                'critical': '#FF0000',  # 红色
                'high': '#FF8C00',      # 橙色
                'normal': '#FFA500',    # 金色
            }
            
            severity_titles = {
                'critical': '🚨 重大突发',
                'high': '⚡ 高优先级',
                'normal': '📰 突发新闻',
            }
            
            markdown = f"""{severity_titles.get(alert.severity, '📰 突发新闻')}

**{alert.title}**

来源：{alert.source}
时间：{alert.detected_at}
相关股票：{', '.join(alert.related_stocks) if alert.related_stocks else '未匹配'}
关键词：{', '.join(alert.keywords[:5])}
情绪：{alert.sentiment_score:+.2f}

[查看详情]({alert.news_id})
"""
            
            payload = {
                'msgtype': 'markdown',
                'markdown': {
                    'title': f"{severity_titles.get(alert.severity, '突发')}：{alert.title[:50]}",
                    'text': markdown
                }
            }
            
            # 发送请求
            resp = requests.post(
                self.dingtalk_webhook,
                json=payload,
                timeout=10
            )
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get('errcode') == 0:
                    logger.info(f"突发新闻推送成功：{alert.title[:50]}...")
                    return True
                else:
                    logger.error(f"推送失败：{result}")
                    return False
            else:
                logger.error(f"推送失败：{resp.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"推送异常：{e}")
            return False


# 全局单例
_detector: Optional[BreakingNewsDetector] = None
_processor: Optional[FastTrackProcessor] = None


def get_breaking_news_detector() -> BreakingNewsDetector:
    """获取突发新闻检测器单例"""
    global _detector
    if _detector is None:
        _detector = BreakingNewsDetector()
    return _detector


def get_fast_track_processor(webhook: str = None,
                            secret: str = None) -> FastTrackProcessor:
    """获取快速通道处理器单例"""
    global _processor
    if _processor is None:
        _processor = FastTrackProcessor(webhook, secret)
    return _processor


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    from app.news_push.collector import NewsItem
    
    detector = get_breaking_news_detector()
    
    # 测试新闻
    test_news = NewsItem(
        title="突发：腾讯发布超预期财报，净利润增长 50%",
        content="腾讯控股今日发布 2024 年 Q1 财报，净利润同比增长 50%，超出市场预期...",
        source='cls',
        category='flash',
        related_stocks=['HK.00700'],
    )
    
    alert = detector.detect(test_news)
    
    if alert:
        print(f"\n检测到突发新闻:")
        print(f"  标题：{alert.title}")
        print(f"  优先级：{alert.priority}")
        print(f"  严重程度：{alert.severity}")
        print(f"  相关股票：{alert.related_stocks}")
        print(f"  关键词：{alert.keywords}")
        print(f"\n统计：{detector.get_statistics()}")
    else:
        print("未检测到突发新闻")
