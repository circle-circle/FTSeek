"""
TickerDictionary 动态构建 (N7 / 智能新闻 L0, 2026-05-14)

从多源综合构建 ticker → 上下文实体词典:
- watchlist 当前关注标的
- trade_records 历史交易过的标的
- yaml `news_relevance.entities` 静态映射 (品牌/产品/CEO 等)
- (可选) tushare/akshare 拉全市场 (Phase B)

为每个 ticker 维护:
- names: ["NVDA", "Nvidia", "英伟达"]
- products: ["GeForce", "CUDA", "H100", "Blackwell", ...]
- people: ["Jensen Huang", "黄仁勋"]
- competitors: ["AMD", "INTC"]
- sector: "Technology"
- aliases: ["NVDA Corp", "Nvidia Corp"]

可控:
- 无外部依赖 (纯 Python)
- 词典每日刷新一次 (启动 + 每 24h)
- yaml 静态映射 + 自动汇入历史持仓
"""
from __future__ import annotations
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TickerEntity:
    """单个 ticker 的实体词集"""
    ticker: str
    names: List[str] = field(default_factory=list)         # 公司名 (含中英)
    products: List[str] = field(default_factory=list)      # 产品/服务/技术
    people: List[str] = field(default_factory=list)        # CEO/创始人
    competitors: List[str] = field(default_factory=list)
    sector: str = ""
    aliases: List[str] = field(default_factory=list)       # 别名/简称

    def all_signal_words(self) -> Set[str]:
        """返回所有"上下文信号词" (用于消歧 — 命中这些才确证是该 ticker)"""
        return set(self.products + self.people + self.competitors)

    def all_name_variants(self) -> Set[str]:
        """所有可能识别的名称 (用于初筛)"""
        return set(self.names + self.aliases + [self.ticker])


