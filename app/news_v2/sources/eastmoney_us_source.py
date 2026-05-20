"""
东方财富美股新闻源 (eastmoney.com) — 个股层面新闻最全

API: https://np-listapi.eastmoney.com/comm/wap/getListInfo (个股新闻)
特点:
- 国内可达 + 中文
- 个股新闻覆盖广 (财报/评级/重大事项)
- 缺陷: 需按 watchlist 逐 symbol 拉, 默认 120s 轮询
- 适合作为 主线 watchlist 内 symbol 的"事件触发器"

注意:
- 东方财富美股 API 路径在 2024 年改过几次, 此实现尽量容错
- 对每 symbol 仅拉最新 5 条, 避免过载
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Dict, List, Optional, Set

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.eastmoney")

# 东财美股个股新闻 API (示例 path - 可能随时变)
# 实测 2026-05: https://search-api.eastmoney.com/news/getNews?keyword=NVDA&pageIndex=1&pageSize=10
EASTMONEY_NEWS_SEARCH_API = "https://search-api.eastmoney.com/news/getNews"


class EastmoneyUSSource(SourceAdapter):
    """东方财富美股个股新闻源"""

    def __init__(
        self,
        interval_seconds: int = 120,
        symbols: Optional[List[str]] = None,
        proxy: Optional[str] = None,
        per_symbol_limit: int = 5,
        max_symbols_per_fetch: int = 20,
    ):
        super().__init__(interval_seconds=interval_seconds)
        self._symbols = list(symbols or [])
        self._proxy = proxy
        self._per_symbol_limit = per_symbol_limit
        self._max_symbols_per_fetch = max_symbols_per_fetch
        self._seen_ids: Set[str] = set()
        self._max_seen = 2000
        self._symbol_cursor = 0  # 轮转, 避免每轮拉满

    @property
    def source_id(self) -> str:
        return NewsSource.EASTMONEY_US

    @property
    def source_name(self) -> str:
        return "东方财富美股"

    def update_symbols(self, symbols: List[str]):
        """运行时更新 watchlist (scheduler 会主动调)"""
        self._symbols = list(symbols)

    async def fetch(self) -> List[UnifiedNewsItemV2]:
        if not self._symbols:
            logger.debug("[Eastmoney] symbols 列表为空, 跳过")
            return []

        # 每轮拉 max_symbols_per_fetch 个, 轮转
        n = min(self._max_symbols_per_fetch, len(self._symbols))
        batch = []
        for i in range(n):
            idx = (self._symbol_cursor + i) % len(self._symbols)
            batch.append(self._symbols[idx])
        self._symbol_cursor = (self._symbol_cursor + n) % max(1, len(self._symbols))

        all_items: List[UnifiedNewsItemV2] = []
        for sym in batch:
            try:
                items = await self._fetch_symbol(sym)
                all_items.extend(items)
            except Exception as e:
                logger.debug(f"[Eastmoney·{sym}] 拉取失败 (跳过): {e}")

        return all_items

    async def _fetch_symbol(self, symbol: str) -> List[UnifiedNewsItemV2]:
        try:
            import requests
        except ImportError:
            logger.error("[Eastmoney] requests 库未安装")
            return []

        # 用英文 ticker + 中文别名混合搜索
        keyword = symbol.upper()
        params = {
            "keyword": keyword,
            "pageindex": 1,
            "pagesize": self._per_symbol_limit,
            "type": "title",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://so.eastmoney.com/news/",
            "Accept": "application/json",
        }
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None

        def _do():
            r = requests.get(EASTMONEY_NEWS_SEARCH_API, params=params, headers=headers,
                           proxies=proxies, timeout=15)
            r.raise_for_status()
            return r.json()

        try:
            data = await asyncio.to_thread(_do)
        except Exception as e:
            raise RuntimeError(f"东财 API 请求失败: {e}")

        # 兼容多种返回结构
        articles = (
            data.get("Data", [])
            or data.get("data", [])
            or data.get("result", [])
            or []
        )
        if not isinstance(articles, list):
            return []

        return self._parse_articles(articles, symbol)

    def _parse_articles(self, articles: List[Dict], symbol: str) -> List[UnifiedNewsItemV2]:
        items: List[UnifiedNewsItemV2] = []
        for a in articles:
            try:
                aid = str(a.get("Url") or a.get("url") or a.get("ArticleCode") or a.get("id") or "")
                title = (a.get("Title") or a.get("title") or "").strip()
                # 清理 HTML 高亮标记 (东财搜索结果包含 <em>...</em>)
                title = re.sub(r"<[^>]+>", "", title)
                content = (a.get("Content") or a.get("content") or a.get("ShowTime") or "").strip()
                content = re.sub(r"<[^>]+>", "", content)

                ts_field = a.get("ShowTime") or a.get("Date") or a.get("date") or a.get("ArtCreateDate")
                ts = self._parse_ts(ts_field) or time.time()

                # 过滤太老的 (> 24h)
                if time.time() - ts > 86400:
                    continue

                if not title or not aid:
                    continue
                uid_src = f"em|{aid}|{title[:64]}"
                uid = hashlib.md5(uid_src.encode("utf-8")).hexdigest()[:16]
                if uid in self._seen_ids:
                    continue

                # N9 (2026-05-14): 智能 ticker 抽取 (含上下文消歧)
                tickers = [symbol]  # 至少有触发的 symbol
                try:
                    from app.news_v2.quality.smart_relevance import get_smart_relevance
                    rel = get_smart_relevance().evaluate(title, content)
                    extra = rel.primary_tickers + rel.related_tickers
                except Exception:
                    extra = self._extract_extra_tickers(title + " " + content)
                tickers.extend([t for t in extra if t != symbol])
                tickers = list(dict.fromkeys(tickers))[:5]  # dedup + cap

                urgency = self._infer_urgency(title)

                item = UnifiedNewsItemV2(
                    id=uid,
                    title=title,
                    content=content[:1500],
                    source=NewsSource.EASTMONEY_US,
                    source_name=f"东方财富·{symbol}",
                    timestamp=ts,
                    raw_url=aid if aid.startswith("http") else f"https://finance.eastmoney.com/news/{aid}",
                    related_stocks=[f"US.{t}" for t in tickers],
                    market_tags=["US"],
                    urgency=urgency,
                )
                items.append(item)
                self._seen_ids.add(uid)
            except Exception as e:
                logger.debug(f"[Eastmoney·{symbol}] 解析失败 (跳过): {e}")
                continue

        if len(self._seen_ids) > self._max_seen:
            self._seen_ids = set(list(self._seen_ids)[-self._max_seen // 2:])

        return items

    def _parse_ts(self, ts_field) -> Optional[float]:
        """解析时间字段 (东财常见 '2026-05-14 10:30:00' / unix timestamp)"""
        if ts_field is None:
            return None
        if isinstance(ts_field, (int, float)):
            v = float(ts_field)
            return v / 1000 if v > 1e12 else v
        if isinstance(ts_field, str):
            ts_field = ts_field.strip()
            try:
                from datetime import datetime
                # ISO 格式
                return datetime.fromisoformat(ts_field.replace(" ", "T").replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
            # 数字字符串
            try:
                v = float(ts_field)
                return v / 1000 if v > 1e12 else v
            except Exception:
                pass
        return None

    def _extract_extra_tickers(self, text: str) -> List[str]:
        try:
            from app.news_v2.sources.cailianpress_source import DEFAULT_SYMBOL_ZH_MAP
        except Exception:
            DEFAULT_SYMBOL_ZH_MAP = {}

        text_upper = text.upper()
        found: Set[str] = set()
        for tk, names in DEFAULT_SYMBOL_ZH_MAP.items():
            if tk in text_upper:
                found.add(tk)
            for n in names:
                if any(c > "" for c in n):
                    if n in text:
                        found.add(tk)
                elif n.upper() in text_upper:
                    found.add(tk)
        return list(found)

    def _infer_urgency(self, text: str) -> str:
        high_kws = ["停牌", "暴跌", "暴涨", "崩盘", "突发", "重大", "紧急"]
        med_kws = ["财报", "EPS", "评级", "并购", "回购", "上调", "下调"]
        for kw in high_kws:
            if kw in text:
                return NewsUrgency.HIGH
        for kw in med_kws:
            if kw in text:
                return NewsUrgency.NORMAL
        return NewsUrgency.LOW
