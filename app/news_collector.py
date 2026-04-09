#!/usr/bin/env python3
"""
FTSeek 综合新闻获取模块

整合:
1. 富途 OpenD 资金流向 (作为市场情绪指标)
2. 东方财富 RSS 新闻
3. 金十数据快讯

更新时间: 2026-03-19
"""
import os
import sys
import json
import time
import logging
import requests
import feedparser
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger("ftseek.news_collector")
logger.propagate = False


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
    related_stocks: List[str] = None
    sentiment_score: float = 0.0
    
    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now().isoformat()
        if not self.id:
            import hashlib
            self.id = hashlib.md5(f"{self.title}{self.url}".encode()).hexdigest()[:16]
        if self.related_stocks is None:
            self.related_stocks = []
    
    def to_dict(self) -> Dict:
        return asdict(self)


class FutuMarketData:
    """富途市场数据获取 (作为情绪指标) — 单例长连接，避免反复 connect/close"""

    _instance = None
    _quote_ctx = None

    def __new__(cls, host: str = '127.0.0.1', port: int = 11111):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.host = host
            cls._instance.port = port
        return cls._instance

    def __init__(self, host: str = '127.0.0.1', port: int = 11111):
        # __new__ 已赋值，避免重复设置
        pass

    def _get_ctx(self):
        """获取或重建复用连接"""
        if FutuMarketData._quote_ctx is None:
            try:
                from futu import OpenQuoteContext
                FutuMarketData._quote_ctx = OpenQuoteContext(
                    host=self.host, port=self.port
                )
                logger.debug("[FutuMarketData] 长连接已建立")
            except Exception as e:
                logger.warning(f"[FutuMarketData] 建立连接失败: {e}")
                raise
        return FutuMarketData._quote_ctx

    def _reset_ctx(self):
        """连接异常时销毁，下次调用重建"""
        try:
            if FutuMarketData._quote_ctx is not None:
                FutuMarketData._quote_ctx.close()
        except Exception:
            pass
        finally:
            FutuMarketData._quote_ctx = None
            logger.debug("[FutuMarketData] 连接已重置，下次调用重建")
    
    def get_capital_flow(self, code: str) -> Dict:
        """
        获取资金流向 (作为情绪指标) — 复用长连接，失败时重建一次
        """
        for attempt in range(2):
            try:
                ctx = self._get_ctx()
                ret, data = ctx.get_capital_flow(code)

                if ret == 0 and len(data) > 0:
                    latest = data.iloc[-1]
                    in_flow = float(latest.get('in_flow', 0))
                    main_in_flow = latest.get('main_in_flow', 0)
                    if main_in_flow == 'N/A' or main_in_flow is None:
                        main_in_flow = in_flow
                    else:
                        main_in_flow = float(main_in_flow)
                    if in_flow > 0:
                        sentiment = min(1.0, in_flow / 100000000)
                    else:
                        sentiment = max(-1.0, in_flow / 100000000)
                    return {
                        'code': code,
                        'in_flow': in_flow,
                        'main_inflow': main_in_flow,
                        'sentiment': round(sentiment, 3),
                        'timestamp': latest.get('capital_flow_item_time', datetime.now().isoformat())
                    }
                return {'code': code, 'sentiment': 0, 'error': '无数据'}
            except Exception as e:
                logger.warning(f"获取资金流向失败 {code} (attempt {attempt+1}): {e}")
                self._reset_ctx()  # 重置连接，下次重建

        return {'code': code, 'sentiment': 0, 'error': '获取失败'}
    
    def get_market_snapshot(self, codes: List[str]) -> List[Dict]:
        """获取市场快照 — 复用长连接，失败时重建一次"""
        for attempt in range(2):
            try:
                ctx = self._get_ctx()
                ret, data = ctx.get_market_snapshot(codes)

                if ret == 0:
                    result = []
                    for _, row in data.iterrows():
                        result.append({
                            'code': row['code'],
                            'name': row.get('stock_name', ''),
                            'price': float(row.get('last_price', 0)),
                            'change_pct': float(row.get('change_rate', 0)),
                            'volume': int(row.get('volume', 0)),
                            'turnover': float(row.get('turnover', 0)),
                        })
                    return result
                return []
            except Exception as e:
                logger.warning(f"获取市场快照失败 (attempt {attempt+1}): {e}")
                self._reset_ctx()

        return []


