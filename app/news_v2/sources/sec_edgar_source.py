"""
FTSeek-US SEC EDGAR 公告采集源

数据源:
- SEC EDGAR RSS Feed (8-K, 10-Q, 10-K, 4 等)

注意: 该 Feed 为全市场流, 仅在配置 watchlist_codes 时按代码过滤;
未配置时返回全量公告 (不保证只含 watchlist 公司)。
"""

import hashlib
import time
import logging
from typing import List, Optional
from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.sec_edgar")

FILING_URGENCY = {
    "8-K": NewsUrgency.HIGH,
    "10-Q": NewsUrgency.NORMAL,
    "10-K": NewsUrgency.HIGH,
    "4": NewsUrgency.LOW,
    "SC 13D": NewsUrgency.HIGH,
    "DEF 14A": NewsUrgency.NORMAL,
}


class SECEdgarSource(SourceAdapter):
    """SEC EDGAR 公告采集"""
    
    def __init__(self, interval_seconds: int = 300, watchlist_codes: list = None):
        super().__init__(interval_seconds=interval_seconds)
        self.watchlist_codes = [c.replace("US.", "") for c in (watchlist_codes or [])]
        self.user_agent = "FTSeek-US/2.0"
    
    @property
    def source_id(self) -> str:
        return NewsSource.SEC_EDGAR
    
    @property
    def source_name(self) -> str:
        return "SEC EDGAR 公告"
    
    async def fetch(self) -> List[UnifiedNewsItemV2]:
        import requests
        
        url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
        
        loop = __import__('asyncio').get_running_loop()
        
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: requests.get(url, timeout=15, headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/xml,application/xml",
                })
            )
            
            if resp.status_code != 200:
                return []
            
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.content)
            
            items = []
            for entry in root.findall(".//entry"):
                title_elem = entry.find("{http://www.w3.org/2005/Atom}title")
                summary_elem = entry.find("{http://www.w3.org/2005/Atom}summary")
                updated_elem = entry.find("{http://www.w3.org/2005/Atom}updated")
                link_elem = entry.find("{http://www.w3.org/2005/Atom}link")
                
                if title_elem is None:
                    continue
                
                title = title_elem.text.strip()
                summary = summary_elem.text.strip() if summary_elem is not None else ""
                link = link_elem.get("href", "") if link_elem is not None else ""
                
                timestamp = time.time()
                if updated_elem is not None and updated_elem.text:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(updated_elem.text.replace("Z", "+00:00"))
                        timestamp = dt.timestamp()
                    except Exception:
                        pass
                
                filing_type = ""
                for cat in entry.findall("{http://www.w3.org/2005/Atom}category"):
                    term = cat.get("term", "")
                    if term in FILING_URGENCY:
                        filing_type = term
                        break
                
                if not filing_type:
                    filing_type = "Other"
                
                urgency = FILING_URGENCY.get(filing_type, NewsUrgency.NORMAL)

                # DEB-20260903 #66: 按 watchlist 过滤 — 原实现保存了 watchlist_codes
                # 却全程未用, 直接返回全市场公告, 与模块 docstring 声称的
                # "只采集 watchlist 公司公告" 不符。配置非空时按代码在标题/正文中
                # 的大小写不敏感匹配过滤; 为空时保持全量 (调用方自行承担过滤)。
                if self.watchlist_codes:
                    # DEB-20260904 #S9: 子串匹配会把 US.A 的 'A' 匹配进任意单词 →
                    # 非 watchlist 公告漏过过滤。剥掉市场前缀, 用词边界正则匹配裸 ticker。
                    import re as _re
                    haystack = (title + " " + summary).upper()
                    _hit = False
                    for code in self.watchlist_codes:
                        _tk = str(code).upper().replace("US.", "").strip()
                        if _tk and _re.search(rf"\b{_re.escape(_tk)}\b", haystack):
                            _hit = True
                            break
                    if not _hit:
                        continue

                item_id = f"sec_{hashlib.md5(title.encode()).hexdigest()[:8]}"
                
                items.append(UnifiedNewsItemV2(
                    id=item_id,
                    title=f"[{filing_type}] {title}",
                    content=summary[:1500],
                    source=NewsSource.SEC_EDGAR,
                    source_name="SEC EDGAR",
                    timestamp=timestamp,
                    urgency=urgency,
                    raw_url=link,
                    market_tags=["US", "SEC"],
                ))
                
                if len(items) >= 20:
                    break
            
            logger.info("[SEC EDGAR] 采集 %d 条公告", len(items))
            return items
            
        except Exception as e:
            logger.warning("[SEC EDGAR] 采集失败: %s", e)
            return []