# ─── 静态默认词集 (国内常见知名美股) ──────────────────
# 这是种子数据, 重要标的内置, 其他从 yaml/DB 自动汇入
DEFAULT_ENTITIES: Dict[str, TickerEntity] = {
    "NVDA": TickerEntity(
        ticker="NVDA",
        names=["Nvidia", "NVIDIA", "英伟达", "辉达"],
        products=["GeForce", "RTX", "GTX", "CUDA", "H100", "H200", "B100", "B200",
                  "Blackwell", "Hopper", "Ampere", "Grace", "DGX", "Omniverse"],
        people=["Jensen Huang", "黄仁勋"],
        competitors=["AMD", "INTC", "TSM"],
        sector="Technology",
        aliases=["NVDA Corp", "Nvidia Corp"],
    ),
    "AAPL": TickerEntity(
        ticker="AAPL",
        names=["Apple", "苹果公司", "苹果"],
        products=["iPhone", "iPad", "Mac", "MacBook", "iMac", "Apple Watch",
                  "AirPods", "iOS", "macOS", "Vision Pro", "App Store",
                  "Siri", "Apple Pay", "Cupertino"],
        people=["Tim Cook", "库克", "蒂姆·库克", "Steve Jobs", "乔布斯"],
        competitors=["GOOG", "MSFT", "META"],
        sector="Technology",
    ),
    "TSLA": TickerEntity(
        ticker="TSLA",
        names=["Tesla", "特斯拉"],
        products=["Model S", "Model 3", "Model X", "Model Y", "Cybertruck",
                  "Roadster", "FSD", "Autopilot", "Powerwall", "Megapack",
                  "Supercharger", "Optimus"],
        people=["Elon Musk", "马斯克", "埃隆·马斯克"],
        competitors=["NIO", "XPEV", "LI", "BYD", "RIVN"],
        sector="Consumer Discretionary",
    ),
    "MSFT": TickerEntity(
        ticker="MSFT",
        names=["Microsoft", "微软"],
        products=["Windows", "Office", "Azure", "GitHub", "Xbox", "Surface",
                  "Teams", "Copilot", "OpenAI", "ChatGPT", "Bing"],
        people=["Satya Nadella", "纳德拉"],
        competitors=["GOOG", "AAPL", "AMZN"],
        sector="Technology",
    ),
    "GOOG": TickerEntity(
        ticker="GOOG",
        names=["Google", "谷歌", "Alphabet", "字母控股"],
        products=["Search", "Android", "Chrome", "YouTube", "Gmail", "Maps",
                  "Pixel", "Gemini", "Bard", "Waymo", "TPU", "DeepMind"],
        people=["Sundar Pichai", "皮查伊"],
        competitors=["MSFT", "AAPL", "META"],
        sector="Communication",
    ),
    "META": TickerEntity(
        ticker="META",
        names=["Meta", "脸书", "Facebook", "Meta Platforms"],
        products=["Facebook", "Instagram", "WhatsApp", "Messenger", "Quest", "Reels",
                  "Threads", "Llama", "Reality Labs"],
        people=["Mark Zuckerberg", "扎克伯格", "小扎"],
        competitors=["GOOG", "SNAP", "PINS"],
        sector="Communication",
    ),
    "AMZN": TickerEntity(
        ticker="AMZN",
        names=["Amazon", "亚马逊"],
        products=["AWS", "Prime", "Kindle", "Alexa", "Echo", "Fire TV",
                  "Whole Foods", "Audible", "Twitch"],
        people=["Andy Jassy", "贾西", "Jeff Bezos", "贝佐斯"],
        competitors=["WMT", "COST", "MSFT"],
        sector="Consumer Discretionary",
    ),
    "AMD": TickerEntity(
        ticker="AMD",
        names=["AMD", "超威半导体", "超微"],
        products=["Ryzen", "EPYC", "Radeon", "Instinct", "MI300", "MI325",
                  "Threadripper", "Zen"],
        people=["Lisa Su", "苏姿丰"],
        competitors=["NVDA", "INTC"],
        sector="Technology",
    ),
    "INTC": TickerEntity(
        ticker="INTC",
        names=["Intel", "英特尔"],
        products=["Core", "Xeon", "Pentium", "Celeron", "Arc", "Foundry"],
        people=["Pat Gelsinger", "盖尔辛格"],
        competitors=["AMD", "NVDA", "TSM"],
        sector="Technology",
    ),
    "TSM": TickerEntity(
        ticker="TSM",
        names=["TSMC", "台积电", "Taiwan Semiconductor"],
        products=["3nm", "5nm", "7nm", "晶圆代工", "EUV", "N3", "N5"],
        people=["Mark Liu", "魏哲家", "刘德音"],
        competitors=["INTC", "Samsung", "GFS"],
        sector="Technology",
    ),
    "AVGO": TickerEntity(
        ticker="AVGO",
        names=["Broadcom", "博通"],
        products=["VMware", "Tomahawk", "Jericho"],
        people=["Hock Tan", "陈福阳"],
        competitors=["QCOM", "MRVL"],
        sector="Technology",
    ),
    "BABA": TickerEntity(
        ticker="BABA",
        names=["Alibaba", "阿里巴巴", "阿里"],
        products=["淘宝", "天猫", "Aliyun", "阿里云", "蚂蚁集团", "饿了么", "菜鸟"],
        people=["Jack Ma", "马云", "张勇"],
        competitors=["PDD", "JD"],
        sector="China ADR",
    ),
    "PDD": TickerEntity(
        ticker="PDD",
        names=["拼多多", "Temu", "Pinduoduo"],
        products=["拼多多", "Temu"],
        people=["黄峥", "陈磊"],
        competitors=["BABA", "JD"],
        sector="China ADR",
    ),
    "NIO": TickerEntity(
        ticker="NIO",
        names=["蔚来", "Nio"],
        products=["ET5", "ET7", "ES6", "ES8", "EC6"],
        people=["李斌"],
        competitors=["XPEV", "LI", "TSLA"],
        sector="China ADR",
    ),
}