class RSSNewsCollector:
    """RSS 新闻采集器"""
    
    # 稳定的新闻源 (仅使用可靠的)
    RSS_SOURCES = {
        "cnbc_markets": {
            "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "category": "finance",
            "priority": 5
        },
        "wsj_markets": {
            "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "category": "finance",
            "priority": 5
        },
        "fed_news": {
            "url": "https://www.federalreserve.gov/feeds/press_all.xml",
            "category": "macro",
            "priority": 3
        },
    }
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        })
    
    def fetch_rss(self, source_name: str, max_items: int = 20) -> List[NewsItem]:
        """获取单个 RSS 源"""
        if source_name not in self.RSS_SOURCES:
            return []
        
        source = self.RSS_SOURCES[source_name]
        news_list = []
        
        try:
            response = self.session.get(source['url'], timeout=10)
            if response.status_code == 200:
                feed = feedparser.parse(response.content)
                
                for entry in feed.entries[:max_items]:
                    # 解析发布时间
                    published_at = ""
                    if hasattr(entry, 'published'):
                        published_at = entry.published
                    elif hasattr(entry, 'pubDate'):
                        published_at = entry.pubDate
                    elif hasattr(entry, 'updated'):
                        published_at = entry.updated
                    
                    news = NewsItem(
                        title=entry.get('title', ''),
                        content=entry.get('summary', entry.get('description', '')),
                        summary=entry.get('summary', '')[:200],
                        source=source_name,
                        category=source['category'],
                        url=entry.get('link', ''),
                        published_at=published_at,
                    )
                    news_list.append(news)
                    
                logger.info(f"RSS {source_name}: 获取 {len(news_list)} 条")
        except Exception as e:
            logger.warning(f"RSS {source_name} 获取失败: {e}")
        
        return news_list
    
    def fetch_all(self, max_per_source: int = 10) -> List[NewsItem]:
        """获取所有 RSS 源"""
        all_news = []
        
        for source_name in self.RSS_SOURCES:
            news = self.fetch_rss(source_name, max_per_source)
            all_news.extend(news)
            time.sleep(0.5)  # 避免请求过快
        
        # 按时间排序 (最新的在前)
        all_news.sort(key=lambda x: x.published_at, reverse=True)
        
        logger.info(f"RSS 总计: {len(all_news)} 条新闻")
        return all_news


class Jin10NewsCollector:
    """金十数据快讯采集"""
    
    def __init__(self):
        self.url = "https://flash-api.jin10.com/get_flash_list"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.jin10.com/',
        })
    
    def fetch(self, max_items: int = 30) -> List[NewsItem]:
        """获取金十快讯"""
        news_list = []
        
        try:
            params = {
                'channel': '-8200',  # 全部
                'vip': '1',
            }
            response = self.session.get(self.url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data.get('data', [])[:max_items]:
                    # 解析时间
                    timestamp = item.get('time', 0)
                    if timestamp:
                        published_at = datetime.fromtimestamp(timestamp).isoformat()
                    else:
                        published_at = datetime.now().isoformat()
                    
                    news = NewsItem(
                        title=item.get('content', ''),
                        content=item.get('content', ''),
                        summary=item.get('content', '')[:200],
                        source='jin10',
                        category='flash',
                        url='https://www.jin10.com/',
                        published_at=published_at,
                    )
                    news_list.append(news)
                
                logger.info(f"金十快讯: 获取 {len(news_list)} 条")
        except Exception as e:
            logger.warning(f"金十快讯获取失败: {e}")
        
        return news_list


class CompositeNewsCollector:
    """综合新闻采集器"""
    
    def __init__(self, futu_host: str = '127.0.0.1', futu_port: int = 11111):
        self.futu = FutuMarketData(futu_host, futu_port)
        self.rss = RSSNewsCollector()
        self.jin10 = Jin10NewsCollector()
    
    def collect_all(self, watch_codes: List[str] = None) -> Dict:
        """
        采集所有数据
        
        Returns:
            {
                'news': [...],
                'capital_flow': {...},
                'market_snapshot': [...],
                'summary': {
                    'news_count': 50,
                    'avg_sentiment': 0.1,
                    'timestamp': '...'
                }
            }
        """
        result = {
            'news': [],
            'capital_flow': {},
            'market_snapshot': [],
            'summary': {},
            'timestamp': datetime.now().isoformat()
        }
        
        # 1. 采集新闻
        try:
            rss_news = self.rss.fetch_all(max_per_source=10)
            jin10_news = self.jin10.fetch(max_items=20)
            result['news'] = rss_news + jin10_news
        except Exception as e:
            logger.error(f"新闻采集失败: {e}")
        
        # 2. 获取资金流向 (情绪指标)
        if watch_codes:
            for code in watch_codes[:3]:  # 限制请求数
                flow = self.futu.get_capital_flow(code)
                result['capital_flow'][code] = flow
                time.sleep(0.3)
        
        # 3. 获取市场快照
        if watch_codes:
            snapshot = self.futu.get_market_snapshot(watch_codes)
            result['market_snapshot'] = snapshot
        
        # 4. 生成摘要
        sentiments = [f.get('sentiment', 0) for f in result['capital_flow'].values() if f.get('sentiment')]
        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
        
        result['summary'] = {
            'news_count': len(result['news']),
            'avg_sentiment': round(avg_sentiment, 3),
            'sentiment_label': 'positive' if avg_sentiment > 0.1 else ('negative' if avg_sentiment < -0.1 else 'neutral'),
        }
        
        return result
    
    def get_sentiment_for_stock(self, code: str) -> float:
        """获取单只股票的情绪得分"""
        flow = self.futu.get_capital_flow(code)
        return flow.get('sentiment', 0)


# 便捷函数
def fetch_news(watch_codes: List[str] = None) -> Dict:
    """获取新闻和市场数据"""
    collector = CompositeNewsCollector()
    return collector.collect_all(watch_codes)


if __name__ == "__main__":
    # 测试
    result = fetch_news(['HK.03690', 'HK.09992', 'HK.00981'])
    
    print(f"\n📊 新闻采集结果")
    print(f"="*50)
    print(f"新闻数量: {result['summary']['news_count']}")
    print(f"平均情绪: {result['summary']['avg_sentiment']}")
    print(f"情绪标签: {result['summary']['sentiment_label']}")
    
    print(f"\n💰 资金流向:")
    for code, flow in result['capital_flow'].items():
        print(f"  {code}: 情绪={flow.get('sentiment', 'N/A')}")
    
    print(f"\n📰 最新新闻 (前5条):")
    for news in result['news'][:5]:
        print(f"  [{news.source}] {news.title[:50]}...")