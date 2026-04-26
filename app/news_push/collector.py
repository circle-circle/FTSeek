#!/usr/bin/env python3
"""
多源新闻采集器 (增强版 - Phase 3)

功能增强:
- 动态缓存 TTL (基于优先级和市场波动率)
- 突发新闻快速通道
- 信息衰减支持
"""
import os
import sys
import time
import uuid
import hashlib
import json
import logging
import requests
import feedparser
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.news_push.config import (
    RSS_FEEDS, SOURCE_PRIORITY, CATEGORY_ENABLED,
    WATCH_STOCKS, STOCK_ALIASES, CACHE_CONFIG, CACHE_DIR
)

logger = logging.getLogger("ftseek.news_push.collector")
logger.propagate = False

# 突发新闻关键词 (P1 优先级)
BREAKING_NEWS_KEYWORDS = [
    '突发', '紧急', '公告', '财报', '制裁', '收购', '合并',
    'breaking', 'urgent', 'announcement', 'earnings', 'sanction',
    'acquisition', 'merger', 'ipo', 'delist', 'investigation',
]


@dataclass
class NewsItem:
    """新闻条目"""
    id: str = ""
    title: str = ""
    content: str = ""
    summary: str = ""
    source: str = ""
    category: str = ""
    url: str = ""
    published_at: str = ""
    fetched_at: str = ""
    priority: int = 5
    related_stocks: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    sentiment_score: float = 0.0
    raw_data: Dict = field(default_factory=dict)
    
    # Phase 3 新增属性
    is_breaking: bool = False         # 是否突发新闻
    decay_factor: float = 1.0         # 衰减因子
    age_minutes: int = 0              # 发布时间距今 (分钟)
    
    # 链路追踪
    trace_id: str = ""                # TraceID，贯穿新闻信号全链路

    # Pipeline 新增属性 (2026-04-12)
    quality_score: float = 0.0        # 新闻质量评分 (0.0-1.0)

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now().isoformat()
        if not self.id:
            self.id = self._generate_id()
        # 生成 trace_id（新闻信号链路追踪）
        if not self.trace_id:
            self.trace_id = f"NEWS-{uuid.uuid4().hex[:12].upper()}"
        # 检测是否为突发新闻
        if not self.is_breaking:
            self.is_breaking = self._check_breaking()
        # 计算年龄
        if self.published_at:
            try:
                pub_time = datetime.fromisoformat(self.published_at.replace('Z', '+00:00'))
                now = datetime.now(pub_time.tzinfo) if pub_time.tzinfo else datetime.now()
                self.age_minutes = int((now - pub_time).total_seconds() / 60)
            except (ValueError, TypeError, AttributeError) as e:
                logger.debug(f"新闻年龄计算失败：{e}")
                self.age_minutes = 0
    
    def _generate_id(self) -> str:
        """生成唯一 ID"""
        text = f"{self.title}{self.url}{self.published_at}"
        return hashlib.md5(text.encode('utf-8')).hexdigest()[:16]
    
    def _check_breaking(self) -> bool:
        """检查是否为突发新闻"""
        text = f"{self.title} {self.content}".lower()
        return any(kw.lower() in text for kw in BREAKING_NEWS_KEYWORDS)
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'NewsItem':
        return cls(**data)


