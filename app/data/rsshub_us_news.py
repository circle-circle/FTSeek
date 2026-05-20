#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US RSSHub 美股新闻采集器

复用 FTSeek-HK 已有的 RSSHub 多源采集管线，专注于美股相关新闻。
与现有的 news_provider.py (RSS feed) 互补，提供更丰富的中文财经源。

数据源:
- P0: 彭博市场 (/bloomberg/markets) - 美股权威
- P0: 华尔街见闻全球 (/wallstreetcn/news/global) - 全球市场含美股
- P1: 金十数据 (/jin10/flash) - 全球快讯含美股宏观
- P1: 财联社电报 (/cls/telegraph) - 中国财经含美股关联

筛选策略:
- 关键词匹配: 美股代码 + 宏观关键词
- 去重: SHA256 标题指纹 (复用全局去重)
- 存储: JSON 文件 + SQLite (供 LLM 策略引擎消费)

作者: FTSeek Team
日期: 2026-04-16
"""

import asyncio
import json
import logging
import os
import sys
import time
import hashlib
import sqlite3
import urllib3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import feedparser
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 公共 RSSHub SSL 警告静默

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

DATA_DIR = BASE_DIR / 'data' / 'rsshub_us'
LOG_DIR = BASE_DIR / 'logs'
DB_PATH = DATA_DIR / 'news_us.db'

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'rsshub_us.log'),
    ]
)
logger = logging.getLogger('rsshub_us')


class RSSHubUSNewsFetcher:
    """
    RSSHub 美股新闻采集器
    
    复用 FTSeek-HK 的 RSSHub 实例，但只采集美股相关新闻。
    P1 修复 (2026-04-17): 优先从 unified_config.yaml 读取新闻源配置。
    """
    
    # 默认 RSSHub 实例列表 (主 → 备)，自动 fallback
    # 2026-04-25 修复: rss.shab.fun 完全失效 (ConnectionError)，移除
    DEFAULT_RSSHUB_INSTANCES = [
        "https://rsshub.rssforever.com",   # 主实例 (唯一可用)
    ]
    
    def __init__(self, config_path: str = None):
        self._load_config(config_path)
        
        self._last_fetch: Dict[str, datetime] = {}
        self._db_path = self.DATA_DIR / 'news_us.db'
        self._init_db()
        self._dedup_set: Set[str] = set()
        self._load_dedup()

    def _load_config(self, config_path: str = None):
        """加载配置，优先从 unified_config.yaml 读取"""
        cfg = {}
        try:
            from app.core.config_container import get_config
            cfg = get_config(config_path).get('news_sources.rsshub', {})
        except Exception:
            pass
            
        # 实例列表
        instances_cfg = cfg.get('instances', [])
        self.RSSHUB_INSTANCES = [i['url'] for i in instances_cfg] if instances_cfg else self.DEFAULT_RSSHUB_INSTANCES
        
        # 数据目录
        self.DATA_DIR = Path(cfg.get('data_dir', 'data/rsshub_us'))
        if not self.DATA_DIR.is_absolute():
            self.DATA_DIR = Path(__file__).resolve().parent.parent.parent / self.DATA_DIR
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # 新闻源定义
        sources_cfg = cfg.get('sources', {})
        self.SOURCES = {}
        for name, s in sources_cfg.items():
            self.SOURCES[name] = {
                'route': s.get('path', ''),
                'name': name,
                'priority': s.get('priority', 1),
                'weight': 0.2,
                'interval': s.get('interval_min', 10),
                'language': s.get('language', 'en'),
                'type': s.get('type', 'rsshub'),  # 2026-04-25 新增: 支持 direct/rsshub
            }
            
        # 如果配置为空，使用默认源
        if not self.SOURCES:
            self._init_default_sources()
            
        logger.info(f"[RSSHub采集器] 已初始化 | 实例={len(self.RSSHUB_INSTANCES)} | 源={len(self.SOURCES)} | 来源={'unified_config' if sources_cfg else 'hardcoded_defaults'}")

    def _init_default_sources(self):
        """默认美股新闻源 — RSSHub 源 + 直接 RSS 源混合"""
        self.SOURCES = {
        # === P1: 美股宏观源 (RSSHub) ===
        'us_bloomberg': {
            'route': '/bloomberg/markets',
            'name': '彭博市场',
            'priority': 0,
            'weight': 0.35,
            'interval': 3,
            'language': 'en',
            'type': 'rsshub',  # 通过 RSSHub 获取
        },
        'us_wallstreetcn': {
            'route': '/wallstreetcn/news',
            'name': '华尔街见闻',
            'priority': 0,
            'weight': 0.25,
            'interval': 5,
            'language': 'zh',
            'type': 'rsshub',
        },
        'us_cls': {
            'route': '/cls/telegraph',
            'name': '财联社电报',
            'priority': 1,
            'weight': 0.20,
            'interval': 10,
            'language': 'zh',
            'type': 'rsshub',
        },
        # 2026-04-25 新增: 直接 RSS 源 (绕过 RSSHub)
        'us_cnbc_direct': {
            'route': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114',
            'name': 'CNBC (直接RSS)',
            'priority': 1,
            'weight': 0.25,
            'interval': 5,
            'language': 'en',
            'type': 'direct',  # 直接获取，不经过 RSSHub
        },
        'us_seekingalpha_direct': {
            'route': 'https://seekingalpha.com/feed.xml',
            'name': 'Seeking Alpha (直接RSS)',
            'priority': 2,
            'weight': 0.20,
            'interval': 10,
            'language': 'en',
            'type': 'direct',
        },
    }
    
    # === 美股关键词筛选 (中英文) ===
    US_KEYWORDS = [
        # 美股代码
        'NVDA', 'TSLA', 'AAPL', 'MSFT', 'GOOGL', 'GOOG',
        'AMZN', 'META', 'AMD', 'INTC', 'NFLX', 'CRM', 'ORCL',
        'JPM', 'BAC', 'GS', 'MS', 'V', 'MA',
        'SPY', 'QQQ', 'IWM', 'DIA',
        'BABA', 'JD', 'PDD', 'BIDU', 'NIO', 'XPEV', 'LI',
        
        # 宏观/事件
        '美联储', 'FED', '利率', '加息', '降息', 'CPI', 'PPI', '非农',
        '纳指', '道指', '标普', 'S&P', 'Nasdaq', 'Dow',
        '美股', '华尔街', '华尔街见闻',
        '财报', '业绩', 'IPO', '并购', '收购',
        'AI', '芯片', '半导体', '电动车', 'EV',
        'Amazon', 'Google', 'Apple', 'Tesla', 'Meta', 'Microsoft',
        'Nvidia', '英伟达', '特斯拉', '苹果', '微软', '谷歌',
        '博通', 'Broadcom', '星链', 'Starlink', 'SpaceX',
        
        # 地缘/宏观事件 (触发二阶推理)
        '制裁', '关税', '贸易战', '地缘', '战争', '停火',
        '原油', '油价', '黄金', '黄金', 'bitcoin', 'crypto',
    ]
    
    # 股票代码映射 (中文 → US.xxx)
    SYMBOL_MAP = {
        '英伟达': 'US.NVDA', 'NVDA': 'US.NVDA',
        '特斯拉': 'US.TSLA', 'TSLA': 'US.TSLA',
        '苹果': 'US.AAPL', 'AAPL': 'US.AAPL',
        '微软': 'US.MSFT', 'MSFT': 'US.MSFT',
        '谷歌': 'US.GOOG', 'GOOGL': 'US.GOOG', 'GOOG': 'US.GOOG',
        '亚马逊': 'US.AMZN', 'AMZN': 'US.AMZN',
        'Meta': 'US.META', 'META': 'US.META',
        'AMD': 'US.AMD', 'AMD': 'US.AMD',
        '博通': 'US.AVGO', 'AVGO': 'US.AVGO',
        '阿里巴巴': 'US.BABA', 'BABA': 'US.BABA', '阿里': 'US.BABA',
        '京东': 'US.JD', 'JD': 'US.JD',
        '拼多多': 'US.PDD', 'PDD': 'US.PDD',
        '百度': 'US.BIDU', 'BIDU': 'US.BIDU',
        '蔚来': 'US.NIO', 'NIO': 'US.NIO',
    }
    
    def _init_db(self):
        """初始化 SQLite 数据库"""
        conn = sqlite3.connect(str(self._db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS us_news (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                source TEXT,
                source_name TEXT,
                url TEXT,
                published_at TEXT,
                fetched_at TEXT,
                symbols TEXT,          -- JSON array of matched symbols
                keywords TEXT,         -- JSON array of matched keywords
                weight REAL DEFAULT 0,
                language TEXT DEFAULT 'en'
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_published ON us_news(published_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_symbols ON us_news(symbols)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_fetched ON us_news(fetched_at)")
        conn.commit()
        conn.close()
        logger.info(f"✅ US News DB 初始化: {self._db_path}")
    
    def _load_dedup(self):
        """加载已存在的新闻指纹到去重集合"""
        try:
            conn = sqlite3.connect(str(self._db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM us_news WHERE fetched_at > ?",
                          ((datetime.now() - timedelta(hours=24)).isoformat(),))
            rows = cursor.fetchall()
            conn.close()
            self._dedup_set = {row[0] for row in rows}
            logger.info(f"去重集合加载: {len(self._dedup_set)} 条 (24h内)")
        except Exception as e:
            logger.warning(f"去重集合加载失败: {e}")
    
    def _compute_id(self, title: str, source: str) -> str:
        """计算新闻唯一指纹"""
        raw = f"{title}|{source}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def _match_keywords(self, text: str) -> tuple:
        """匹配关键词，返回 (symbols, keywords)"""
        text_lower = text.lower()
        matched_keywords = []
        matched_symbols = set()
        
        for kw in self.US_KEYWORDS:
            if kw.lower() in text_lower:
                matched_keywords.append(kw)
                # 检查是否是股票代码
                if kw in self.SYMBOL_MAP:
                    matched_symbols.add(self.SYMBOL_MAP[kw])
        
        return list(matched_symbols), matched_keywords
    
    def fetch_source(self, source_key: str) -> List[Dict]:
        """从单个源获取新闻 (支持 RSSHub 和 直接 RSS 源)"""
        source = self.SOURCES.get(source_key)
        if not source:
            return []
        
        source_type = source.get('type', 'rsshub')  # 默认 rsshub 向后兼容
        
        if source_type == 'direct':
            return self._fetch_direct_source(source_key, source)
        else:
            return self._fetch_rsshub_source(source_key, source)
    
    def _fetch_direct_source(self, source_key: str, source: Dict) -> List[Dict]:
        """直接 RSS 源获取 (不经过 RSSHub)"""
        url = source['route']
        articles = []
        
        try:
            resp = requests.get(url, timeout=15, headers={
                'User-Agent': 'FTSeek-US/1.0'
            })
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            
            for entry in feed.entries[:50]:
                title = entry.get('title', '').strip()
                if not title:
                    continue
                
                summary = entry.get('summary', '')
                published = entry.get('published', '')
                link = entry.get('link', '')
                content_text = f"{title} {summary}"
                
                # 关键词筛选
                symbols, keywords = self._match_keywords(content_text)
                if not keywords:
                    continue
                
                # 去重
                news_id = self._compute_id(title, source_key)
                if news_id in self._dedup_set:
                    continue
                
                articles.append({
                    'id': news_id,
                    'title': title,
                    'summary': summary[:500],
                    'source': source_key,
                    'source_name': source['name'],
                    'url': link,
                    'published_at': published,
                    'fetched_at': datetime.now().isoformat(),
                    'symbols': json.dumps(symbols, ensure_ascii=False),
                    'keywords': json.dumps(keywords, ensure_ascii=False),
                    'weight': source['weight'],
                    'language': source['language'],
                })
            
            if articles:
                logger.debug(f"直接RSS {source['name']}: {len(articles)} 条")
                
        except Exception as e:
            logger.warning(f"直接RSS {source['name']} 失败: {type(e).__name__} - {str(e)[:80]}")
        
        return articles
    
    def _fetch_rsshub_source(self, source_key: str, source: Dict) -> List[Dict]:
        """RSSHub 源获取 (带多实例 fallback)"""
        route = source['route']
        articles = []
        feed_failed_all = True  # 追踪是否所有实例都连接失败
        
        # 尝试多个 RSSHub 实例，自动 fallback
        for instance in self.RSSHUB_INSTANCES:
            url = f"{instance}{route}"
            try:
                resp = requests.get(url, timeout=15, headers={
                    'User-Agent': 'FTSeek-US/1.0'
                }, verify=False)  # 公共 RSSHub 实例常有 SSL 证书问题
                resp.raise_for_status()
                feed_failed_all = False  # 至少一个实例连接成功
                feed = feedparser.parse(resp.content)
                
                for entry in feed.entries[:50]:  # 最多取50条
                    title = entry.get('title', '').strip()
                    if not title:
                        continue
                    
                    summary = entry.get('summary', '')
                    published = entry.get('published', '')
                    link = entry.get('link', '')
                    content_text = f"{title} {summary}"
                    
                    # 关键词筛选 (必须匹配至少一个关键词)
                    symbols, keywords = self._match_keywords(content_text)
                    if not keywords:
                        continue
                    
                    # 去重
                    news_id = self._compute_id(title, source_key)
                    if news_id in self._dedup_set:
                        continue
                    
                    articles.append({
                        'id': news_id,
                        'title': title,
                        'summary': summary[:500],
                        'source': source_key,
                        'source_name': source['name'],
                        'url': link,
                        'published_at': published,
                        'fetched_at': datetime.now().isoformat(),
                        'symbols': json.dumps(symbols, ensure_ascii=False),
                        'keywords': json.dumps(keywords, ensure_ascii=False),
                        'weight': source['weight'],
                        'language': source['language'],
                    })
                
                # 成功获取，跳出实例循环
                if articles:
                    logger.debug(f"RSSHub {source['name']} 从 {instance} 获取 {len(articles)} 条")
                    break
                    
            except Exception as e:
                logger.debug(f"RSSHub {instance} 失败: {e}")
                continue
        
        if not articles:
            # 区分连接失败 vs 无匹配文章
            if feed_failed_all:
                logger.warning(f"RSSHub 采集失败 {source['name']}: 所有实例连接失败")
            else:
                logger.debug(f"RSSHub {source['name']}: 连接成功但无匹配关键词的文章")
        
        return articles
    
    def save_articles(self, articles: List[Dict]) -> int:
        """保存新闻到数据库"""
        if not articles:
            return 0
        
        conn = sqlite3.connect(str(self._db_path))
        cursor = conn.cursor()
        saved = 0
        
        for art in articles:
            try:
                cursor.execute("""
                    INSERT INTO us_news 
                    (id, title, summary, source, source_name, url, published_at, 
                     fetched_at, symbols, keywords, weight, language)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    art['id'], art['title'], art['summary'], art['source'],
                    art['source_name'], art['url'], art['published_at'],
                    art['fetched_at'], art['symbols'], art['keywords'],
                    art['weight'], art['language']
                ))
                self._dedup_set.add(art['id'])
                saved += 1
            except sqlite3.IntegrityError:
                pass  # 已存在，跳过
        
        conn.commit()
        conn.close()
        return saved
    
    def fetch_all(self) -> Dict[str, int]:
        """采集所有源，返回统计"""
        results = {}
        all_articles = []
        
        for source_key, source_info in self.SOURCES.items():
            # 检查采集间隔
            last = self._last_fetch.get(source_key)
            if last and (datetime.now() - last).total_seconds() < source_info['interval'] * 60:
                results[source_info['name']] = 0
                continue
            
            articles = self.fetch_source(source_key)
            self._last_fetch[source_key] = datetime.now()
            all_articles.extend(articles)
            results[source_info['name']] = len(articles)
            logger.info(f"[RSSHub] {source_info['name']}: {len(articles)} 条相关新闻")
        
        saved = self.save_articles(all_articles)
        logger.info(f"[RSSHub-US] 本轮采集完成: 获取 {len(all_articles)} 条, 保存 {saved} 条")
        return results
    
    def get_recent_news(self, hours: int = 2, limit: int = 50) -> List[Dict]:
        """获取最近 N 小时的新闻"""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        cursor.execute("""
            SELECT * FROM us_news 
            WHERE fetched_at > ? 
            ORDER BY weight DESC, fetched_at DESC 
            LIMIT ?
        """, (cutoff, limit))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    
    def get_news_for_symbol(self, symbol: str, hours: int = 4) -> List[Dict]:
        """获取指定股票的相关新闻"""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        cursor.execute("""
            SELECT * FROM us_news 
            WHERE fetched_at > ? AND symbols LIKE ?
            ORDER BY weight DESC, fetched_at DESC
        """, (cutoff, f'%{symbol}%'))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    
    def cleanup_old(self, keep_hours: int = 48):
        """清理旧数据"""
        conn = sqlite3.connect(str(self._db_path))
        cursor = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=keep_hours)).isoformat()
        cursor.execute("DELETE FROM us_news WHERE fetched_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            logger.info(f"清理旧新闻: {deleted} 条")


def main():
    """独立运行模式"""
    fetcher = RSSHubUSNewsFetcher()
    results = fetcher.fetch_all()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    
    # 展示最近新闻
    recent = fetcher.get_recent_news(hours=2, limit=5)
    for n in recent:
        print(f"  [{n['source_name']}] {n['title'][:80]}...")


if __name__ == '__main__':
    main()
