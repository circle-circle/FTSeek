"""
华尔街见闻源 (wallstreetcn.com) — 国内可达, 美股深度 + 央行解读

API: https://api-prod.wallstreetcn.com/apiv1/content/articles
特点:
- 美股专题质量高, 翻译及时准确, 含分析
- 含央行解读 (FOMC/ECB/BOJ)
- 国内 CDN, 500ms 延迟
- 60s 抓 1 次 即可

筛选:
- channel 过滤 (us-stock / fed / global-macro)
- 默认 60s 拉 1 次
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time
from typing import Dict, List, Optional, Set

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.wallstreetcn")

WSCN_API = "https://api-prod.wallstreetcn.com/apiv1/content/articles"

# 重要 channel
DEFAULT_CHANNELS = [
    {"key": "us-stock", "label": "美股"},
    {"key": "fed", "label": "美联储"},
    {"key": "global-macro", "label": "宏观"},
    {"key": "earnings", "label": "财报"},
]


class WallstreetCNSource(SourceAdapter):
    """华尔街见闻 (wallstreetcn.com)"""

    def __init__(
        self,
        interval_seconds: int = 60,
        channels: Optional[List[str]] = None,
        proxy: Optional[str] = None,
        max_per_channel: int = 20,
    ):
        super().__init__(interval_seconds=interval_seconds)
        self._channels = list(channels or [c["key"] for c in DEFAULT_CHANNELS])
        self._proxy = proxy
        self._max_per_channel = max_per_channel
        self._seen_ids: Set[str] = set()
        self._max_seen = 1000

    @property
    def source_id(self) -> str:
        return NewsSource.WALLSTREETCN

    @property
    def source_name(self) -> str:
        return "华尔街见闻"

    async def fetch(self) -> List[UnifiedNewsItemV2]:
        all_items: List[UnifiedNewsItemV2] = []
        for ch in self._channels:
            try:
                items = await self._fetch_channel(ch)
                all_items.extend(items)
            except Exception as e:
                logger.warning(f"[WallstreetCN·{ch}] 拉取失败 (跳过): {e}")
        return all_items

    async def _fetch_channel(self, channel: str) -> List[UnifiedNewsItemV2]:
        try:
            import requests
        except ImportError:
            logger.error("[WallstreetCN] requests 库未安装")
            return []

        params = {
            "channel": channel,
            "limit": self._max_per_channel,
            "cursor": "",
            "type": "all",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://wallstreetcn.com/",
            "Accept": "application/json",
        }
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None

        def _do():
            r = requests.get(WSCN_API, params=params, headers=headers, proxies=proxies, timeout=15)
            r.raise_for_status()
            return r.json()

        data = await asyncio.to_thread(_do)
        articles = data.get("data", {}).get("items", []) or []
        return self._parse_articles(articles, channel)

    def _parse_articles(self, articles: List[Dict], channel: str) -> List[UnifiedNewsItemV2]:
        items: List[UnifiedNewsItemV2] = []
        for a in articles:
            try:
                # WSCN 文章结构: {"resource": {"id":..., "title":..., "content_short":...}}
                resource = a.get("resource", a)
                if not isinstance(resource, dict):
                    continue
                aid = str(resource.get("id") or "")
                title = (resource.get("title") or "").strip()
                content = (
                    resource.get("content_short")
                    or resource.get("content_text")
                    or resource.get("summary")
                    or ""
                ).strip()
                ts_field = (
                    resource.get("display_time")
                    or resource.get("created_at")
                    or resource.get("published_at")
                )
                if isinstance(ts_field, str):
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(ts_field.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        ts = time.time()
                elif isinstance(ts_field, (int, float)):
                    ts = float(ts_field)
                else:
                    ts = time.time()

                if not title:
                    continue
                if aid and aid in self._seen_ids:
                    continue

                # N9 (2026-05-14): 智能相关性 + ticker 抽取 (含上下文消歧)
                try:
                    from app.news_v2.quality.smart_relevance import get_smart_relevance
                    rel = get_smart_relevance().evaluate(title, content)
                    tickers = rel.primary_tickers
                    # WSCN 自带板块标签, 不强制相关性 (channel 已限定 us-stock)
                except Exception:
                    # 降级: 旧关键词逻辑
                    tickers = self._extract_tickers(title + " " + content)

                # 紧急度
                urgency = self._infer_urgency(title + " " + content, channel)

                uid = hashlib.md5(f"wscn|{aid}|{title[:64]}".encode("utf-8")).hexdigest()[:16]

                item = UnifiedNewsItemV2(
                    id=uid,
                    title=title,
                    content=content[:1500],
                    source=NewsSource.WALLSTREETCN,
                    source_name=f"华尔街见闻·{channel}",
                    timestamp=ts,
                    raw_url=f"https://wallstreetcn.com/articles/{aid}" if aid else "",
                    related_stocks=[f"US.{t}" for t in tickers],
                    market_tags=["US"],
                    urgency=urgency,
                )
                items.append(item)
                if aid:
                    self._seen_ids.add(aid)
            except Exception as e:
                logger.debug(f"[WallstreetCN] 解析单条失败 (跳过): {e}")
                continue

        if len(self._seen_ids) > self._max_seen:
            self._seen_ids = set(list(self._seen_ids)[-self._max_seen // 2:])

        return items

    def _extract_tickers(self, text: str) -> List[str]:
        """复用财联社的 ticker 抽取逻辑 (中英文别名映射)"""
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
                # 中文别名 case-sensitive 匹配
                if any(c > "" for c in n):
                    if n in text:
                        found.add(tk)
                else:
                    if n.upper() in text_upper:
                        found.add(tk)

        # 直接 ticker (3-5 大写字母)
        import re
        for m in re.finditer(r"\b([A-Z]{2,5})\b", text):
            tk = m.group(1)
            if tk in DEFAULT_SYMBOL_ZH_MAP:
                found.add(tk)

        return list(found)[:5]

    def _infer_urgency(self, text: str, channel: str) -> str:
        if channel == "fed":
            return NewsUrgency.HIGH
        high_kws = ["突发", "美联储", "FOMC", "决议", "暴跌", "暴涨", "停牌", "崩盘"]
        med_kws = ["财报", "EPS", "指引", "评级", "并购", "回购"]
        for kw in high_kws:
            if kw in text:
                return NewsUrgency.HIGH
        for kw in med_kws:
            if kw in text:
                return NewsUrgency.NORMAL
        return NewsUrgency.LOW
