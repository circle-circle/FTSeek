#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSSHub 多源财经快讯采集器 (港股+美股版)

核心新闻源:
1. 港股核心:
   - 富途快讯 (futunn/live) - 港股实时快讯
   - 金十数据 (jin10/flash) - 全球快讯含港股
   
2. 股核心:
   - 彭博市场 (bloomberg/markets) - 美股市场
   - 华尔街见闻全球 (wallstreetcn/news/global) - 全球市场含美股
   
3. 辅助源:
   - 华尔街见闻中国 (wallstreetcn/news/china) - 港股相关深度分析
   - 财联社电报 (cls/telegraph) - 中国财经含港股

定时配置: 每3分钟采集一次
数据存储: data/rsshub_multi_source/

作者: FTSeek Team
日期: 2026-04-12
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import hashlib
import feedparser

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.factors.news_models import UnifiedNewsItem  # [NEW 2026-04-13] 统一模型
from app.utils.dedup_global import get_global_dedup  # [NEW 2026-04-13] 全局去重
from scripts.news_value_decay import SmartNewsCacheManager  # [NEW 2026-04-17] 智能缓存管理
from scripts.unified_news_push import UnifiedNewsPushEngine  # [NEW 2026-04-17] 统一推送引擎 (合并 futu_flash)

BASE_DIR = Path('/opt/ftseek/FTSeek')
DATA_DIR = BASE_DIR / 'data' / 'rsshub_multi_source'
LOG_DIR = BASE_DIR / 'logs'

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / 'rsshub_multi_source.log'),
    ]
)
logger = logging.getLogger('rsshub_multi_source')


