"""
FTSeek-US HKEX 新闻源 — 港交所披露易 (一手信息)

特点:
1. 上市公司公告一手源 (公司直接提交)
2. 覆盖 watchlist 中有港股代码的公司
3. 轮询间隔 2 分钟 (120秒)
4. 筛选关键词: 公司名 + 常见公告类型

URL: https://www1.hkexnews.hk/search/titlesearch.xhtml
"""

import hashlib
import logging
import re
import time
from typing import List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.hkex")

# 港股代码 → 公司名映射 (watchlist 中有港股的公司)
HK_STOCK_MAPPING = {
    "00700": ("腾讯", "US.TCEHY"),      # Tencent
    "09988": ("阿里巴巴", "US.BABA"),    # Alibaba
    "09618": ("京东", "US.JD"),          # JD.com
    "03690": ("美团", "US.MPNGY"),       # Meituan
    "09888": ("百度", "US.BIDU"),        # Baidu
    "01810": ("小米", "US.XIAOMY"),      # Xiaomi
    "09999": ("网易", "US.NTES"),        # NetEase
}

# 公告类型关键词 → urgency 映射
FILING_KEYWORDS = {
    "盈利预告": NewsUrgency.HIGH,
    "业绩公告": NewsUrgency.HIGH,
    "收购": NewsUrgency.HIGH,
    "出售": NewsUrgency.NORMAL,
    "股份回购": NewsUrgency.NORMAL,
    "董事会变更": NewsUrgency.HIGH,
    "监管": NewsUrgency.IMMEDIATE,
    "调查": NewsUrgency.IMMEDIATE,
    "处罚": NewsUrgency.IMMEDIATE,
    "停牌": NewsUrgency.IMMEDIATE,
    "复牌": NewsUrgency.HIGH,
}


class HKEXSource(SourceAdapter):
    """港交所披露易新闻源"""
    
    def __init__(self, interval_seconds: int = 120):
        super().__init__(interval_seconds=interval_seconds)
        self.search_url = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
        self.base_url = "https://www1.hkexnews.hk"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; FTSeek-US/2.0)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
    
    @property
    def source_id(self) -> str:
        return NewsSource.HKEX_NEWS
    
    @property
    def source_name(self) -> str:
        return "HKEX 港交所披露易"
    
    async def fetch(self) -> List[UnifiedNewsItemV2]:
        """异步采集港交所公告"""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch_sync)
    
    def _fetch_sync(self) -> List[UnifiedNewsItemV2]:
        """同步采集 (在 executor 中运行)"""
        items = []
        
        for stock_code, (company_name, us_ticker) in HK_STOCK_MAPPING.items():
            try:
                company_items = self._fetch_company_filings(stock_code, company_name, us_ticker)
                items.extend(company_items)
            except Exception as e:
                logger.warning(f"[HKEX] {company_name}({stock_code}) 采集失败: {e}")
                continue
        
        logger.info(f"[HKEX] 采集完成: {len(items)} 条公告")
        return items
    
    def _fetch_company_filings(self, stock_code: str, company_name: str, 
                               us_ticker: str) -> List[UnifiedNewsItemV2]:
        """采集单个公司的最新公告"""
        params = {
            "lang": "ZH",
            "stock": stock_code,
            "from": "0",
            "count": "10",  # 最新10条
        }
        
        resp = self.session.get(self.search_url, params=params, timeout=15)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.content, "html.parser")
        items = []
        
        # 解析公告列表
        for row in soup.find_all("tr", class_="row0") + soup.find_all("tr", class_="row1"):
            try:
                title_elem = row.find("td", class_="title")
                date_elem = row.find("td", class_="date")
                link_elem = row.find("a")
                
                if not title_elem or not date_elem:
                    continue
                
                title = title_elem.get_text(strip=True)
                date_str = date_elem.get_text(strip=True)
                link = link_elem.get("href", "") if link_elem else ""
                
                # 解析时间
                timestamp = self._parse_hkex_date(date_str)
                if not timestamp:
                    continue
                
                # 只保留最近 24 小时的公告
                if time.time() - timestamp > 86400:
                    continue
                
                # 判断 urgency
                urgency = self._classify_urgency(title)
                
                # 生成唯一 ID
                item_id = f"hkex_{stock_code}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
                
                item = UnifiedNewsItemV2(
                    id=item_id,
                    title=f"[{company_name}] {title}",
                    content=title,  # 标题作为摘要
                    source=self.source_id,
                    source_name=self.source_name,
                    timestamp=timestamp,
                    urgency=urgency,
                    related_stocks=[us_ticker],
                    market_tags=["HK", "US"],
                    raw_url=urljoin(self.base_url, link) if link else "",
                )
                items.append(item)
                
            except Exception as e:
                logger.debug(f"[HKEX] 解析行失败: {e}")
                continue
        
        return items
    
    def _parse_hkex_date(self, date_str: str) -> float:
        """解析 HKEX 日期格式 (YYYY/MM/DD HH:MM)"""
        try:
            from datetime import datetime
            # 格式: 2026/05/27 14:30
            dt = datetime.strptime(date_str, "%Y/%m/%d %H:%M")
            # HKEX 时间是 HKT (UTC+8)
            return dt.timestamp() - 8 * 3600  # 转换为 UTC
        except Exception:
            return 0.0
    
    def _classify_urgency(self, title: str) -> str:
        """根据标题关键词分类 urgency"""
        for keyword, urgency in FILING_KEYWORDS.items():
            if keyword in title:
                return urgency
        return NewsUrgency.NORMAL
