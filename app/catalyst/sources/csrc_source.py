"""
FTSeek-US CSRC 新闻源 — 中国证监会官网 (一手信息)

特点:
1. 监管公告一手源 (证监会直接发布)
2. 关键词匹配: watchlist 公司名 + 监管/处罚/调查
3. 轮询间隔 5 分钟 (300秒)
4. 自动关联受影响的 US ticker

URL: http://www.csrc.gov.cn/csrc/c100028/common_list.shtml
"""

import hashlib
import logging
import re
import time
from typing import List, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.csrc")

# 公司名 → US ticker 映射 (用于关键词匹配后关联标的)
COMPANY_TICKER_MAPPING = {
    # 中概券商
    "富途": ["US.FUTU"],
    "富途控股": ["US.FUTU"],
    "moomoo": ["US.FUTU"],
    "老虎证券": ["US.TIGR"],
    "老虎": ["US.TIGR"],
    "长桥": ["US.LB"],
    "华盛": ["US.HLBZ"],
    # 中概科技
    "阿里巴巴": ["US.BABA"],
    "阿里": ["US.BABA"],
    "腾讯": ["US.TCEHY"],
    "百度": ["US.BIDU"],
    "京东": ["US.JD"],
    "拼多多": ["US.PDD"],
    "美团": ["US.MPNGY"],
    "网易": ["US.NTES"],
    "小米": ["US.XIAOMY"],
    # 新能源
    "蔚来": ["US.NIO"],
    "小鹏": ["US.XPEV"],
    "理想": ["US.LI"],
    "比亚迪": ["US.BYDDY"],
}

# 监管关键词 → urgency 映射
REGULATORY_KEYWORDS = {
    "行政处罚": NewsUrgency.IMMEDIATE,
    "立案调查": NewsUrgency.IMMEDIATE,
    "监管措施": NewsUrgency.HIGH,
    "警示函": NewsUrgency.HIGH,
    "责令整改": NewsUrgency.HIGH,
    "暂停业务": NewsUrgency.IMMEDIATE,
    "吊销牌照": NewsUrgency.IMMEDIATE,
    "罚款": NewsUrgency.HIGH,
    "没收违法所得": NewsUrgency.HIGH,
    "市场禁入": NewsUrgency.IMMEDIATE,
    "调查": NewsUrgency.HIGH,
    "处罚": NewsUrgency.HIGH,
    "监管": NewsUrgency.NORMAL,
    "通报": NewsUrgency.NORMAL,
}


class CSRCSource(SourceAdapter):
    """证监会官网新闻源"""
    
    def __init__(self, interval_seconds: int = 300):
        super().__init__(interval_seconds=interval_seconds)
        self.list_url = "http://www.csrc.gov.cn/csrc/c100028/common_list.shtml"
        self.base_url = "http://www.csrc.gov.cn"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; FTSeek-US/2.0)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._seen_ids: Set[str] = set()  # 去重缓存
    
    @property
    def source_id(self) -> str:
        return NewsSource.CSRC_NEWS
    
    @property
    def source_name(self) -> str:
        return "CSRC 证监会官网"
    
    async def fetch(self) -> List[UnifiedNewsItemV2]:
        """异步采集证监会公告"""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch_sync)
    
    def _fetch_sync(self) -> List[UnifiedNewsItemV2]:
        """同步采集 (在 executor 中运行)"""
        items = []
        
        try:
            resp = self.session.get(self.list_url, timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            
            soup = BeautifulSoup(resp.content, "html.parser")
            
            # 解析新闻列表
            for item_elem in soup.find_all("li"):
                try:
                    link_elem = item_elem.find("a")
                    date_elem = item_elem.find("span", class_="date") or item_elem.find("em")
                    
                    if not link_elem:
                        continue
                    
                    title = link_elem.get_text(strip=True)
                    link = link_elem.get("href", "")
                    date_str = date_elem.get_text(strip=True) if date_elem else ""
                    
                    # 检查是否包含监管关键词
                    urgency = self._check_regulatory_keywords(title)
                    if urgency is None:
                        continue  # 不相关
                    
                    # 提取受影响公司
                    related_tickers = self._extract_related_tickers(title)
                    if not related_tickers:
                        continue  # 未关联到 watchlist 公司
                    
                    # 解析时间
                    timestamp = self._parse_csrc_date(date_str)
                    if not timestamp:
                        timestamp = time.time()  # fallback: 当前时间
                    
                    # 只保留最近 48 小时
                    if time.time() - timestamp > 172800:
                        continue
                    
                    # 生成唯一 ID
                    item_id = f"csrc_{hashlib.md5(title.encode()).hexdigest()[:12]}"
                    
                    # 去重
                    if item_id in self._seen_ids:
                        continue
                    self._seen_ids.add(item_id)
                    
                    item = UnifiedNewsItemV2(
                        id=item_id,
                        title=title,
                        content=title,  # 标题作为摘要
                        source=self.source_id,
                        source_name=self.source_name,
                        timestamp=timestamp,
                        urgency=urgency,
                        related_stocks=related_tickers,
                        market_tags=["CN", "US"],
                        raw_url=urljoin(self.base_url, link) if link else "",
                    )
                    items.append(item)
                    
                except Exception as e:
                    logger.debug(f"[CSRC] 解析条目失败: {e}")
                    continue
            
            # 限制 seen_ids 大小 (防止内存泄漏)
            if len(self._seen_ids) > 1000:
                self._seen_ids = set(list(self._seen_ids)[-500:])
            
            logger.info(f"[CSRC] 采集完成: {len(items)} 条监管公告")
            
        except Exception as e:
            logger.warning(f"[CSRC] 采集失败: {e}")
        
        return items
    
    def _check_regulatory_keywords(self, title: str) -> str:
        """检查监管关键词，返回 urgency 或 None"""
        for keyword, urgency in REGULATORY_KEYWORDS.items():
            if keyword in title:
                return urgency
        return None
    
    def _extract_related_tickers(self, title: str) -> List[str]:
        """从标题提取受影响的 US ticker"""
        tickers = set()
        for company_name, ticker_list in COMPANY_TICKER_MAPPING.items():
            if company_name in title:
                tickers.update(ticker_list)
        return list(tickers)
    
    def _parse_csrc_date(self, date_str: str) -> float:
        """解析 CSRC 日期格式 (YYYY-MM-DD 或 YYYY年MM月DD日)"""
        try:
            from datetime import datetime
            # 尝试多种格式
            for fmt in ["%Y-%m-%d", "%Y年%m月%d日", "%Y.%m.%d"]:
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    # CSRC 时间是 CST (UTC+8)
                    return dt.timestamp() - 8 * 3600  # 转换为 UTC
                except ValueError:
                    continue
            return 0.0
        except Exception:
            return 0.0
