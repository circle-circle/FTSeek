"""
FTSeek-US Finnhub 突发新闻源

数据源:
- Finnhub Company News API
- Finnhub General News API

需要 FINNHUB_API_KEY 环境变量

熔断机制: 连续 401/403 达到阈值后自动禁用以避免日志噪音
"""

import os
import hashlib
import time
import logging
from typing import List, Dict, Optional
from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.finnhub")


class FinnhubNewsSource(SourceAdapter):
    """
    Finnhub 新闻源适配器

    支持:
    1. 个股新闻 (按 watchlist)
    2. 宏观新闻 (general news)
    """

    _CIRCUIT_BREAKER_LIMIT = 5
    _CIRCUIT_COOLDOWN_SECONDS = 3600

    def __init__(self, interval_seconds: int = 120, watchlist_codes: List[str] = None):
        super().__init__(interval_seconds=interval_seconds)
        self.api_key = os.getenv("FINNHUB_API_KEY", "")
        self.watchlist_codes = [c.replace("US.", "") for c in (watchlist_codes or [])]
        # DEB-20260904 #162: 水位改按 symbol 维度 — 原全源全局值会被前一标的的新文章推进,
        # 误抑制其他标的较旧但仍有效的文章; 沿用原语义(启动回看 1h, 纯内存无持久化)
        self._watermark_init = int(time.time()) - 3600
        self._last_timestamps: Dict[str, int] = {}
        self._consecutive_auth_failures = 0
        self._circuit_open_until = 0.0

    @property
    def source_id(self) -> str:
        return NewsSource.FINNHUB_NEWS

    @property
    def source_name(self) -> str:
        return "Finnhub 突发新闻"

    def _circuit_is_open(self) -> bool:
        """检查熔断器是否开启"""
        if self._circuit_open_until > time.time():
            return True
        if self._circuit_open_until > 0 and self._circuit_open_until <= time.time():
            logger.info("Finnhub 熔断冷却结束，恢复尝试")
            self._consecutive_auth_failures = 0
            self._circuit_open_until = 0.0
        return False

    def _record_auth_failure(self):
        self._consecutive_auth_failures += 1
        if self._consecutive_auth_failures >= self._CIRCUIT_BREAKER_LIMIT:
            self._circuit_open_until = time.time() + self._CIRCUIT_COOLDOWN_SECONDS
            logger.warning(
                "Finnhub 连续 %d 次认证失败，熔断 %d 秒",
                self._consecutive_auth_failures,
                self._CIRCUIT_COOLDOWN_SECONDS,
            )

    def _fetch_with_retry(self, url: str, params: Dict) -> Optional[List[Dict]]:
        import requests
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    self._consecutive_auth_failures = 0
                    return resp.json()
                elif resp.status_code in (401, 403):
                    self._record_auth_failure()
                    return []
                elif resp.status_code == 429:
                    time.sleep(2 ** attempt)
                else:
                    logger.debug("Finnhub HTTP %d", resp.status_code)
                    return []
            except Exception as e:
                if attempt < 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.debug("Finnhub 请求失败: %s", e)
                    return []
        return []

    async def fetch(self) -> List[UnifiedNewsItemV2]:
        if not self.api_key:
            return []

        if self._circuit_is_open():
            return []

        items = []

        for symbol in self.watchlist_codes[:5]:
            url = "https://finnhub.io/api/v1/company-news"
            params = {"symbol": symbol, "category": "general", "token": self.api_key}

            loop = __import__('asyncio').get_running_loop()
            data = await loop.run_in_executor(None, self._fetch_with_retry, url, params)

            if data:
                for article in data[:10]:
                    item = self._parse_article(article, symbol)
                    if item:
                        items.append(item)

        return items

    def _parse_article(self, article: Dict, symbol: str = "") -> Optional[UnifiedNewsItemV2]:
        title = article.get("headline", "").strip()
        content = article.get("summary", "").strip()

        if not title:
            return None

        import re
        content = re.sub(r"<[^>]+>", "", content)[:1500]

        timestamp = article.get("datetime", int(time.time()))
        if isinstance(timestamp, str):
            try:
                timestamp = int(timestamp)
            except ValueError:
                timestamp = int(time.time())

        # DEB-20260903 #63: _last_timestamp 原初始化后全文零引用(死代码) —
        # 现用于增量过滤: 跳过不新于上次采集水位(严格更早)的文章, 产出有效 item 时推进水位
        # DEB-20260904 #162: 水位按 symbol 维度取/推进, 避免跨标的互相抑制
        wm = self._last_timestamps.get(symbol, self._watermark_init)
        if timestamp < wm:
            return None

        item_id = f"finnhub_{hashlib.md5(title.encode()).hexdigest()[:8]}"

        urgency = NewsUrgency.NORMAL
        if any(kw in title.lower() for kw in ["breaking", "alert", "urgent"]):
            urgency = NewsUrgency.IMMEDIATE

        related = []
        if symbol:
            related.append(f"US.{symbol}")

        # 推进增量水位 (仅在产出有效 item 时; 按 symbol 维度, DEB-20260904 #162)
        if timestamp > wm:
            self._last_timestamps[symbol] = timestamp

        return UnifiedNewsItemV2(
            id=item_id,
            title=title,
            content=content,
            source=NewsSource.FINNHUB_NEWS,
            source_name="Finnhub",
            timestamp=timestamp,
            urgency=urgency,
            related_stocks=related,
            raw_url=article.get("url", ""),
            market_tags=["US"],
        )
