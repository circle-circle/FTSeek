"""
财联社快讯源 (cls.cn) — 中国大陆 7×24 实时财经快讯

API: https://www.cls.cn/nodeapi/telegraphList (公开接口)
特点:
- 国内可达, 200ms 延迟
- 财联社记者 24h 蹲守, 中文实时快讯
- 美股相关重大事件常常比英文媒体早 1-3 分钟
- 配合 L12 多语言 LLM 处理中文新闻

筛选:
- 中文关键字过滤 (美股/美联储/纳指/标普/symbol_zh)
- 默认 30s 抓 1 次
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

logger = logging.getLogger("ftseek_us.news_v2.sources.cailianpress")

CLS_API_URL = "https://www.cls.cn/nodeapi/telegraphList"

# 默认中文关键字 (yaml 可覆盖)
DEFAULT_KEYWORDS_CN = [
    "美股", "纳指", "标普", "道指", "美联储", "鲍威尔", "FOMC",
    "财报", "EPS", "营收", "指引", "盘前", "盘后", "美元",
    "SEC", "停牌", "拆股", "回购", "并购",
]

# 知名美股标的中文别名 (与 watchlist 关联)
DEFAULT_SYMBOL_ZH_MAP = {
    "NVDA": ["英伟达", "Nvidia"],
    "AAPL": ["苹果", "Apple"],
    "TSLA": ["特斯拉", "Tesla"],
    "MSFT": ["微软", "Microsoft"],
    "GOOG": ["谷歌", "Alphabet", "Google"],
    "AMZN": ["亚马逊", "Amazon"],
    "META": ["Meta", "脸书", "Facebook"],
    "AMD": ["AMD", "超威半导体"],
    "INTC": ["英特尔", "Intel"],
    "TSM": ["台积电", "TSMC"],
    "AVGO": ["博通", "Broadcom"],
    "QCOM": ["高通", "Qualcomm"],
    "CRM": ["Salesforce"],
    "ORCL": ["甲骨文", "Oracle"],
    "NFLX": ["Netflix"],
    "DIS": ["迪士尼", "Disney"],
    "BAC": ["美国银行"],
    "JPM": ["摩根大通"],
    "GS": ["高盛"],
    "WMT": ["沃尔玛", "Walmart"],
    "JNJ": ["强生", "Johnson"],
    "PFE": ["辉瑞", "Pfizer"],
    "MRK": ["默沙东", "Merck"],
    "LLY": ["礼来"],
    "XOM": ["埃克森美孚"],
    "CVX": ["雪佛龙"],
    "BA": ["波音", "Boeing"],
    "F": ["福特", "Ford"],
    "GM": ["通用汽车"],
    "BABA": ["阿里巴巴", "Alibaba"],
    "PDD": ["拼多多"],
    "JD": ["京东"],
    "NIO": ["蔚来"],
    "XPEV": ["小鹏"],
    "LI": ["理想汽车"],
}


class CailianPressSource(SourceAdapter):
    """财联社快讯源 (国内可达)"""

    def __init__(
        self,
        interval_seconds: int = 30,
        keywords: Optional[List[str]] = None,
        symbol_zh_map: Optional[Dict[str, List[str]]] = None,
        proxy: Optional[str] = None,
        max_per_fetch: int = 50,
    ):
        super().__init__(interval_seconds=interval_seconds)
        self._keywords = list(keywords or DEFAULT_KEYWORDS_CN)
        self._symbol_zh_map = dict(symbol_zh_map or DEFAULT_SYMBOL_ZH_MAP)
        self._proxy = proxy
        self._max_per_fetch = max_per_fetch
        self._seen_ids: Set[str] = set()  # 简单 dedup, max 1000
        self._max_seen = 1000

        # 反向索引: 中文别名 → ticker
        self._zh_to_ticker: Dict[str, str] = {}
        for tk, names in self._symbol_zh_map.items():
            for n in names:
                self._zh_to_ticker[n.upper()] = tk

    @property
    def source_id(self) -> str:
        return NewsSource.CAILIANPRESS

    @property
    def source_name(self) -> str:
        return "财联社快讯"

    async def fetch(self) -> List[UnifiedNewsItemV2]:
        """拉最新 N 条快讯"""
        try:
            data = await self._http_get()
            if not data:
                return []
            telegrams = data.get("data", {}).get("roll_data", []) or data.get("data", {}).get("telegrams", []) or []
            if not isinstance(telegrams, list):
                return []
            return self._parse(telegrams)
        except Exception as e:
            logger.warning(f"[CailianPress] 拉取失败: {e}")
            raise

    async def _http_get(self) -> Optional[Dict]:
        """HTTP GET (复用项目已有 requests, 异步 asyncio.to_thread 包装)"""
        try:
            import requests
        except ImportError:
            logger.error("[CailianPress] requests 库未安装")
            return None

        params = {
            "app": "CailianpressWeb",
            "category": "",
            "lastTime": int(time.time() * 1000),
            "last_time": int(time.time()),
            "rn": self._max_per_fetch,
            "os": "web",
            "sv": "7.7.5",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.cls.cn/telegraph",
            "Accept": "application/json",
        }
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None

        def _do():
            r = requests.get(CLS_API_URL, params=params, headers=headers, proxies=proxies, timeout=10)
            r.raise_for_status()
            return r.json()

        return await asyncio.to_thread(_do)

    def _parse(self, telegrams: List[Dict]) -> List[UnifiedNewsItemV2]:
        items: List[UnifiedNewsItemV2] = []
        for t in telegrams:
            try:
                tg_id = str(t.get("id") or t.get("ctime") or "")
                title = (t.get("title") or t.get("subject") or "").strip()
                content = (t.get("content") or t.get("brief") or "").strip()
                ctime = t.get("ctime") or t.get("modified_time") or t.get("time")
                ts = float(ctime) if ctime else time.time()

                if not title and not content:
                    continue
                if tg_id in self._seen_ids:
                    continue

                # N9 (2026-05-14): 智能相关性 (替代纯关键词)
                # 优先用 SmartRelevanceEngine; 失败降级到关键词
                full_text = f"{title} {content}"
                try:
                    from app.news_v2.quality.smart_relevance import get_smart_relevance
                    rel = get_smart_relevance().evaluate(title, content)
                    if not rel.is_relevant:
                        # 智能引擎判断不相关 → 跳过
                        continue
                    matched_tickers = rel.primary_tickers
                    matched_kws = ["smart_relevant"] if rel.macro_hit else []
                except Exception as _e:
                    # 降级: 旧关键词逻辑
                    matched_kws = [kw for kw in self._keywords if kw in full_text]
                    matched_tickers = self._extract_tickers(full_text)
                    if not matched_kws and not matched_tickers:
                        continue

                # 紧急程度: 含"美联储"/"FOMC"/"鲍威尔" 升 HIGH; 含 "财报"/"突发" 升 MED
                urgency = self._infer_urgency(full_text)

                # 唯一 hash
                uid_src = f"{tg_id}|{title[:64]}"
                uid = hashlib.md5(uid_src.encode("utf-8")).hexdigest()[:16]

                item = UnifiedNewsItemV2(
                    id=uid,
                    title=title or content[:80],
                    content=content or title,
                    source=NewsSource.CAILIANPRESS,
                    source_name="财联社",
                    timestamp=ts,
                    raw_url=f"https://www.cls.cn/detail/{tg_id}",
                    related_stocks=[f"US.{t}" for t in matched_tickers],
                    market_tags=["US"],
                    urgency=urgency,
                )
                items.append(item)
                self._seen_ids.add(tg_id)
            except Exception as e:
                logger.debug(f"[CailianPress] 解析单条失败 (跳过): {e}")
                continue

        # 限制 _seen_ids 内存
        if len(self._seen_ids) > self._max_seen:
            # 简单 LRU: 转 list 砍头
            self._seen_ids = set(list(self._seen_ids)[-self._max_seen // 2:])

        return items

    def _extract_tickers(self, text: str) -> List[str]:
        """从中文文本抽取 US ticker (依赖中文别名 + 直接 ticker)"""
        text_upper = text.upper()
        found: Set[str] = set()

        # 1. 中文别名匹配
        for name_upper, ticker in self._zh_to_ticker.items():
            # 中文匹配走原文 (不要 upper)
            if any(c > "" for c in name_upper):  # 非 ASCII 走 case-sensitive
                # 找中文原文
                for tk, names in self._symbol_zh_map.items():
                    for n in names:
                        if n in text:
                            found.add(tk)
            else:
                if name_upper in text_upper:
                    found.add(ticker)

        # 2. 直接 ticker 匹配 (3-5 大写字母)
        import re
        for m in re.finditer(r"\b([A-Z]{2,5})\b", text):
            tk = m.group(1)
            if tk in self._symbol_zh_map:
                found.add(tk)

        return list(found)[:5]

    def _infer_urgency(self, text: str) -> str:
        """推断紧急度"""
        high_kws = ["突发", "美联储", "FOMC", "鲍威尔", "停牌", "熔断", "暴跌", "崩盘",
                    "黑天鹅", "重大", "紧急", "决议"]
        med_kws = ["财报", "EPS", "指引", "评级", "上调", "下调", "并购"]
        for kw in high_kws:
            if kw in text:
                return NewsUrgency.HIGH
        for kw in med_kws:
            if kw in text:
                return NewsUrgency.NORMAL
        return NewsUrgency.LOW
