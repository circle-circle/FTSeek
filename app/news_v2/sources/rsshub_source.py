"""
FTSeek RSSHub 新闻源适配器 (V2)

从现有 rsshub_multi_source_cron.py 迁移而来，实现 SourceAdapter 接口。

数据源:
- RSSHub 多实例 (主 → 备)
- 6 个新闻源 (富途/金十/彭博/见闻/财联社/WSJ)
- 支持自动 fallback 和健康监控

用法:
    from app.news_v2.sources.rsshub_source import RSSHubSource
    
    source = RSSHubSource(interval_seconds=180)  # 每3分钟
    items = await source.fetch()
"""

import asyncio
import logging
import re
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime

try:
    import feedparser
    import requests
except ImportError:
    feedparser = None
    requests = None

from app.news_v2.sources.base import SourceAdapter
from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource, compute_simhash

logger = logging.getLogger("ftseek.news_v2.sources.rsshub")


# ============================================================
# 源配置 (从现有 RSSHubMultiSourceFetcher 迁移)
# ============================================================

RSSHUB_ROUTES = {
    "rsshub_futu": {
        "route": "/futunn/live",
        "name": "富途快讯",
        "market": "HK",
        "weight": 0.30,
    },
    "rsshub_jin10": {
        "route": "/jin10/flash",
        "name": "金十数据",
        "market": "ALL",
        "weight": 0.25,
    },
    "rsshub_bloomberg": {
        "route": "/bloomberg/markets",
        "name": "彭博市场",
        "market": "US",
        "weight": 0.25,
    },
    "rsshub_wscn_global": {
        "route": "/wallstreetcn/news/global",
        "name": "华尔街见闻全球",
        "market": "US",
        "weight": 0.10,
    },
    "rsshub_wscn_china": {
        "route": "/wallstreetcn/news/china",
        "name": "华尔街见闻中国",
        "market": "HK",
        "weight": 0.05,
    },
    "rsshub_cls": {
        "route": "/cls/telegraph",
        "name": "财联社电报",
        "market": "HK",
        "weight": 0.05,
    },
}

# RSSHub 实例列表 (主 → 备)
RSSHUB_INSTANCES: List[Tuple[str, int]] = [
    ("https://rsshub.rssforever.com", 10),
    ("https://rsshub.ktachibana.party", 8),
    ("https://rsshub.moeyy.cn", 8),
]

# 股票名称 → 代码映射
STOCK_NAME_TO_CODE = {
    "腾讯": "HK.00700", "阿里巴巴": "HK.09988", "阿里": "HK.09988",
    "美团": "HK.03690", "小米": "HK.01810", "中芯国际": "HK.00981",
    "泡泡玛特": "HK.09992", "比亚迪": "HK.01211", "快手": "HK.01024",
    "京东": "HK.09618", "网易": "HK.09999",
    "英伟达": "US.NVDA", "NVDA": "US.NVDA",
    "特斯拉": "US.TSLA", "TSLA": "US.TSLA",
    "苹果": "US.AAPL", "AAPL": "US.AAPL",
    "微软": "US.MSFT", "MSFT": "US.MSFT",
    "谷歌": "US.GOOG", "GOOGL": "US.GOOG",
    "亚马逊": "US.AMZN", "AMZN": "US.AMZN",
    "META": "US.META",
}