class ServiceClient:
    """
    统一 HTTP 客户端 (增强版)
    功能：缓存、熔断、请求去重、超时重试、动态 TTL
    """

    def __init__(self, cache_dir: Path = None, ttl: int = 3600,
                 use_dynamic_ttl: bool = True, vix_monitor=None):
        """
        初始化 ServiceClient

        Args:
            cache_dir: 缓存目录
            ttl: 基础缓存 TTL (秒)
            use_dynamic_ttl: 是否使用动态 TTL
            vix_monitor: VIX 监控器实例 (用于动态 TTL)
        """
        self.cache_dir = cache_dir or CACHE_DIR
        self.base_ttl = ttl
        self.use_dynamic_ttl = use_dynamic_ttl
        self.vix_monitor = vix_monitor

        # 动态 TTL 配置
        self.priority_ttl_factors = {
            1: 0.1,   # 优先级 1 (最高): TTL 10%
            2: 0.3,   # 优先级 2: TTL 30%
            3: 0.5,   # 优先级 3: TTL 50%
            4: 0.7,   # 优先级 4: TTL 70%
            5: 1.0,   # 优先级 5 (最低): TTL 100%
        }

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/xml, application/xml, */*',
        })

        # 熔断器状态
        self._circuit_breaker: Dict[str, Dict] = {}
        self._request_cache: Dict[str, Dict] = {}  # 内存缓存

        # 去重记录 (URL -> timestamp)
        self._seen_urls: Dict[str, float] = {}

    def _get_dynamic_ttl(self, url: str, priority: int = 5) -> int:
        """
        计算动态 TTL

        Args:
            url: 请求 URL
            priority: 新闻优先级 (1-5)

        Returns:
            TTL (秒)
        """
        # 基础 TTL
        ttl = self.base_ttl

        # 优先级因子
        priority_factor = self.priority_ttl_factors.get(priority, 1.0)
        ttl *= priority_factor

        # 波动率因子 (如果有 VIX 监控器)
        if self.use_dynamic_ttl and self.vix_monitor:
            vol_factor = self.vix_monitor.get_cache_ttl_multiplier()
            ttl *= vol_factor

        # 突发新闻 TTL 大幅降低
        if any(kw in url.lower() for kw in ['flash', 'breaking', 'urgent']):
            ttl *= 0.1

        return max(60, int(ttl))  # 最少 60 秒

    def _get_cache_path(self, url: str) -> Path:
        """获取缓存文件路径"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return self.cache_dir / f"{url_hash}.json"

    def _load_cache(self, url: str, priority: int = 5) -> Optional[Dict]:
        """加载缓存"""
        # 先查内存缓存
        if url in self._request_cache:
            cached = self._request_cache[url]
            dynamic_ttl = self._get_dynamic_ttl(url, priority)
            if time.time() - cached.get('timestamp', 0) < dynamic_ttl:
                return cached.get('data')

        # 再查文件缓存
        cache_path = self._get_cache_path(url)
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                    dynamic_ttl = self._get_dynamic_ttl(url, priority)
                    if time.time() - cached.get('timestamp', 0) < dynamic_ttl:
                        # 加载到内存缓存
                        self._request_cache[url] = cached
                        return cached.get('data')
            except Exception as e:
                logger.warning(f"读取缓存失败：{e}")
        return None

    def _save_cache(self, url: str, data: Any, priority: int = 5):
        """保存缓存"""
        dynamic_ttl = self._get_dynamic_ttl(url, priority)
        cached = {
            'timestamp': time.time(),
            'data': data,
            'ttl': dynamic_ttl,  # 记录 TTL
        }

        # 保存内存缓存
        self._request_cache[url] = cached

        # 保存文件缓存
        cache_path = self._get_cache_path(url)
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cached, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"写入缓存失败：{e}")

    def _is_circuit_open(self, key: str) -> bool:
        """检查熔断器是否打开"""
        if key not in self._circuit_breaker:
            return False

        cb = self._circuit_breaker[key]
        if cb.get('state') == 'open':
            # 检查是否可以进入半开状态
            if time.time() - cb.get('opened_at', 0) > 60:  # 60 秒后尝试恢复
                cb['state'] = 'half-open'
                logger.info(f"熔断器半开：{key}")
                return False
            return True
        return False

    def _record_failure(self, key: str):
        """记录失败"""
        if key not in self._circuit_breaker:
            self._circuit_breaker[key] = {'failures': 0, 'state': 'closed'}

        cb = self._circuit_breaker[key]
        cb['failures'] = cb.get('failures', 0) + 1

        if cb['failures'] >= 5:  # 连续 5 次失败，打开熔断器
            cb['state'] = 'open'
            cb['opened_at'] = time.time()
            logger.warning(f"熔断器打开：{key} (连续失败{cb['failures']}次)")

    def _record_success(self, key: str):
        """记录成功"""
        if key in self._circuit_breaker:
            self._circuit_breaker[key] = {'failures': 0, 'state': 'closed'}

    def get(self, url: str, timeout: int = 10,
            use_cache: bool = True, headers: Dict = None,
            priority: int = 5, is_breaking: bool = False) -> Optional[Dict]:
        """
        HTTP GET 请求 (带缓存、熔断、去重、动态 TTL)

        Args:
            url: 请求 URL
            timeout: 超时时间 (秒)
            use_cache: 是否使用缓存
            headers: 自定义请求头
            priority: 新闻优先级 (1-5)
            is_breaking: 是否突发新闻

        Returns:
            响应数据 (JSON 或 text)
        """
        # 突发新闻跳过缓存
        if is_breaking:
            use_cache = False
            logger.debug(f"突发新闻跳过缓存：{url}")

        # 检查去重
        now = time.time()
        if url in self._seen_urls:
            if now - self._seen_urls[url] < 60:  # 60 秒内不重复请求
                logger.debug(f"请求去重：{url}")
                return self._load_cache(url, priority)
        self._seen_urls[url] = now

        # 检查缓存
        if use_cache:
            cached = self._load_cache(url, priority)
            if cached:
                logger.debug(f"缓存命中：{url}")
                return cached

        # 检查熔断器
        key = hashlib.md5(url.encode()).hexdigest()[:8]
        if self._is_circuit_open(key):
            logger.warning(f"熔断器打开，跳过请求：{url}")
            return None

        # 发起请求
        try:
            resp = self.session.get(url, timeout=timeout, headers=headers or {})

            if resp.status_code == 200:
                # 尝试解析 JSON
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError):
                    data = {'text': resp.text}

                # 保存缓存
                if use_cache:
                    self._save_cache(url, data, priority)

                # 重置熔断器
                self._record_success(key)
                logger.debug(f"请求成功：{url} ({resp.status_code})")
                return data
            else:
                logger.warning(f"请求失败：{url} ({resp.status_code})")
                self._record_failure(key)
                return None

        except requests.exceptions.Timeout:
            logger.error(f"请求超时：{url}")
            self._record_failure(key)
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"请求异常：{url} - {e}")
            self._record_failure(key)
            return None

    def close(self):
        """关闭会话"""
        self.session.close()


class NewsCollector:
    """
    多源新闻采集器 (增强版)
    """

    # 情绪分析关键词
    POSITIVE_KEYWORDS = [
        '盈利', '增长', '利好', '上涨', '突破', '超预期', '创新高',
        '收购', '重组', '分红', '回购', '中标', '合作', '扩张',
        'earnings', 'beat', 'surge', 'rally', 'breakthrough', 'outperform',
        'acquisition', 'dividend', 'partnership', 'expansion', 'bullish'
    ]

    NEGATIVE_KEYWORDS = [
        '亏损', '下滑', '利空', '下跌', '暴跌', '不及预期', '新低',
        '减持', '退市', '调查', '诉讼', '违约', '风险', '警告',
        'loss', 'decline', 'drop', 'plunge', 'miss', 'lawsuit', 'risk',
        'bearish', 'warning', 'investigation', 'lawsuit'
    ]

    def __init__(self, config: Dict = None, vix_monitor=None):
        """
        初始化采集器

        Args:
            config: 配置字典
            vix_monitor: VIX 监控器实例
        """
        self.config = config or {}
        self.vix_monitor = vix_monitor

        # 初始化 ServiceClient (带动态 TTL)
        self.client = ServiceClient(
            ttl=CACHE_CONFIG['ttl_seconds'],
            use_dynamic_ttl=True,
            vix_monitor=vix_monitor
        )

        # 清理过期缓存
        self._cleanup_cache()

        logger.info("✅ 新闻采集器已初始化 (Phase 3 增强版)")

    def _cleanup_cache(self):
        """清理过期缓存"""
        try:
            now = time.time()
            for cache_file in self.client.cache_dir.glob("*.json"):
                try:
                    mtime = cache_file.stat().st_mtime
                    if now - mtime > CACHE_CONFIG['ttl_seconds'] * 2:
                        cache_file.unlink()
                except (OSError, IOError):
                    pass
        except Exception as e:
            logger.warning(f"清理缓存失败：{e}")

    def _analyze_sentiment(self, text: str) -> tuple:
        """
        情绪分析

        Returns:
            (score, confidence)  score: -1~1, confidence: 0~1
        """
        text_lower = text.lower()

        positive_count = sum(1 for kw in self.POSITIVE_KEYWORDS if kw in text_lower)
        negative_count = sum(1 for kw in self.NEGATIVE_KEYWORDS if kw in text_lower)

        total = positive_count + negative_count

        if total == 0:
            return 0.0, 0.0

        score = (positive_count - negative_count) / total
        confidence = min(1.0, total / 10.0)

        return score, confidence

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        text_lower = text.lower()
        keywords = []

        for kw in self.POSITIVE_KEYWORDS + self.NEGATIVE_KEYWORDS:
            if kw in text_lower:
                keywords.append(kw)

        return list(set(keywords))[:10]

    def _match_stocks(self, text: str) -> List[str]:
        """
        匹配新闻中提到的股票

        Returns:
            匹配的股票代码列表
        """
        matched = []

        for code, aliases in STOCK_ALIASES.items():
            for alias in aliases:
                if alias.lower() in text.lower():
                    matched.append(code)
                    break

        return list(set(matched))

    def _parse_rss_feed(self, url: str, category: str,
                       source: str = None) -> List[NewsItem]:
        """解析 RSS Feed"""
        try:
            data = self.client.get(url, use_cache=True)
            if not data:
                return []

            # 处理不同的 RSS 格式
            if 'text' in data:
                feed = feedparser.parse(data['text'])
            else:
                # 已经是解析后的数据
                feed = data

            news_list = []
            entries = feed.get('entries', [])

            for entry in entries:
                title = entry.get('title', '')
                content = entry.get('summary', entry.get('description', ''))
                published = entry.get('published', '')

                if not title:
                    continue

                # 生成新闻条目
                news = NewsItem(
                    title=title,
                    content=content,
                    summary=content[:200] if len(content) > 200 else content,
                    source=source or 'rss',
                    category=category,
                    url=entry.get('link', ''),
                    published_at=published,
                    priority=SOURCE_PRIORITY.get(source, 5),
                )

                # 情绪分析
                text = title + ' ' + content
                news.sentiment_score, news.sentiment_confidence = self._analyze_sentiment(text)
                news.keywords = self._extract_keywords(text)
                news.related_stocks = self._match_stocks(text)

                news_list.append(news)

            logger.debug(f"RSS 解析成功：{url} -> {len(news_list)} 条")
            return news_list

        except Exception as e:
            logger.error(f"RSS 解析失败：{url} - {e}")
            return []

    def fetch_cls_telegraph(self) -> List[NewsItem]:
        """获取财联社电报"""
        url = "https://www.cls.cn/api/telegraph"
        try:
            # 快讯不使用缓存
            data = self.client.get(url, use_cache=False, priority=1)
            if not data:
                return []

            news_list = []
            items = data.get('data', {}).get('list', [])

            for item in items:
                title = item.get('title', '')
                content = item.get('content', '')

                if not title:
                    continue

                news = NewsItem(
                    title=title,
                    content=content,
                    source='cls',
                    category='flash',
                    url=f"https://www.cls.cn/detail/{item.get('id', '')}",
                    published_at=datetime.fromtimestamp(
                        item.get('ctime', 0)
                    ).isoformat() if item.get('ctime') else '',
                    priority=SOURCE_PRIORITY.get('cls', 1),
                    is_breaking=True,  # 快讯标记为突发
                )

                text = title + ' ' + content
                news.sentiment_score, news.sentiment_confidence = self._analyze_sentiment(text)
                news.keywords = self._extract_keywords(text)
                news.related_stocks = self._match_stocks(text)

                news_list.append(news)

            logger.info(f"✅ 财联社电报：获取 {len(news_list)} 条")
            return news_list[:20]  # 最多 20 条

        except Exception as e:
            logger.error(f"财联社电报获取失败：{e}")
            return []

    def fetch_blockbeats(self) -> List[NewsItem]:
        """获取 BlockBeats 快讯"""
        url = "https://www.blockbeats.com/api/news/flash"
        try:
            data = self.client.get(url, use_cache=False, priority=2)
            if not data:
                return []

            news_list = []
            items = data.get('data', [])

            for item in items:
                title = item.get('content', '')

                if not title:
                    continue

                news = NewsItem(
                    title=title,
                    content=title,
                    source='blockbeats',
                    category='flash',
                    url=f"https://www.blockbeats.com/news/{item.get('id', '')}",
                    published_at=datetime.fromtimestamp(
                        item.get('ctime', 0)
                    ).isoformat() if item.get('ctime') else '',
                    priority=SOURCE_PRIORITY.get('blockbeats', 2),
                    is_breaking=True,
                )

                news.sentiment_score, news.sentiment_confidence = self._analyze_sentiment(title)
                news.keywords = self._extract_keywords(title)
                news.related_stocks = self._match_stocks(title)

                news_list.append(news)

            logger.info(f"✅ BlockBeats 快讯：获取 {len(news_list)} 条")
            return news_list[:20]

        except Exception as e:
            logger.error(f"BlockBeats 快讯获取失败：{e}")
            return []

    def fetch_rss_feeds(self) -> List[NewsItem]:
        """获取所有启用的 RSS 源"""
        all_news = []

        for category, feeds in RSS_FEEDS.items():
            if not CATEGORY_ENABLED.get(category, False):
                logger.debug(f"跳过未启用的分类：{category}")
                continue

            for feed_url in feeds:
                source = 'rss'
                if 'wallstcn' in feed_url:
                    source = 'wallstreetcn'
                elif 'sina' in feed_url:
                    source = 'sina'
                elif '100727362' in feed_url:
                    source = 'cnbc_asia'   # CNBC Asia (亚太专项)
                elif 'cnbc' in feed_url:
                    source = 'cnbc'
                elif 'dj' in feed_url:
                    source = 'wsj'
                elif 'federalreserve' in feed_url:
                    source = 'fed'
                elif 'sec.gov' in feed_url:
                    source = 'sec'
                elif 'reuters' in feed_url:
                    source = 'reuters'
                elif 'hkex' in feed_url:
                    source = 'hkex'
                elif 'scmp' in feed_url:
                    source = 'scmp'
                elif 'eastmoney' in feed_url:
                    source = 'eastmoney'
                elif 'aastocks' in feed_url:
                    source = 'aastocks'
                elif 'rsshub' in feed_url and 'futunn' in feed_url:
                    source = 'futu_rsshub'  # 富途快讯 (RSSHub) - 2026-04-12

                news_list = self._parse_rss_feed(feed_url, category, source)
                all_news.extend(news_list)

                # 避免请求过快
                time.sleep(0.5)

        logger.info(f"✅ RSS 源：获取 {len(all_news)} 条")
        return all_news

    def fetch_all(self) -> List[NewsItem]:
        """
        从所有源采集新闻

        Returns:
            新闻列表 (按优先级和时间排序)
        """
        all_news = []

        logger.info("\n📰 开始采集新闻...")
        logger.info("=" * 60)

        # 1. RSS 源 (稳定源 + 港股源)
        rss_news = self.fetch_rss_feeds()
        all_news.extend(rss_news)

        # 3. 去重 (基于标题)
        seen = set()
        unique_news = []
        for news in all_news:
            if news.title not in seen:
                seen.add(news.title)
                unique_news.append(news)

        # 4. 排序 (优先级高的在前，时间新的在前)
        unique_news.sort(
            key=lambda x: (x.priority, x.published_at or ''),
            reverse=False
        )

        logger.info("=" * 60)
        logger.info(f"📊 采集完成：共 {len(unique_news)} 条新闻")

        # 统计
        by_source = {}
        for news in unique_news:
            source = news.source
            by_source[source] = by_source.get(source, 0) + 1

        for source, count in sorted(by_source.items(), key=lambda x: x[1], reverse=True):
            logger.info(f"   {source}: {count} 条")

        # 输出前 3 条 trace_id 为采样日志
        for news in unique_news[:3]:
            logger.debug(
                f"[{news.trace_id}] 新闻采集：{news.title[:40]}... "
                f"| 源:{news.source} | 优先级:{news.priority} "
                f"| 突发:{'yes' if news.is_breaking else 'no'}"
            )

        return unique_news

    def close(self):
        """关闭采集器"""
        self.client.close()


# 全局单例
_collector: Optional[NewsCollector] = None


def get_collector(config: Dict = None, vix_monitor=None) -> NewsCollector:
    """获取采集器单例"""
    global _collector
    if _collector is None:
        _collector = NewsCollector(config, vix_monitor)
    return _collector


if __name__ == "__main__":
    # 测试运行
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 测试 VIX 集成
    try:
        from app.risk.vix_monitor import get_vix_monitor
        vix = get_vix_monitor()
        print(f"VIX 状态：{vix.get_state()}")
        collector = get_collector(vix_monitor=vix)
    except Exception as e:
        print(f"VIX 集成失败 (使用默认配置): {e}")
        collector = get_collector()

    news_list = collector.fetch_all()

    print(f"\n📋 最新 5 条新闻:")
    for news in news_list[:5]:
        print(f"\n- {news.title}")
        print(f"  来源：{news.source} | 优先级：{news.priority}")
        print(f"  相关股票：{news.related_stocks or '无'}")
        print(f"  情绪：{news.sentiment_score:+.2f}")
        print(f"  突发新闻：{'是' if news.is_breaking else '否'}")
        print(f"  发布时间：{news.age_minutes}分钟前")

    collector.close()


# 别名函数
def get_news_collector(config: Dict = None, vix_monitor=None) -> NewsCollector:
    """获取新闻采集器单例 (别名)"""
    return get_collector(config, vix_monitor)