class TickerDictionary:
    """综合 ticker 词典 (单例, 自动刷新)"""

    def __init__(self):
        self._entities: Dict[str, TickerEntity] = {}
        self._name_to_ticker: Dict[str, str] = {}        # 用于 O(1) 反查
        self._signal_to_tickers: Dict[str, Set[str]] = {}  # 信号词 → 候选 tickers
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._build_initial()

    def _build_initial(self):
        """启动时载入静态默认词集"""
        with self._lock:
            for tk, ent in DEFAULT_ENTITIES.items():
                self._entities[tk] = ent
            self._rebuild_indexes()
        logger.info(f"[TickerDict] 静态词集载入 {len(self._entities)} 标的")

    def add_entity(self, entity: TickerEntity):
        """运行时增/覆盖一个 ticker 的词集 (yaml/DB 调用)"""
        with self._lock:
            existing = self._entities.get(entity.ticker)
            if existing:
                # 合并 (扩展, 不覆盖)
                existing.names = list(set(existing.names + entity.names))
                existing.products = list(set(existing.products + entity.products))
                existing.people = list(set(existing.people + entity.people))
                existing.competitors = list(set(existing.competitors + entity.competitors))
                existing.aliases = list(set(existing.aliases + entity.aliases))
                if entity.sector:
                    existing.sector = entity.sector
            else:
                self._entities[entity.ticker] = entity
            self._rebuild_indexes()

    def _rebuild_indexes(self):
        """重建反向索引 — 在 _lock 内调"""
        self._name_to_ticker.clear()
        self._signal_to_tickers.clear()
        for tk, ent in self._entities.items():
            for n in ent.all_name_variants():
                self._name_to_ticker[n.upper()] = tk
                # 中文不做 upper 但作为另一份键
                self._name_to_ticker[n] = tk
            for sw in ent.all_signal_words():
                self._signal_to_tickers.setdefault(sw.upper(), set()).add(tk)
                self._signal_to_tickers.setdefault(sw, set()).add(tk)

    def lookup_by_name(self, name: str) -> Optional[str]:
        """按名称反查 ticker. 不区分大小写匹配 + 中文原文"""
        with self._lock:
            return self._name_to_ticker.get(name) or self._name_to_ticker.get(name.upper())

    def lookup_by_signal(self, signal_word: str) -> Set[str]:
        """信号词反查 (返回所有可能 ticker)"""
        with self._lock:
            return set(self._signal_to_tickers.get(signal_word, set()) |
                       self._signal_to_tickers.get(signal_word.upper(), set()))

    def get(self, ticker: str) -> Optional[TickerEntity]:
        with self._lock:
            return self._entities.get(ticker.upper())

    def all_tickers(self) -> List[str]:
        with self._lock:
            return list(self._entities.keys())

    def refresh_from_sources(self):
        """从 watchlist + trade_records + yaml 综合更新"""
        try:
            self._load_from_yaml()
        except Exception as e:
            logger.debug(f"[TickerDict·yaml] 加载失败: {e}")
        try:
            self._load_from_trade_records()
        except Exception as e:
            logger.debug(f"[TickerDict·db] 加载失败: {e}")
        self._last_refresh = time.time()
        logger.info(f"[TickerDict·refresh] 完成. 当前 {len(self._entities)} 标的")

    def _load_from_yaml(self):
        """从 yaml `news_relevance.entities` 加载"""
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            entities = cfg.get_dotted("news_relevance.entities", {}) or {}
            for tk, data in entities.items():
                if not isinstance(data, dict):
                    continue
                self.add_entity(TickerEntity(
                    ticker=tk.upper(),
                    names=data.get("names", []) or [],
                    products=data.get("products", []) or [],
                    people=data.get("people", []) or [],
                    competitors=data.get("competitors", []) or [],
                    sector=data.get("sector", "") or "",
                    aliases=data.get("aliases", []) or [],
                ))
        except Exception:
            pass

    def _load_from_trade_records(self):
        """从 trade_records.db 拉历史交易过的所有 ticker"""
        try:
            from app.core.paths import get_trade_records_db
            db = str(get_trade_records_db())
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM trade_records "
                "WHERE symbol IS NOT NULL"
            ).fetchall()
            conn.close()
            with self._lock:
                for (sym,) in rows:
                    if sym and sym.upper() not in self._entities:
                        # 历史交易过但无静态词集 → 占位 (至少有 ticker 名)
                        self._entities[sym.upper()] = TickerEntity(
                            ticker=sym.upper(),
                            names=[sym.upper()],
                        )
                self._rebuild_indexes()
        except Exception as e:
            logger.debug(f"[TickerDict·trade_records] {e}")


_singleton: Optional[TickerDictionary] = None


def get_ticker_dictionary() -> TickerDictionary:
    global _singleton
    if _singleton is None:
        _singleton = TickerDictionary()
        try:
            _singleton.refresh_from_sources()
        except Exception:
            pass
    return _singleton
