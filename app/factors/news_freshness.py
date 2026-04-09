"""
新闻时效性管理器 (News Freshness Manager)

功能:
1. 新闻新鲜度评分 - 越新的新闻权重越高
2. 时间衰减机制 - 旧新闻影响力逐渐降低
3. 时效性过滤 - 只使用有效时间内的新闻
4. 重大事件即时标记 - 突发新闻特殊处理

创建时间: 2026-03-17
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import math

logger = logging.getLogger("ftseek.news_freshness")
logger.propagate = False  # 防止日志重复


@dataclass
class FreshnessScore:
    """新鲜度评分结果"""
    news_id: str
    published_at: datetime
    age_minutes: int
    freshness_weight: float      # 新鲜度权重 0-1
    decay_factor: float          # 衰减因子
    is_actionable: bool          # 是否可用于交易决策
    category: str                # 时效性分类: hot/fresh/normal/stale/expired


class NewsFreshnessManager:
    """
    新闻时效性管理器
    
    核心机制:
    1. 时间衰减: 新闻影响力随时间指数衰减
    2. 新鲜度权重: 越新的新闻在情绪计算中权重越高
    3. 时效性过滤: 超过有效时间的新闻不参与决策
    4. 重大事件标记: 特殊新闻保持更长时间的影响力
    """
    
    # 时效性配置
    DEFAULT_CONFIG = {
        # 新闻有效期 (分钟)
        'hot_threshold': 30,          # 热点新闻: 30分钟内
        'fresh_threshold': 60,        # 新鲜新闻: 60分钟内
        'normal_threshold': 180,      # 正常新闻: 3小时内
        'stale_threshold': 360,       # 陈旧新闻: 6小时内
        'expiry_threshold': 1440,     # 过期时间: 24小时
        
        # 权重衰减参数
        'decay_rate': 0.5,            # 衰减率 (越大衰减越快)
        'min_weight': 0.1,            # 最小权重
        
        # 重大事件配置
        'major_event_keywords': [
            '财报', '业绩', '盈利', '亏损', '暴雷',
            '收购', '重组', 'IPO', '退市', '停牌',
            '监管', '调查', '处罚', '诉讼',
            '涨跌停', '暴涨', '暴跌', '熔断',
        ],
        'major_event_multiplier': 2.0,  # 重大事件权重倍增
        'major_event_expiry': 720,       # 重大事件有效期延长到 12 小时
    }
    
    def __init__(self, config: dict = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        logger.info(
            f"新闻时效性管理器已初始化 | "
            f"热点<{self.config['hot_threshold']}min | "
            f"新鲜<{self.config['fresh_threshold']}min | "
            f"过期>{self.config['expiry_threshold']}min"
        )
    
    def calculate_freshness(self, published_at: datetime, 
                           is_major_event: bool = False) -> FreshnessScore:
        """
        计算新闻新鲜度
        
        Args:
            published_at: 新闻发布时间
            is_major_event: 是否为重大事件
        
        Returns:
            FreshnessScore: 新鲜度评分结果
        """
        # 统一时区：将 published_at 转换为本地 naive datetime
        # 避免 UTC published_at 与本地 datetime.now() 混合比较导致年龄虚高
        now = datetime.now()
        if hasattr(published_at, 'tzinfo') and published_at.tzinfo is not None:
            # aware datetime → 转为 UTC naive → 加 8 小时转 CST
            import calendar
            utc_ts = calendar.timegm(published_at.utctimetuple())
            published_at = datetime.fromtimestamp(utc_ts)
        age = now - published_at
        age_minutes = int(age.total_seconds() / 60)
        # 防止因未来时间或解析误差导致负数
        if age_minutes < 0:
            age_minutes = 0
        
        # 计算权重 (指数衰减)
        if age_minutes <= 0:
            freshness_weight = 1.0
        else:
            # 指数衰减: weight = e^(-decay_rate * age / threshold)
            threshold = self.config['fresh_threshold']
            freshness_weight = math.exp(-self.config['decay_rate'] * age_minutes / threshold)
            freshness_weight = max(self.config['min_weight'], freshness_weight)
        
        # 重大事件权重倍增
        if is_major_event:
            freshness_weight *= self.config['major_event_multiplier']
            freshness_weight = min(1.0, freshness_weight)  # 不超过 1.0
        
        # 衰减因子
        decay_factor = freshness_weight
        
        # 分类
        expiry = self.config['major_event_expiry'] if is_major_event else self.config['expiry_threshold']
        
        if age_minutes <= self.config['hot_threshold']:
            category = 'hot'
            is_actionable = True
        elif age_minutes <= self.config['fresh_threshold']:
            category = 'fresh'
            is_actionable = True
        elif age_minutes <= self.config['normal_threshold']:
            category = 'normal'
            is_actionable = True
        elif age_minutes <= self.config['stale_threshold']:
            category = 'stale'
            is_actionable = True
        elif age_minutes <= expiry:
            # stale 区间 (stale_threshold ~ expiry)：普通新闻仍可用，权重低
            category = 'stale'
            is_actionable = True
        else:
            category = 'expired'
            is_actionable = False
        
        return FreshnessScore(
            news_id='',
            published_at=published_at,
            age_minutes=age_minutes,
            freshness_weight=freshness_weight,
            decay_factor=decay_factor,
            is_actionable=is_actionable,
            category=category
        )
    
    def is_major_event(self, title: str, content: str = '') -> bool:
        """
        判断是否为重大事件
        
        Args:
            title: 新闻标题
            content: 新闻内容
        
        Returns:
            bool: 是否为重大事件
        """
        text = f"{title} {content}".lower()
        keywords = self.config['major_event_keywords']
        
        for keyword in keywords:
            if keyword.lower() in text:
                return True
        return False
    
    def filter_actionable_news(self, news_list: List[dict], 
                               max_age_minutes: int = None) -> List[Tuple[dict, FreshnessScore]]:
        """
        过滤可用的新闻 (未过期)
        
        Args:
            news_list: 新闻列表
            max_age_minutes: 最大年龄 (分钟)，默认使用配置的过期时间
        
        Returns:
            List[(news, freshness)]: 可用的新闻及其新鲜度评分
        """
        max_age = max_age_minutes or self.config['expiry_threshold']
        actionable = []
        
        for news in news_list:
            # 获取发布时间
            published_at_str = news.get('published_at') or news.get('timestamp')
            if not published_at_str:
                continue
            
            try:
                # 解析时间
                if isinstance(published_at_str, str):
                    # 尝试多种格式
                    published_at_str = published_at_str.replace('Z', '+00:00')
                    
                    # RSS 格式: Fri, 13 Mar 2026 15:00:00 GMT
                    if 'GMT' in published_at_str or ', ' in published_at_str:
                        from email.utils import parsedate_to_datetime
                        try:
                            dt_aware = parsedate_to_datetime(published_at_str)
                            # GMT(UTC) → 本地时间(CST = UTC+8)
                            import calendar
                            utc_ts = calendar.timegm(dt_aware.utctimetuple())
                            published_at = datetime.fromtimestamp(utc_ts)
                        except (ValueError, TypeError):
                            # 手动解析
                            import re
                            match = re.match(r'\w+, (\d+) (\w+) (\d+) (\d+):(\d+):(\d+)', published_at_str)
                            if match:
                                day, mon, year, h, m, s = match.groups()
                                months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                                         'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
                                published_at = datetime(int(year), months.get(mon, 1), int(day), int(h), int(m), int(s))
                            else:
                                continue
                    elif '+' in published_at_str:
                        from dateutil.parser import parse as parse_date
                        try:
                            dt = parse_date(published_at_str)
                            # aware → 本地时间
                            import calendar
                            utc_ts = calendar.timegm(dt.utctimetuple())
                            published_at = datetime.fromtimestamp(utc_ts)
                        except (ValueError, TypeError):
                            published_at = datetime.fromisoformat(published_at_str.split('+')[0])
                    else:
                        published_at = datetime.fromisoformat(published_at_str)
                else:
                    published_at = published_at_str
            except Exception as e:
                logger.debug(f"时间解析失败: {published_at_str}, {e}")
                continue
            
            # 判断是否重大事件
            is_major = self.is_major_event(
                news.get('title', ''),
                news.get('content', news.get('summary', ''))
            )
            
            # 计算新鲜度
            freshness = self.calculate_freshness(published_at, is_major)
            
            # 过滤
            if freshness.is_actionable:
                actionable.append((news, freshness))
        
        # 按新鲜度排序 (最新的在前)
        actionable.sort(key=lambda x: x[1].age_minutes)
        
        logger.info(
            f"新闻过滤: {len(news_list)} → {len(actionable)} 条可用 | "
            f"热点: {sum(1 for _, f in actionable if f.category == 'hot')} | "
            f"新鲜: {sum(1 for _, f in actionable if f.category == 'fresh')}"
        )
        
        return actionable
    
    def calculate_weighted_sentiment(self, 
                                     news_sentiments: List[Tuple[dict, float, FreshnessScore]]) -> dict:
        """
        计算加权情绪得分 (考虑时效性)
        
        Args:
            news_sentiments: [(news, sentiment_score, freshness), ...]
        
        Returns:
            {
                'weighted_score': 0.5,
                'raw_score': 0.3,
                'hot_count': 5,
                'fresh_count': 10,
                'total_count': 20,
                'major_events': 2,
                'confidence': 0.8
            }
        """
        if not news_sentiments:
            return {
                'weighted_score': 0.0,
                'raw_score': 0.0,
                'hot_count': 0,
                'fresh_count': 0,
                'total_count': 0,
                'major_events': 0,
                'confidence': 0.0
            }
        
        weighted_sum = 0.0
        weight_sum = 0.0
        raw_sum = 0.0
        hot_count = 0
        fresh_count = 0
        major_events = 0
        
        for news, sentiment, freshness in news_sentiments:
            weight = freshness.freshness_weight
            weighted_sum += sentiment * weight
            weight_sum += weight
            raw_sum += sentiment
            
            if freshness.category == 'hot':
                hot_count += 1
            elif freshness.category == 'fresh':
                fresh_count += 1
            
            # 检查重大事件
            if self.is_major_event(news.get('title', '')):
                major_events += 1
        
        # 加权平均
        weighted_score = weighted_sum / weight_sum if weight_sum > 0 else 0.0
        raw_score = raw_sum / len(news_sentiments)
        
        # 置信度计算 (基于新闻数量和新鲜度)
        total_weight = weight_sum / len(news_sentiments) if news_sentiments else 0
        confidence = min(1.0, total_weight * (1 + 0.1 * min(len(news_sentiments), 10)))
        
        return {
            'weighted_score': round(weighted_score, 3),
            'raw_score': round(raw_score, 3),
            'hot_count': hot_count,
            'fresh_count': fresh_count,
            'total_count': len(news_sentiments),
            'major_events': major_events,
            'confidence': round(confidence, 2)
        }
    
    def should_trade_on_news(self, sentiment_result: dict, 
                             min_confidence: float = 0.5,
                             min_hot_count: int = 1) -> Tuple[bool, str]:
        """
        判断是否应该基于新闻进行交易
        
        Args:
            sentiment_result: 情绪分析结果
            min_confidence: 最小置信度
            min_hot_count: 最少热点新闻数量
        
        Returns:
            (should_trade, reason): 是否交易及原因
        """
        confidence = sentiment_result.get('confidence', 0)
        hot_count = sentiment_result.get('hot_count', 0)
        fresh_count = sentiment_result.get('fresh_count', 0)
        major_events = sentiment_result.get('major_events', 0)
        weighted_score = sentiment_result.get('weighted_score', 0)
        
        # 检查是否有足够的新鲜新闻
        if hot_count < min_hot_count and fresh_count < 3:
            return False, f"新鲜新闻不足: 热点{hot_count}条, 新鲜{fresh_count}条"
        
        # 检查置信度
        if confidence < min_confidence:
            return False, f"置信度不足: {confidence:.2f} < {min_confidence}"
        
        # 检查情绪强度
        if abs(weighted_score) < 0.3:
            return False, f"情绪强度不足: {weighted_score:.2f}"
        
        # 重大事件可以放宽条件
        if major_events > 0 and abs(weighted_score) > 0.5:
            return True, f"重大事件触发: {major_events}个重大事件, 情绪={weighted_score:.2f}"
        
        # 正常条件
        if hot_count >= 2 or fresh_count >= 5:
            return True, f"时效性验证通过: 热点{hot_count}条, 新鲜{fresh_count}条"
        
        return False, "时效性验证未通过"
    
    def get_freshness_summary(self, news_list: List[dict]) -> dict:
        """获取新闻时效性摘要"""
        actionable = self.filter_actionable_news(news_list)
        
        categories = {'hot': 0, 'fresh': 0, 'normal': 0, 'stale': 0, 'expired': 0}
        major_count = 0
        
        for news, freshness in actionable:
            categories[freshness.category] = categories.get(freshness.category, 0) + 1
            if self.is_major_event(news.get('title', '')):
                major_count += 1
        
        return {
            'total_news': len(news_list),
            'actionable_news': len(actionable),
            'categories': categories,
            'major_events': major_count,
            'oldest_age_minutes': actionable[-1][1].age_minutes if actionable else 0,
            'newest_age_minutes': actionable[0][1].age_minutes if actionable else 0,
        }


# 全局单例
_freshness_manager = None

def get_freshness_manager(config: dict = None) -> NewsFreshnessManager:
    """获取时效性管理器单例"""
    global _freshness_manager
    if _freshness_manager is None:
        _freshness_manager = NewsFreshnessManager(config)
    return _freshness_manager