class RSSHubMultiSourceFetcher:
    """
    RSSHub 多源采集器 - 港股+美股版
    
    源配置按优先级:
      P0 - 核心: 富途快讯, 金十数据, 彭博市场
      P1 - 重要: 华尔街见闻全球/中国, 财联社
    
    P0 修复 (2026-04-16): 多实例自动 fallback
    """
    
    # P1 (2026-04-17): RSSHub 实例列表 (主 → 备)
    # P1 修复 (2026-04-18): 增加更多备用实例，应对公共实例 503
    RSSHUB_INSTANCES = [
        "https://rsshub.rssforever.com",   # 主实例 (最稳定)
        "https://rsshub.ktachibana.party",  # 备用 1
        "https://rss.shab.fun",             # 备用 2
        "https://rsshub.moeyy.cn",          # 备用 3
    ]
    RSSHUB_BASE = "https://rsshub.rssforever.com"  # 保留向后兼容
    
    # 新闻源配置 (港股+美股为主)
    SOURCES = {
        # === 港股核心源 ===
        'hk_futu': {
            'route': '/futunn/live',
            'name': '富途快讯',
            'market': 'HK',
            'priority': 1,
            'description': '港股实时快讯，原生港股源',
            'weight': 0.30,  # 最高权重
            'interval': 3,   # 每3分钟采集
        },
        'hk_jin10': {
            'route': '/jin10/flash',
            'name': '金十数据',
            'market': 'HK',
            'priority': 1,
            'description': '全球财经快讯，含港股板块',
            'weight': 0.25,
            'interval': 3,   # 每3分钟采集
        },
        
        # === 美股核心源 ===
        'us_bloomberg': {
            'route': '/bloomberg/markets',
            'name': '彭博市场',
            'market': 'US',
            'priority': 1,
            'description': '美股市场权威资讯',
            'weight': 0.25,
            'interval': 3,   # 每3分钟采集
        },
        'us_wallstreetcn': {
            'route': '/wallstreetcn/news/global',
            'name': '华尔街见闻全球',
            'market': 'US',
            'priority': 2,
            'description': '全球市场深度，含美股',
            'weight': 0.15,
            'interval': 10,  # 辅助源，每10分钟采集
        },
        
        # === 辅助源 ===
        'cn_wallstreetcn': {
            'route': '/wallstreetcn/news/china',
            'name': '华尔街见闻中国',
            'market': 'HK',  # 港股相关
            'priority': 2,
            'description': '中国市场深度，港股参考',
            'weight': 0.05,
            'interval': 10,  # 辅助源，每10分钟采集
        },
        'cn_cls': {
            'route': '/cls/telegraph',
            'name': '财联社电报',
            'market': 'HK',  # 港股相关
            'priority': 2,
            'description': '中国财经电报，港股参考',
            'weight': 0.05,
            'interval': 10,  # 辅助源，每10分钟采集
        },
    }
    
    # 重要关键词 (港股+美股)
    MAJOR_KEYWORDS = {
        'HK': [
            '腾讯', '阿里巴巴', '美团', '小米', '中芯国际',
            '泡泡玛特', '比亚迪', '快手', '京东', '网易',
            '恒生指数', '恒生科技', '港股', 'HK',
            '财报', '业绩', 'IPO', '上市', '并购',
            '监管', '处罚', '停牌', '复牌',
        ],
        'US': [
            'NVDA', 'TSLA', 'AAPL', 'MSFT', 'GOOGL',
            'AMZN', 'META', 'AMD', 'INTC',
            '美联储', 'FED', '利率', '加息',
            '纳指', '道指', '标普', 'SPY', 'QQQ',
            '财报', '业绩', 'IPO', '并购',
        ],
    }
    
    # 股票代码映射
    STOCK_MAPPING = {
        'HK': {
            '腾讯': 'HK.00700',
            '阿里巴巴': 'HK.09988',
            '阿里': 'HK.09988',
            '美团': 'HK.03690',
            '小米': 'HK.01810',
            '中芯国际': 'HK.00981',
            '泡泡玛特': 'HK.09992',
            '比亚迪': 'HK.01211',
            '快手': 'HK.01024',
            '京东': 'HK.09618',
        },
        'US': {
            '英伟达': 'US.NVDA',
            'NVDA': 'US.NVDA',
            '特斯拉': 'US.TSLA',
            'TSLA': 'US.TSLA',
            '苹果': 'US.AAPL',
            'AAPL': 'US.AAPL',
            '微软': 'US.MSFT',
            'MSFT': 'US.MSFT',
            '谷歌': 'US.GOOG',
            'GOOGL': 'US.GOOG',
        },
    }
    
    def __init__(self):
        self.today = datetime.now().strftime('%Y%m%d')
        self.data_file = DATA_DIR / f'multi_source_{self.today}.json'
        self.stats_file = DATA_DIR / 'stats.json'
        self.health_file = DATA_DIR / 'rsshub_source_health.json'  # 2026-04-14 新增
        
        # 全局去重引擎 (替代 history_ids.json)
        self.dedup = get_global_dedup(ttl_hours=48)
        
        # [NEW 2026-04-17] 智能缓存管理器
        self.cache_manager = SmartNewsCacheManager(str(DATA_DIR))
        
        # [NEW 2026-04-17] 统一推送引擎 (合并自 futu_flash_cron.py)
        self.push_engine = UnifiedNewsPushEngine()
        
        # 统计
        self.stats = {
            'total_runs': 0,
            'total_news': 0,
            'hk_news': 0,
            'us_news': 0,
            'major_news': 0,
            'pushed_news': 0,
            'sources_status': {},
            'last_run': None,
        }
        self._load_stats()
        
        # 2026-04-14 新增: 源健康监控
        self.source_health = self._load_source_health()
        
        # 2026-04-22 新增: 采集时间跟踪 (用于冷却检查)
        self._last_fetch_times: dict = {}  # source_id -> datetime
    
    def _load_stats(self):
        if self.stats_file.exists():
            try:
                with open(self.stats_file, 'r') as f:
                    self.stats.update(json.load(f))
            except Exception as e:
                pass
    
    def _save_stats(self):
        with open(self.stats_file, 'w') as f:
            json.dump(self.stats, f, indent=2)

    def _load_source_health(self) -> dict:
        """2026-04-14 新增: 加载源健康状态"""
        if self.health_file.exists():
            try:
                with open(self.health_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        # 初始化所有源的健康状态
        return {
            src_id: {
                'consecutive_failures': 0,
                'total_runs': 0,
                'total_success': 0,
                'total_errors': 0,
                'disabled_until': None,  # 暂停到某个时间
                'last_error': None,
            }
            for src_id in self.SOURCES
        }
    
    def _save_source_health(self):
        """2026-04-14 新增: 保存源健康状态"""
        with open(self.health_file, 'w') as f:
            json.dump(self.source_health, f, indent=2)
    
    def _should_skip_source(self, source_id: str) -> bool:
        """2026-04-14 新增: 检查是否应该跳过该源（连续失败暂停）"""
        health = self.source_health.get(source_id, {})
        disabled_until = health.get('disabled_until')
        
        if disabled_until:
            try:
                until = datetime.fromisoformat(disabled_until)
                if datetime.now() < until:
                    remaining = (until - datetime.now()).total_seconds() / 60
                    logger.info(f"  ⏸️ 源 {self.SOURCES[source_id]['name']} 暂停中，剩余 {remaining:.0f} 分钟")
                    return True
                else:
                    # 暂停时间已到，重置状态
                    logger.info(f"  ▶️ 源 {self.SOURCES[source_id]['name']} 暂停结束，恢复采集")
                    health['consecutive_failures'] = 0
                    health['disabled_until'] = None
                    self.source_health[source_id] = health
                    self._save_source_health()
            except Exception:
                pass
        return False
    
    def _record_source_result(self, source_id: str, success: bool, error: str = None):
        """2026-04-14 新增: 记录源采集结果"""
        health = self.source_health.get(source_id, {
            'consecutive_failures': 0,
            'total_runs': 0,
            'total_success': 0,
            'total_errors': 0,
            'disabled_until': None,
            'last_error': None,
        })
        
        health['total_runs'] += 1
        
        if success:
            health['total_success'] += 1
            health['consecutive_failures'] = 0  # 重置连续失败计数
        else:
            health['total_errors'] += 1
            health['consecutive_failures'] += 1
            health['last_error'] = error[:100] if error else None
            
            # 连续失败 5 次 → 暂停 30 分钟
            if health['consecutive_failures'] >= 5 and health.get('disabled_until') is None:
                pause_until = datetime.now() + timedelta(minutes=30)
                health['disabled_until'] = pause_until.isoformat()
                logger.warning(
                    f"  🚫 源 {self.SOURCES[source_id]['name']} 连续失败 5 次，"
                    f"暂停 30 分钟 (到 {pause_until.strftime('%H:%M')})"
                )
        
        self.source_health[source_id] = health
        self._save_source_health()
    
    def _generate_id(self, title: str, source: str) -> str:
        content = f"{title}:{source}"
        return hashlib.md5(content.encode()).hexdigest()[:16]
    
    def _classify_market(self, text: str) -> List[str]:
        """判断新闻所属市场"""
        markets = []
        
        # 港股关键词检查
        hk_keywords = self.MAJOR_KEYWORDS['HK']
        if any(kw in text for kw in hk_keywords):
            markets.append('HK')
        
        # 美股关键词检查
        us_keywords = self.MAJOR_KEYWORDS['US']
        if any(kw in text for kw in us_keywords):
            markets.append('US')
        
        return markets if markets else ['ALL']
    
    def _is_major(self, text: str, markets: List[str]) -> bool:
        """判断是否重要新闻"""
        for market in markets:
            if market in self.MAJOR_KEYWORDS:
                keywords = self.MAJOR_KEYWORDS[market]
                if any(kw in text for kw in keywords):
                    return True
        return False

    def _get_watchlist(self) -> set:
        """获取监控股票名单 (2026-04-13 统一配置)

        优先级:
        1. 环境变量 WATCH_LIST_HK + WATCH_LIST_US
        2. config/unified_unified_config.yaml via config_center
        3. fallback 最小列表
        """
        try:
            import json as _json
            import os as _os

            # 1. 环境变量
            env_hk = _os.getenv("WATCH_LIST_HK")
            env_us = _os.getenv("WATCH_LIST_US")
            if env_hk:
                try:
                    lst = _json.loads(env_hk)
                    if isinstance(lst, list) and len(lst) > 0:
                        codes = set(lst)
                        if env_us:
                            try:
                                codes.update(_json.loads(env_us))
                            except (json.JSONDecodeError, ValueError):
                                pass
                        logger.info(f"[watchlist] 从环境变量加载 {len(codes)} 只")
                        return codes
                except Exception:
                    pass

            # 2. config_center
            try:
                sys.path.insert(0, '/opt/ftseek/FTSeek')
                from config.config_center import get_config_center
                cc = get_config_center()
                hk = cc.get('trading.watch_list_hk', [])
                us = cc.get('trading.watch_list_us', [])
                codes = set(hk) | set(us)
                if codes:
                    logger.info(f"[watchlist] 从 unified_config.yaml 加载 {len(codes)} 只")
                    return codes
            except Exception as e:
                logger.debug(f"[watchlist] config_center 加载失败: {e}")

            # 3. fallback
            logger.warning("[watchlist] 无法加载 watchlist，使用 fallback")
            return {"HK.00700", "HK.09988", "HK.03690"}

        except Exception as e:
            logger.error(f"[watchlist] 加载异常: {e}")
            return set()

    def _is_watchlist_related(self, text: str, stocks: list, markets: list,
                               watchlist_codes: set) -> bool:
        """检查新闻是否与监控股票相关 (2026-04-13 修复: 严格过滤)

        判断逻辑:
        1. 如果提取到了股票代码，检查是否在 watchlist 中
        2. 如果没提取到股票，检查文本是否包含 watchlist 股票名称/代码或核心宏观指标
        3. 不相关则拒绝 (解决 "高盛交易员" 等无关宏观评论推送问题)
        """
        if not watchlist_codes:
            return True

        # 1. 如果提取到了股票代码，检查是否匹配
        if stocks:
            return any(s in watchlist_codes for s in stocks)

        # 2. 没有提取到股票，检查文本是否包含 watchlist 关键词
        keywords = set()
        for code in watchlist_codes:
            keywords.add(code.split('.')[-1])  # "00700"
            keywords.add(code)                 # "HK.00700"
            for mkt, mapping in self.STOCK_MAPPING.items():
                for name, mcode in mapping.items():
                    if mcode == code:
                        keywords.add(name)

        # 允许核心宏观指标新闻通过
        macro_keywords = {'美联储', 'FED', '纳指', '道指', '标普', 'SPY', 'QQQ', '利率', '加息', '降息', 'CPI', '非农'}
        keywords.update(macro_keywords)

        # 严格匹配：文本中必须出现 watchlist 股票名/代码 或 宏观指标
        if any(kw in text for kw in keywords):
            return True

        return False

    def _extract_stocks(self, text: str, markets: List[str]) -> List[str]:
        """提取相关股票代码"""
        stocks = []
        
        for market in markets:
            if market in self.STOCK_MAPPING:
                for name, code in self.STOCK_MAPPING[market].items():
                    if name in text:
                        stocks.append(code)
        
        return list(set(stocks))
    
    async def fetch_source(self, source_id: str) -> List[UnifiedNewsItem]:
        """采集单个源 (2026-04-13 升级为输出 UnifiedNewsItem)
        
        P0 修复 (2026-04-16): 多实例自动 fallback，空源自动切换实例重试
        P1 修复 (2026-04-17): 优化超时设置，添加降级策略
        """
        source = self.SOURCES[source_id]
        route = source['route']
        name = source['name']
        market = source['market']
        
        # P1 (2026-04-17): RSSHub 实例列表 (主 → 备)
        # 2026-04-17 21:15 修复: 移除失效的 rss.shab.fun (DNS 解析失败)
        # P1 修复 (2026-04-18): 恢复备用实例列表
        RSSHUB_INSTANCES_OPTIMIZED = [
            ("https://rsshub.rssforever.com", 10),    # 主实例 (10s 超时)
            ("https://rsshub.ktachibana.party", 8),   # 备用 1 (8s 超时)
            # ("https://rss.shab.fun", 8),            # ❌ 已失效 (DNS 解析失败)
            ("https://rsshub.moeyy.cn", 8),           # 备用 3
        ]
        
        last_error = None
        for instance_url, timeout in RSSHUB_INSTANCES_OPTIMIZED:
            url = f"{instance_url}{route}"
            
            try:
                if instance_url != RSSHUB_INSTANCES_OPTIMIZED[0][0]:
                    logger.info(f"  🔄 切换到备用实例: {instance_url} (超时{timeout}s)")
                
                logger.info(f"Fetching {name}: {url} (timeout={timeout}s)")
                
                # P1 优化: 使用 requests 替代 feedparser 的直接 URL 解析
                # 这样可以更好地控制超时和错误处理
                import requests
                resp = requests.get(url, timeout=(5, timeout))  # (连接超时，读取超时)
                resp.raise_for_status()
                
                # 使用 feedparser 解析响应内容
                feed = feedparser.parse(resp.content)
                
                if not feed.entries:
                    logger.debug(f"{name}: empty on {instance_url}")
                    last_error = f"empty feed from {instance_url}"
                    continue  # 尝试下一个实例
                
                news_list = []
                
                for entry in feed.entries[:30]:  # 每源最多30条
                    title = entry.title if hasattr(entry, 'title') else ''
                    
                    if not title:
                        continue
                    
                    # 清理HTML标签
                    import re
                    title_clean = re.sub(r'<[^>]+>', '', title)
                    
                    link = entry.link if hasattr(entry, 'link') else ''
                    summary = entry.summary if hasattr(entry, 'summary') else ''
                    summary_clean = re.sub(r'<[^>]+>', '', summary)[:500]
                    
                    # 解析时间
                    try:
                        from email.utils import parsedate_to_datetime
                        published = entry.published if hasattr(entry, 'published') else ''
                        publish_time = parsedate_to_datetime(published)
                    except Exception as e:
                        publish_time = datetime.now()
                    
                    # 分类市场
                    text = f"{title_clean} {summary_clean}"
                    detected_markets = self._classify_market(text)
                    
                    # 判断重要性
                    is_major = self._is_major(text, detected_markets)
                    
                    # 提取股票
                    stocks = self._extract_stocks(text, detected_markets)
                    
                    # 构造统一新闻条目 (2026-04-13)
                    # 2026-04-14 修复: UnifiedNewsItem 需要 id 参数
                    content_for_hash = f"{title_clean}{summary_clean}"
                    content_hash = hashlib.sha256(content_for_hash.encode("utf-8")).hexdigest()[:16]
                    uni_item = UnifiedNewsItem(
                        id=content_hash,
                        source=f"rsshub/{name}",
                        title=title_clean,
                        content=summary_clean,
                        url=link,
                        published_at=publish_time.isoformat() if hasattr(publish_time, 'isoformat') else str(publish_time),
                        tags=["rsshub"] + detected_markets,
                        watchlist_codes=stocks,
                    )
                    
                    # 附加额外元数据 (作为属性动态添加，或保留在 dict 中供兼容)
                    uni_item._extra = {
                        'id': uni_item.content_hash,
                        'is_major': is_major,
                        'detected_markets': detected_markets,
                        'weight': source['weight'],
                        'pushed': False,
                    }
                    
                    # 全局去重: 跳过已见过的新闻
                    if not self.dedup.check_and_mark(uni_item.content_hash, source=f"rsshub/{name}", title=title_clean):
                        continue
                    
                    news_list.append(uni_item)
                
                self.stats['sources_status'][source_id] = f'ok:{len(news_list)}'
                logger.info(f"  ✅ {name}: {len(news_list)} new items (via {instance_url})")
                
                # 2026-04-14: 记录成功结果
                self._record_source_result(source_id, success=True)
                
                return news_list
                
            except Exception as e:
                logger.error(f"  ❌ {name} on {instance_url}: {e}")
                last_error = str(e)
                continue  # 尝试下一个实例
        
        # 所有实例都失败 - P1 新增: 降级策略
        logger.warning(f"  ⚠️ {name}: 所有 RSSHub 实例均失败 (last error: {last_error})")
        
        # P1 (2026-04-17): 降级策略 - 尝试使用 futu_flash 数据源作为替代
        if source_id == 'hk_futu':
            logger.info(f"  🔄 降级策略: 尝试使用 futu_flash 数据源")
            try:
                futu_flash_file = BASE_DIR / 'data' / 'futu_flash' / f'futu_flash_{datetime.now().strftime("%Y%m%d")}.json'
                if futu_flash_file.exists():
                    with open(futu_flash_file, 'r') as f:
                        flash_data = json.load(f)
                    
                    # 转换格式并返回最近的 20 条
                    news_list = []
                    for item in flash_data[:20]:
                        title = item.get('title', '')
                        if not title:
                            continue
                        
                        content_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
                        uni_item = UnifiedNewsItem(
                            id=content_hash,
                            source=f"futu_flash/{name}",
                            title=title,
                            content=item.get('content', '')[:500],
                            url=item.get('url', ''),
                            published_at=item.get('timestamp', datetime.now().isoformat()),
                            tags=["futu_flash", "HK", "degraded"],
                            watchlist_codes=[],
                        )
                        
                        uni_item._extra = {
                            'id': content_hash,
                            'is_major': False,
                            'detected_markets': ['HK'],
                            'weight': 0.1,  # 降级数据权重降低
                            'pushed': False,
                            'degraded': True,
                        }
                        
                        if self.dedup.check_and_mark(uni_item.content_hash, source=f"futu_flash/{name}", title=title):
                            news_list.append(uni_item)
                    
                    if news_list:
                        logger.info(f"  ✅ 降级成功: 从 futu_flash 获取 {len(news_list)} 条")
                        self.stats['sources_status'][source_id] = f'degraded:{len(news_list)}'
                        self._record_source_result(source_id, success=True)
                        return news_list
            except Exception as e:
                logger.warning(f"  ⚠️ 降级策略失败: {e}")
        
        # 最终失败
        self.stats['sources_status'][source_id] = f'failed:{last_error[:50] if last_error else "all empty"}'
        self._record_source_result(source_id, success=False, error=last_error)
        return []
    
    async def fetch_all(self) -> List[dict]:
        """采集所有源（2026-04-22 修复：基于冷却时间而非分钟取模）"""
        logger.info("=" * 60)
        logger.info(f"RSSHub 多源采集开始: {datetime.now()}")
        logger.info("=" * 60)
        
        # 按优先级排序
        sources_by_priority = sorted(
            self.SOURCES.keys(),
            key=lambda x: self.SOURCES[x]['priority']
        )
        
        # 2026-04-22 修复: 基于上次采集时间的冷却检查，而非分钟取模
        now = datetime.now()
        active_sources = []
        skipped_sources = []
        
        for source_id in sources_by_priority:
            # 检查健康状态
            if self._should_skip_source(source_id):
                skipped_sources.append(f"{self.SOURCES[source_id]['name']} (暂停中)")
                continue
            
            # 检查采集间隔 (基于上次采集时间)
            interval = self.SOURCES[source_id].get('interval', 3)
            last_fetch = self._last_fetch_times.get(source_id)
            
            if last_fetch and (now - last_fetch).total_seconds() < interval * 60:
                skipped_sources.append(f"{self.SOURCES[source_id]['name']} (冷却中)")
                continue
            
            active_sources.append(source_id)
        
        if skipped_sources:
            logger.info(f"\n跳过源 ({len(skipped_sources)}): {', '.join(skipped_sources)}")
        
        logger.info(f"\n活跃源 ({len(active_sources)}): {active_sources}")
        
        # 并行采集核心源 (P1) - 2026-04-14: 只采集活跃的源
        core_sources = [k for k in active_sources if self.SOURCES[k]['priority'] == 1]
        
        if not core_sources:
            logger.info("\n无活跃核心源，跳过")
            all_news = []
        else:
            logger.info(f"\n核心源采集 (P1): {core_sources}")
            tasks = [self.fetch_source(s) for s in core_sources]
            results = await asyncio.gather(*tasks)
            
            # 2026-04-22: 更新采集时间戳
            for source_id in core_sources:
                self._last_fetch_times[source_id] = datetime.now()
            
            all_news = []
            for news_list in results:
                all_news.extend(news_list)
        
        # 串行采集辅助源 (P2) - 2026-04-14: 只采集活跃的源
        aux_sources = [k for k in active_sources if self.SOURCES[k]['priority'] == 2]
        
        if aux_sources:
            logger.info(f"\n辅助源采集 (P2): {aux_sources}")
            for source_id in aux_sources:
                news_list = await self.fetch_source(source_id)
                all_news.extend(news_list)
                # 2026-04-22: 更新采集时间戳
                self._last_fetch_times[source_id] = datetime.now()
        
        # 按时间排序 (修复: 兼容 UnifiedNewsItem + dict + ISO时间字符串)
        def _parse_ts(val) -> float:
            """统一解析时间值为 float timestamp"""
            if val is None or val == 0 or val == '':
                return 0
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    from datetime import datetime as _dt
                    return _dt.fromisoformat(val).timestamp()
                except Exception:
                    return 0
            return 0

        def _get_publish_time(item):
            if isinstance(item, dict):
                for k in ('timestamp', 'publish_time'):
                    v = item.get(k)
                    if v and v != 0:
                        return _parse_ts(v)
                # 尝试 ISO 字符串
                return _parse_ts(item.get('published_at', 0))
            else:
                # UnifiedNewsItem 对象
                for attr in ('timestamp', 'publish_time'):
                    v = getattr(item, attr, None)
                    if v and v != 0:
                        return _parse_ts(v)
                return _parse_ts(getattr(item, 'published_at', 0))
        all_news.sort(key=_get_publish_time, reverse=True)
        
        return all_news
    
    async def save_news(self, news_list: List[dict]):
        """保存数据 - 2026-04-17 升级为智能缓存管理"""
        if not news_list:
            return
        
        # [NEW 2026-04-17] 1. 获取 watchlist 用于质量评分
        watchlist = self._get_watchlist()
        
        # [NEW 2026-04-17] 2. 对新闻进行质量评分和标签
        scored_news = self.cache_manager.score_and_tag_news(news_list, watchlist)
        
        # 读取已有数据
        existing = []
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r') as f:
                    existing = json.load(f)
            except Exception as e:
                pass
        
        # 合并
        all_data = existing + scored_news
        
        # 去重 (兼容 UnifiedNewsItem 和 dict)
        seen_ids = set()
        unique_data = []
        for news in all_data:
            try:
                news_id = news['id'] if isinstance(news, dict) else getattr(news, 'id', None)
                if news_id is None:
                    continue
                if news_id not in seen_ids:
                    seen_ids.add(news_id)
                    # 转换为 dict 存储
                    if isinstance(news, dict):
                        unique_data.append(news)
                    else:
                        # UnifiedNewsItem → dict
                        try:
                            unique_data.append(news.to_dict())
                        except Exception:
                            unique_data.append({'id': str(news_id), 'title': str(news)})
            except Exception:
                continue
        
        # [NEW 2026-04-17] 3. 清理过期新闻
        unique_data, removed_count = self.cache_manager.cleanup_expired(unique_data)
        if removed_count > 0:
            logger.info(f"\n🗑️ 清理过期新闻: {removed_count} 条")
        
        # [NEW 2026-04-17] 4. 按优先级排序 (质量 + 时间衰减)
        unique_data = self.cache_manager.get_priority_sorted(unique_data)
        
        # 保存
        with open(self.data_file, 'w') as f:
            json.dump(unique_data, f, indent=2, ensure_ascii=False)
        
        # [NEW 2026-04-17] 5. 输出缓存统计 (含 LLM 评分信息)
        cache_stats = self.cache_manager.get_cache_stats(unique_data)
        logger.info(f"\n💾 保存 {len(unique_data)} 条快讯到 {self.data_file}")
        logger.info(f"   📊 缓存统计：S 级={cache_stats['by_grade']['S']} A 级={cache_stats['by_grade']['A']} B 级={cache_stats['by_grade']['B']}")
        logger.info(f"   ⏱️ 平均分数: {cache_stats['avg_score']:.1f} | 消化窗口内: {cache_stats['digest_window_count']}")
        
        # [NEW 2026-04-17] LLM 评分统计
        if cache_stats['llm_scored_count'] > 0:
            logger.info(f"   🤖 LLM 评分: {cache_stats['llm_scored_count']} 条 | 平均 LLM 分数: {cache_stats['avg_llm_score']:.1f}")
        
        # [NEW 2026-04-17] 运行反馈学习周期
        if self.cache_manager.feedback_learner:
            try:
                learning_result = self.cache_manager.feedback_learner.run_learning_cycle()
                if not learning_result.get("skipped"):
                    logger.info(f"   📚 反馈学习: {learning_result['adjustment']}")
            except Exception as e:
                logger.debug(f"反馈学习异常: {e}")
        
        # [NEW 2026-04-17] 6. 推送重要新闻 (合并自 futu_flash)
        # 将新闻列表转换为推送格式
        push_candidates = []
        for news in unique_data[:50]:  # 检查前 50 条最新新闻
            # 如果是新采集的新闻 (未推送过)，加入候选
            if not news.get('pushed', False):
                push_candidates.append(news)
        
        if push_candidates:
            try:
                pushed_count = await self.push_engine.push_news(push_candidates, max_push=5)
                if pushed_count > 0:
                    logger.info(f"   📤 推送重要新闻: {pushed_count} 条")
                    self.stats['pushed_news'] += pushed_count
                
                # 注入事件驱动管线 (原 futu_flash 功能)
                await self.push_engine.trigger_event_pipeline(push_candidates, max_events=20)
            except Exception as e:
                logger.debug(f"推送异常: {e}")
        
        # 更新统计
        self.stats['total_news'] += len(news_list)
        self.stats['hk_news'] = sum(1 for n in unique_data if 'HK' in n.get('detected_markets', []))
        self.stats['us_news'] = sum(1 for n in unique_data if 'US' in n.get('detected_markets', []))
        self.stats['major_news'] = sum(1 for n in unique_data if n.get('is_major'))
    
    def _load_impact_map(self) -> dict:
        """加载事件影响映射配置 (2026-04-13 新增)"""
        path = BASE_DIR / 'config' / 'event_impact_map.json'
        if not path.exists():
            return {}
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载事件映射失败: {e}")
            return {}

    async def push_event_impacts(self, news_list: List[dict], watchlist_codes: set, 
                                  impact_map: dict, dedup):
        """宏观事件联想推送 (2026-04-13 新增 + 2026-04-15 统一LLM管线)"""
        if not impact_map:
            return

        for rule in impact_map.get('rules', []):
            keywords = rule.get('keywords', [])
            stocks = rule.get('stocks', {})
            logic = rule.get('logic', '')
            name = rule.get('name', '未知事件')

            # 过滤未推送且未去重的新闻
            candidates = [
                n for n in news_list 
                if not n.get('pushed') and 
                   not dedup.is_duplicate(n.get('title', '') + '|' + str(n.get('source_name', '')))
            ]

            for news in candidates:
                text = news.get('title', '') + ' ' + news.get('content', '')
                # 检查是否命中事件关键词
                if any(kw in text for kw in keywords):
                    # 标记为已推送
                    news['pushed'] = True
                    self.stats['pushed_news'] += 1
                    
                    # 检查去重
                    dedup_key = f"impact_{rule.get('id', 'unknown')}_{news.get('id', news.get('content_hash', ''))}"
                    if dedup.is_duplicate(dedup_key):
                        continue
                    dedup.mark_as_seen(dedup_key)

                    title = news.get('title', '无标题')
                    source = news.get('source_name', '未知来源')
                    pub_time = news.get('publish_time', '')
                    content = news.get('content', '')

                    # [UNIFIED 2026-04-15] 统一 LLM 管线: 一次分析, 多路输出
                    # 1. LLM 策略分析 (同时生成 DingTalk 内容 + 交易信号)
                    # 2. 消除之前 llm_impact_analyzer + llm_news_strategy 的双LLM调用
                    try:
                        await self._trigger_unified_news_strategy(
                            news_title=title,
                            news_content=content,
                            news_source=source,
                            pub_time=pub_time,
                            event_name=name,
                            event_sector=rule.get('impact_sector', ''),
                            static_logic=logic,
                            related_stocks=stocks,
                            dedup_key=dedup_key,
                        )
                    except Exception as e:
                        logger.error(f"统一新闻策略引擎触发失败: {e}")

    async def _trigger_unified_news_strategy(
        self, news_title: str, news_content: str, news_source: str,
        pub_time: str, event_name: str, event_sector: str,
        static_logic: str, related_stocks: dict, dedup_key: str,
    ):
        """
        统一新闻策略触发器 (2026-04-15)
        
        设计原则:
        - 一次 LLM 调用, 同时输出 DingTalk 推送内容 + 交易信号
        - 避免之前 llm_impact_analyzer + llm_news_strategy 的重复LLM调用
        - 内置降级: LLM 不可用时 fallback 到规则引擎
        
        输出:
        1. DingTalk 推送: 策略分析卡片 (含推理链、止盈止损)
        2. SignalStore 注入: 交易信号 (供共识引擎和执行器消费)
        """
        from app.factors.llm_news_strategy import get_news_strategy_engine
        from app.factors.news_signal_bridge import get_signal_bridge

        # 获取行情数据 (可选, 不阻塞)
        market_data = {}
        try:
            from app.news_collector import FutuMarketData
            futu = FutuMarketData()
            codes = list(related_stocks.keys())[:5]
            snapshots = futu.get_market_snapshot(codes)
            for snap in snapshots:
                code = snap.get('code', '')
                market_data[code] = {
                    'price': snap.get('price', 0),
                    'change_pct': snap.get('change_pct', 0),
                    'volume': snap.get('volume', 0),
                }
        except Exception as e:
            logger.debug(f"获取行情数据失败(非阻塞): {e}")

        # 调用 LLM 策略引擎 (一次调用)
        engine = get_news_strategy_engine()
        strategy_signal = await engine.analyze_news(
            news_title=news_title,
            news_content=news_content,
            news_source=news_source,
            pub_time=pub_time,
            event_name=event_name,
            event_sector=event_sector,
            static_logic=static_logic,
            related_stocks=related_stocks,
            market_data=market_data,
        )

        if not strategy_signal:
            return

        # 桥接 → SignalStore + 钉钉推送 (复用 bridge 的推送逻辑)
        try:
            from app.signal_store import signal_store
            from app.notifier.dingtalk import DingTalkNotifier
            
            notifier = DingTalkNotifier()
            bridge = get_signal_bridge(
                signal_store=signal_store,
                dingtalk_notifier=notifier,
            )
            
            injected, rejected = bridge.bridge_signal(
                news_strategy_signal=strategy_signal,
                auto_inject=True,
                auto_alert=True,
            )
            
            if injected:
                logger.info(
                    f"🎯 新闻策略信号已注入: {len(injected)} 只 | "
                    f"标的: {', '.join(s.code for s in injected)}"
                )
            else:
                logger.info(f"📋 新闻策略分析完成，但无交易信号生成")
                
        except Exception as e:
            logger.error(f"信号桥接失败: {e}")

    def _get_prop(self, item, key: str, default=None):
        """统一获取属性，兼容 UnifiedNewsItem 和 dict"""
        if isinstance(item, UnifiedNewsItem):
            # 标准字段映射
            if key == 'title': return item.title
            if key == 'content': return item.content
            if key == 'source_name': return item.source
            if key == 'publish_time': return item.published_at
            if key == 'published_at': return item.published_at
            if key == 'stocks': return item.watchlist_codes
            if key == 'detected_markets': return item.tags
            if key == 'timestamp':
                # 尝试从 published_at 计算
                if item.published_at:
                    try:
                        from datetime import datetime as _dt
                        return int(_dt.fromisoformat(item.published_at).timestamp())
                    except Exception:
                        pass
                return 0
            # 从 _extra 获取 (is_major, pushed, weight 等)
            if hasattr(item, '_extra') and isinstance(item._extra, dict):
                if key in item._extra:
                    return item._extra[key]
            return default
        elif isinstance(item, dict):
            return item.get(key, default)
        return default

    async def push_major_news(self, news_list: List[dict]):
        """推送重要快讯到钉钉（带全局去重 + watchlist 过滤）"""
        # 初始化全局去重器
        dedup = get_global_dedup(ttl_hours=2)

        # 加载 watchlist (2026-04-13 统一配置)
        watchlist_codes = self._get_watchlist()
        impact_map = self._load_impact_map()

        # 过滤: 重要快讯 + 未在本进程推送过 + 未在全局去重库中 + 与 watchlist 相关
        major_news = []
        for n in news_list:
            if not self._get_prop(n, 'is_major'):
                continue
            if self._get_prop(n, 'pushed'):
                continue
            title = self._get_prop(n, 'title', '')
            source = self._get_prop(n, 'source_name', '')
            
            # 全局去重检查 (使用 UnifiedNewsItem 的 content_hash 或标题)
            news_hash = n.content_hash if isinstance(n, UnifiedNewsItem) else title
            if dedup.is_duplicate(news_hash):
                logger.debug(f"[去重] 跳过已推送: {title[:50]}")
                continue

            # 2026-04-13 新增: watchlist 过滤
            text = self._get_prop(n, 'title', '') + ' ' + self._get_prop(n, 'content', '')
            stocks = self._get_prop(n, 'stocks', [])
            markets = self._get_prop(n, 'detected_markets', [])
            if not self._is_watchlist_related(text, stocks, markets, watchlist_codes):
                logger.debug(f"[watchlist过滤] 跳过非监控股票新闻: {title[:60]}")
                continue

            major_news.append(n)
        
        if not major_news:
            logger.info("\n无重要快讯需推送（全部已推送或去重）")
            return
        
        # 按权重排序
        major_news.sort(key=lambda x: self._get_prop(x, 'weight', 0), reverse=True)
        
        # 最多推送5条
        to_push = major_news[:5]
        
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            
            logger.info(f"\n推送 {len(to_push)} 条重要快讯（经去重过滤）:")
            
            for news in to_push:
                title = self._get_prop(news, 'title', '')[:80]
                markets = self._get_prop(news, 'detected_markets', [])
                stocks = self._get_prop(news, 'stocks', [])
                source = self._get_prop(news, 'source_name', '')
                
                # 全局去重标记 (使用 ContentHash)
                news_hash = news.content_hash if isinstance(news, UnifiedNewsItem) else title
                if not dedup.check_and_mark(news_hash, source, title):
                    logger.debug(f"[去重] 跳过: {title[:50]}")
                    continue
                
                # 构建消息
                market_icon = '🇭🇰' if 'HK' in markets else '🇺🇸' if 'US' in markets else '🌍'
                
                text = f"""
### {market_icon} 重要财经快讯

**标题**: {title}

**市场**: {', '.join(markets)}

**相关股票**: {', '.join(stocks) if stocks else '未识别'}

**来源**: {source}

**时间**: {self._get_prop(news, 'publish_time', '')[:19]}

---
⚡ 此快讯包含重要关键词
"""
                
                result = notifier.send_text(text)
                
                if result:
                    if hasattr(news, '_extra'):
                        news._extra['pushed'] = True
                    self.stats['pushed_news'] += 1
                    logger.info(f"  ✅ 推送成功: {title[:50]}")
                else:
                    logger.warning(f"  ❌ 推送失败: {title[:50]}")
            
            # 4. [NEW] 宏观事件联想推送 (2026-04-13)
            # 检查未被推送的新闻是否命中宏观事件映射
            await self.push_event_impacts(news_list, watchlist_codes, impact_map, dedup)

            # 更新数据文件中的推送状态
            await self.save_news(news_list)
            
        except Exception as e:
            logger.error(f"钉钉推送异常: {e}")
    
    async def run(self):
        """主流程"""
        self.stats['total_runs'] += 1
        self.stats['last_run'] = datetime.now().isoformat()
        
        # 1. 采集所有源
        news_list = await self.fetch_all()
        
        # 2. 保存数据
        await self.save_news(news_list)
        
        # 3. 推送重要快讯
        await self.push_major_news(news_list)
        
        # 4. 保存统计 (历史去重已迁移至全局 news_dedup.db)
        self._save_stats()
        
        # 5. 输出统计
        logger.info("\n" + "=" * 60)
        logger.info("本次采集统计:")
        logger.info(f"  获取快讯: {len(news_list)} 条")
        logger.info(f"  港股相关: {sum(1 for n in news_list if 'HK' in n.get('detected_markets', []))} 条")
        logger.info(f"  美股相关: {sum(1 for n in news_list if 'US' in n.get('detected_markets', []))} 条")
        logger.info(f"  重要快讯: {sum(1 for n in news_list if n.get('is_major'))} 条")
        logger.info(f"  推送数量: {self.stats['pushed_news']} 条")
        logger.info(f"\n各源状态:")
        for source_id, status in self.stats['sources_status'].items():
            source_name = self.SOURCES[source_id]['name']
            logger.info(f"  {source_name}: {status}")
        logger.info("=" * 60)
        logger.info("采集完成")


async def main():
    fetcher = RSSHubMultiSourceFetcher()
    await fetcher.run()


if __name__ == "__main__":
    asyncio.run(main())