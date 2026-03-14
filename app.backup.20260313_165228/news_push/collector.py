#!/usr/bin/env python3
"""
多源新闻采集器

基于 Situation-aggregation-monitoring 项目改造
保留 RSS 解析、API 调用和网页抓取能力
使用 ServiceClient 统一处理缓存、熔断和请求去重
"""
import os
import sys
import time
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
    
    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now().isoformat()
        if not self.id:
            self.id = self._generate_id()
    
    def _generate_id(self) -> str:
        """生成唯一 ID"""
        text = f"{self.title}{self.url}{self.published_at}"
        return hashlib.md5(text.encode('utf-8')).hexdigest()[:16]
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'NewsItem':
        return cls(**data)


class ServiceClient:
    """
    统一 HTTP 客户端
    功能：缓存、熔断、请求去重、超时重试
    """
    
    def __init__(self, cache_dir: Path = None, ttl: int = 3600):
        self.cache_dir = cache_dir or CACHE_DIR
        self.ttl = ttl
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
    
    def _get_cache_path(self, url: str) -> Path:
        """获取缓存文件路径"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return self.cache_dir / f"{url_hash}.json"
    
    def _load_cache(self, url: str) -> Optional[Dict]:
        """加载缓存"""
        # 先查内存缓存
        if url in self._request_cache:
            cached = self._request_cache[url]
            if time.time() - cached.get('timestamp', 0) < self.ttl:
                return cached.get('data')
        
        # 再查文件缓存
        cache_path = self._get_cache_path(url)
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                    if time.time() - cached.get('timestamp', 0) < self.ttl:
                        # 加载到内存缓存
                        self._request_cache[url] = cached
                        return cached.get('data')
            except Exception as e:
                logger.warning(f"读取缓存失败：{e}")
        return None
    
    def _save_cache(self, url: str, data: Any):
        """保存缓存"""
        cached = {
            'timestamp': time.time(),
            'data': data
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
            use_cache: bool = True, headers: Dict = None) -> Optional[Dict]:
        """
        HTTP GET 请求 (带缓存、熔断、去重)
        
        Args:
            url: 请求 URL
            timeout: 超时时间 (秒)
            use_cache: 是否使用缓存
            headers: 自定义请求头
            
        Returns:
            响应数据 (JSON 或 text)
        """
        # 检查去重
        now = time.time()
        if url in self._seen_urls:
            if now - self._seen_urls[url] < 60:  # 60 秒内不重复请求
                logger.debug(f"请求去重：{url}")
                return self._load_cache(url)
        self._seen_urls[url] = now
        
        # 检查缓存
        if use_cache:
            cached = self._load_cache(url)
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
                except:
                    data = {'text': resp.text}
                
                # 保存缓存
                if use_cache:
                    self._save_cache(url, data)
                
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
    多源新闻采集器
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
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.client = ServiceClient(ttl=CACHE_CONFIG['ttl_seconds'])
        
        # 清理过期缓存
        self._cleanup_cache()
        
        logger.info("✅ 新闻采集器已初始化")
    
    def _cleanup_cache(self):
        """清理过期缓存"""
        try:
            now = time.time()
            for cache_file in self.client.cache_dir.glob("*.json"):
                try:
                    mtime = cache_file.stat().st_mtime
                    if now - mtime > CACHE_CONFIG['ttl_seconds'] * 2:
                        cache_file.unlink()
                except:
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
            data = self.client.get(url, use_cache=False)  # 快讯不使用缓存
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
            data = self.client.get(url, use_cache=False)
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
                elif 'cnbc' in feed_url:
                    source = 'cnbc'
                elif 'dj' in feed_url:
                    source = 'wsj'
                elif 'federalreserve' in feed_url:
                    source = 'fed'
                elif 'sec.gov' in feed_url:
                    source = 'sec'
                
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
        
        # 1. 快讯类 (最高优先级)
        cls_news = self.fetch_cls_telegraph()
        all_news.extend(cls_news)
        
        bb_news = self.fetch_blockbeats()
        all_news.extend(bb_news)
        
        # 2. RSS 源
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
        
        return unique_news
    
    def close(self):
        """关闭采集器"""
        self.client.close()


# 全局单例
_collector: Optional[NewsCollector] = None


def get_collector(config: Dict = None) -> NewsCollector:
    """获取采集器单例"""
    global _collector
    if _collector is None:
        _collector = NewsCollector(config)
    return _collector


if __name__ == "__main__":
    # 测试运行
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    collector = get_collector()
    news_list = collector.fetch_all()
    
    print(f"\n📋 最新 5 条新闻:")
    for news in news_list[:5]:
        print(f"\n- {news.title}")
        print(f"  来源：{news.source} | 优先级：{news.priority}")
        print(f"  相关股票：{news.related_stocks or '无'}")
        print(f"  情绪：{news.sentiment_score:+.2f}")
    
    collector.close()
