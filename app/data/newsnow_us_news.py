#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# TOMBSTONE 2026-07-31 (DEB-fullchain-cleanup 第5轮终裁): 唯一实例化点在死函数
#   run_llm_strategy_scan (llm_pipeline.py:236) 内 — 零活调用, 保留备查, 08-07 后物理删除批处理。
"""
FTSeek-US NewsNow 美股新闻采集器 (补充源)

目的: 解决 RSSHub 大面积失效问题 (us_reuters/us_cnbc 不可用)
引入 TrendRadar 项目的 newsnow API 能力，但只做交易相关源。

数据源 (仅交易相关):
- wallstreetcn: 华尔街见闻 (中文财经权威)
- cls: 财联社电报 (A 股/美股快讯)
- jin10: 金十数据 (宏观/外汇/原油快讯)

设计原则 (Karpathy):
- 不修改现有 RSSHub 代码
- 接口兼容 RSSHubUSNewsFetcher (复用存储/去重)
- 关键词筛选 + 去重 + 存储逻辑独立
- 失败不影响主流程 (try/except 保护)

API 来源: https://github.com/sansan0/TrendRadar (55k stars)
API 端点: https://newsnow.busiyi.world/api/s?id={platform}&latest
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
import sqlite3

# 复用 RSSHub 采集器的数据目录和 DB
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / 'data' / 'rsshub_us'
DB_PATH = DATA_DIR / 'news_us.db'

DATA_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger('rsshub_us')  # 复用同一 logger，便于统一监控


class NewsNowUSFetcher:
    """
    NewsNow API 美股新闻采集器
    
    接口设计: 与 RSSHubUSNewsFetcher 兼容
    - fetch_all() → Dict[str, int] (源名 → 数量)
    - get_recent_news(hours, limit) → List[Dict]
    """
    
    # 仅交易相关的源 (不引入抖音/微博/知乎等噪音)
    SOURCES = {
        'wallstreetcn': {
            'name': '华尔街见闻',
            'weight': 0.35,
            'interval': 3,  # 分钟
        },
        'cls': {
            'name': '财联社电报',
            'weight': 0.30,
            'interval': 2,  # 2分钟 (催化辩论一手源提速, RFC-Catalyst 2026-05-27)
        },
        'jin10': {
            'name': '金十数据',
            'weight': 0.25,
            'interval': 5,
        },
    }
    
    API_BASE = "https://newsnow.busiyi.world/api/s"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    
    # 美股关键词筛选 (与 RSSHub 采集器保持一致)
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
        '美股', '华尔街', '财报', '业绩', 'IPO', '并购', '收购',
        'AI', '芯片', '半导体', '电动车', 'EV',
        'Amazon', 'Google', 'Apple', 'Tesla', 'Meta', 'Microsoft',
        'Nvidia', '英伟达', '特斯拉', '苹果', '微软', '谷歌',
        '制裁', '关税', '贸易战', '地缘', '原油', '油价', '黄金',
        'crypto', 'bitcoin', '霍尔木兹', '能源', '石油',
    ]
    
    SYMBOL_MAP = {
        '英伟达': 'US.NVDA', 'NVDA': 'US.NVDA',
        '特斯拉': 'US.TSLA', 'TSLA': 'US.TSLA',
        '苹果': 'US.AAPL', 'AAPL': 'US.AAPL',
        '微软': 'US.MSFT', 'MSFT': 'US.MSFT',
        '谷歌': 'US.GOOG', 'GOOGL': 'US.GOOG', 'GOOG': 'US.GOOG',
        '亚马逊': 'US.AMZN', 'AMZN': 'US.AMZN',
        'Meta': 'US.META', 'META': 'US.META',
        'AMD': 'US.AMD', '博通': 'US.AVGO', 'AVGO': 'US.AVGO',
        '阿里巴巴': 'US.BABA', 'BABA': 'US.BABA', '阿里': 'US.BABA',
        '京东': 'US.JD', 'JD': 'US.JD',
        '拼多多': 'US.PDD', 'PDD': 'US.PDD',
        '百度': 'US.BIDU', 'BIDU': 'US.BIDU',
        '蔚来': 'US.NIO', 'NIO': 'US.NIO',
    }
    
    def __init__(self):
        self._last_fetch: Dict[str, datetime] = {}
        self._dedup_set = set()
        self._load_dedup()
    
    def _load_dedup(self):
        """加载已存在的新闻指纹 (复用同一 DB)"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            cursor.execute("SELECT id FROM us_news WHERE fetched_at > ?", (cutoff,))
            rows = cursor.fetchall()
            conn.close()
            self._dedup_set = {row[0] for row in rows}
            logger.info(f"[NewsNow] 去重集合加载: {len(self._dedup_set)} 条 (24h内)")
        except Exception as e:
            logger.warning(f"[NewsNow] 去重集合加载失败: {e}")
    
    def _compute_id(self, title: str, source: str) -> str:
        """计算新闻唯一指纹 (与 RSSHub 一致)"""
        import hashlib
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
                if kw in self.SYMBOL_MAP:
                    matched_symbols.add(self.SYMBOL_MAP[kw])
        
        return list(matched_symbols), matched_keywords
    
    def fetch_source(self, source_key: str) -> List[Dict]:
        """从单个 NewsNow 源获取新闻"""
        source = self.SOURCES.get(source_key)
        if not source:
            return []
        
        # 检查采集间隔
        last = self._last_fetch.get(source_key)
        if last and (datetime.now() - last).total_seconds() < source['interval'] * 60:
            return []
        
        url = f"{self.API_BASE}?id={source_key}&latest"
        articles = []
        
        try:
            resp = requests.get(url, timeout=15, headers=self.HEADERS)
            resp.raise_for_status()
            data = resp.json()
            items = data.get('items', [])
            
            for item in items[:50]:
                title = item.get('title', '').strip()
                if not title:
                    continue
                
                # 提取时间戳 (毫秒 → ISO)
                extra = item.get('extra', {})
                date_ms = extra.get('date', 0)
                if date_ms:
                    published = datetime.fromtimestamp(date_ms / 1000).isoformat()
                else:
                    published = datetime.now().isoformat()
                
                link = item.get('url', '')
                
                # 关键词筛选 (必须匹配至少一个关键词)
                symbols, keywords = self._match_keywords(title)
                if not keywords:
                    continue
                
                # 去重
                news_id = self._compute_id(title, source_key)
                if news_id in self._dedup_set:
                    continue
                
                articles.append({
                    'id': news_id,
                    'title': title,
                    'summary': '',
                    'source': f"newsnow_{source_key}",
                    'source_name': source['name'],
                    'url': link,
                    'published_at': published,
                    'fetched_at': datetime.now().isoformat(),
                    'symbols': json.dumps(symbols, ensure_ascii=False),
                    'keywords': json.dumps(keywords, ensure_ascii=False),
                    'weight': source['weight'],
                    'language': 'zh',
                })
            
            self._last_fetch[source_key] = datetime.now()
            logger.debug(f"[NewsNow] {source['name']}: 获取 {len(articles)} 条")
            
        except Exception as e:
            logger.warning(f"[NewsNow] {source['name']} 采集失败: {e}")
        
        return articles
    
    def save_articles(self, articles: List[Dict]) -> int:
        """保存新闻到数据库 (复用同一 DB 表)"""
        if not articles:
            return 0
        
        conn = sqlite3.connect(str(DB_PATH))
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
                pass  # 已存在
        
        conn.commit()
        conn.close()
        return saved
    
    def fetch_all(self) -> Dict[str, int]:
        """采集所有源，返回统计 (接口兼容 RSSHubUSNewsFetcher)"""
        results = {}
        all_articles = []
        
        for source_key in self.SOURCES:
            articles = self.fetch_source(source_key)
            all_articles.extend(articles)
            source_name = self.SOURCES[source_key]['name']
            results[source_name] = len(articles)
            if articles:
                logger.info(f"[NewsNow] {source_name}: {len(articles)} 条相关新闻")
        
        saved = self.save_articles(all_articles)
        if all_articles:
            logger.info(f"[NewsNow-US] 本轮采集完成: 获取 {len(all_articles)} 条, 保存 {saved} 条")
        return results
    
    def get_recent_news(self, hours: int = 2, limit: int = 50) -> List[Dict]:
        """获取最近 N 小时的新闻 (接口兼容 RSSHubUSNewsFetcher)"""
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        cursor.execute("""
            SELECT * FROM us_news 
            WHERE fetched_at > ? AND source LIKE 'newsnow_%'
            ORDER BY weight DESC, fetched_at DESC 
            LIMIT ?
        """, (cutoff, limit))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