class RSSHubSource(SourceAdapter):
    """
    RSSHub 多源新闻采集器 (V2)
    
    特性:
    - 多实例自动 fallback
    - 健康监控 + 连续失败暂停
    - 输出 UnifiedNewsItemV2 格式
    """
    
    def __init__(self, interval_seconds: int = 180):
        super().__init__(interval_seconds=interval_seconds)
        self._route_index = 0  # 轮询采集所有源
    
    @property
    def source_id(self) -> str:
        return "rsshub_multi"
    
    @property
    def source_name(self) -> str:
        return "RSSHub多源"
    
    async def fetch(self) -> List[UnifiedNewsItemV2]:
        """
        采集所有配置的 RSSHub 源
        
        每次调用采集一个源 (轮询)，避免单次调用过载。
        """
        if not feedparser or not requests:
            logger.error("RSSHub 源缺少依赖: feedparser 或 requests")
            return []
        
        # 轮询选择下一个源
        route_keys = list(RSSHUB_ROUTES.keys())
        route_key = route_keys[self._route_index % len(route_keys)]
        self._route_index += 1
        
        route_info = RSSHUB_ROUTES[route_key]
        route = route_info["route"]
        name = route_info["name"]
        market = route_info["market"]
        weight = route_info["weight"]
        
        all_items = []
        
        # 尝试每个 RSSHub 实例
        for instance_url, timeout in RSSHUB_INSTANCES:
            url = f"{instance_url}{route}"
            try:
                resp = requests.get(url, timeout=(5, timeout))
                resp.raise_for_status()
                
                feed = feedparser.parse(resp.content)
                if not feed.entries:
                    logger.debug("[%s] 空源 (实例 %s)", name, instance_url)
                    continue
                
                for entry in feed.entries[:20]:  # 每源最多 20 条
                    item = self._parse_entry(entry, name, market, weight, route_key)
                    if item:
                        all_items.append(item)
                
                if all_items:
                    logger.info(
                        "[%s] 采集 %d 条 (实例 %s)",
                        name, len(all_items), instance_url
                    )
                    break  # 成功则停止尝试其他实例
                
            except requests.exceptions.RequestException as e:
                logger.debug("[%s] 实例 %s 失败: %s", name, instance_url, e)
                continue
        
        if not all_items:
            logger.debug("[%s] 所有实例均失败", name)
        
        return all_items
    
    def _parse_entry(self, entry, source_name: str, market: str,
                     weight: float, route_key: str) -> Optional[UnifiedNewsItemV2]:
        """解析单个 RSS 条目为 UnifiedNewsItemV2"""
        title_raw = getattr(entry, "title", "")
        if not title_raw:
            return None
        
        # 清理 HTML
        title_clean = re.sub(r"<[^>]+>", "", title_raw).strip()
        if not title_clean:
            return None
        
        summary_raw = getattr(entry, "summary", "")
        summary_clean = re.sub(r"<[^>]+>", "", summary_raw).strip()[:1000]
        
        link = getattr(entry, "link", "")
        
        # 解析时间
        pub_time = time.time()
        try:
            from email.utils import parsedate_to_datetime
            published = getattr(entry, "published", "")
            if published:
                dt = parsedate_to_datetime(published)
                pub_time = dt.timestamp()
        except Exception:
            pass
        
        # 判断市场标签
        text = f"{title_clean} {summary_clean}"
        market_tags = self._detect_markets(text, market)
        
        # 提取相关股票
        related_stocks = self._extract_stocks(text)
        
        # 判断紧急程度
        urgency = self._detect_urgency(title_clean, summary_clean)
        
        # 计算初始分数
        raw_score = weight
        if related_stocks:
            raw_score = max(raw_score, 0.5)
        if urgency == NewsUrgency.IMMEDIATE:
            raw_score = max(raw_score, 0.6)
        
        return UnifiedNewsItemV2(
            id=f"{route_key}_{self._content_hash(title_clean, summary_clean)}",
            title=title_clean,
            content=summary_clean,
            source=route_key,
            source_name=source_name,
            timestamp=pub_time,
            market_tags=market_tags,
            sectors=self._detect_sectors(text),
            urgency=urgency,
            related_stocks=related_stocks,
            raw_score=raw_score,
            raw_url=link,
        )
    
    def _detect_markets(self, text: str, source_market: str) -> List[str]:
        """检测新闻所属市场"""
        tags = set()
        
        if source_market in ("HK", "ALL"):
            hk_kw = ["腾讯", "阿里", "美团", "小米", "港股", "恒生", "中芯",
                     "比亚迪", "快手", "京东", "网易", "泡泡玛特", "HK."]
            if any(kw in text for kw in hk_kw):
                tags.add("HK")
        
        if source_market in ("US", "ALL"):
            us_kw = ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META",
                     "AMD", "纳指", "道指", "标普", "SPY", "QQQ", "美股", "US."]
            if any(kw in text for kw in us_kw):
                tags.add("US")
        
        # 宏观新闻
        macro_kw = ["美联储", "FED", "CPI", "PPI", "非农", "加息", "降息",
                    "利率", "通胀", "VIX", "恐慌", "央行", "GDP"]
        if any(kw in text for kw in macro_kw):
            tags.add("MACRO")
        
        if not tags:
            tags.add("ALL")
        
        return list(tags)
    
    def _extract_stocks(self, text: str) -> List[str]:
        """提取相关股票代码"""
        stocks = []
        for name, code in STOCK_NAME_TO_CODE.items():
            if name in text:
                stocks.append(code)
        
        # 直接匹配股票代码 (HK.XXXXX, US.XXXX)
        for m in re.finditer(r"HK\.\d{5}", text):
            code = m.group()
            if code not in stocks:
                stocks.append(code)
        for m in re.finditer(r"US\.[A-Z]{1,5}", text):
            code = m.group()
            if code not in stocks:
                stocks.append(code)
        
        return stocks[:10]  # 最多 10 个
    
    def _detect_urgency(self, title: str, content: str) -> str:
        """检测紧急程度"""
        text = f"{title} {content}"
        
        # 紧急事件
        immediate_kw = ["突发", "紧急", "暴涨", "暴跌", "崩盘", "停牌",
                        "战争", "空袭", "制裁", "加息", "降息", "财报",
                        "超预期", "不及预期", "大单", "异动"]
        if any(kw in text for kw in immediate_kw):
            return NewsUrgency.HIGH
        
        return NewsUrgency.NORMAL
    
    def _detect_sectors(self, text: str) -> List[str]:
        """检测板块"""
        sectors = []
        sector_map = {
            "科技": ["科技", "半导体", "芯片", "AI", "人工智能", "云计算", "软件", "互联网"],
            "能源": ["能源", "石油", "原油", "天然气", "煤炭", "新能源", "光伏"],
            "金融": ["银行", "保险", "券商", "金融", "利率"],
            "消费": ["消费", "零售", "电商"],
            "医药": ["医药", "制药", "疫苗", "FDA", "生物"],
            "汽车": ["汽车", "比亚迪", "特斯拉", "TSLA", "新能源"],
        }
        for sector, kws in sector_map.items():
            if any(kw in text for kw in kws):
                sectors.append(sector)
        return sectors
    
    @staticmethod
    def _content_hash(title: str, content: str) -> str:
        """生成内容哈希 (短版本)"""
        import hashlib
        text = f"{title}|{content[:100]}"
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
