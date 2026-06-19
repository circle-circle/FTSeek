"""盘前热点发现器 (Hot Discovery) — 2026-05-28

职责:
    从 watchlist 之外发现盘前异动股 + 新闻催化股，补充到推荐卡片。

数据源:
    1. Futu 盘前行情 — 扫描 watchlist 外的盘前异动 (gap > 3%)
    2. 新闻 DB (us_news) — 提取高频提及的股票 + 催化关键词
    3. Finnhub/TwelveData — 备用行情源

输出:
    hot_picks: List[Dict] — 每只含 symbol, change_pct, catalyst, sector, source, rationale

设计原则:
    - 纯数据采集 + 规则筛选，不依赖 LLM
    - 任何异常返回空列表，不影响主推荐流程
    - 与 battle_plan targets 去重，只推荐 watchlist 外的标的
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── 催化关键词表 (新闻标题匹配 → 信号类型) ──────────────────
CATALYST_KEYWORDS = {
    # 财报相关
    "earnings beat": "财报超预期",
    "earnings miss": "财报不及预期",
    "revenue beat": "营收超预期",
    "guidance raise": "上调指引",
    "guidance cut": "下调指引",
    "财报超预期": "财报超预期",
    "财报不及预期": "财报不及预期",
    "业绩超预期": "业绩超预期",
    # FDA / 生物医药
    "fda approval": "FDA批准",
    "fda reject": "FDA拒绝",
    "phase 3": "三期临床",
    "breakthrough therapy": "突破性疗法",
    # 分析师
    "upgrade": "分析师上调",
    "downgrade": "分析师下调",
    "price target": "目标价调整",
    "overweight": "增持评级",
    # 并购 / 重大事件
    "acquisition": "收购",
    "merger": "合并",
    "buyback": "回购",
    "dividend": "分红",
    "split": "拆股",
    # 宏观 / 行业
    "ai chip": "AI芯片需求",
    "ai demand": "AI需求",
    "tariff": "关税影响",
    "sanctions": "制裁影响",
    "rate cut": "降息预期",
    "rate hike": "加息预期",
    "oil surge": "油价飙升",
    "oil crash": "油价暴跌",
}

# ── 异动阈值 ──────────────────────────────────────────────
MIN_PREMARKET_CHANGE_PCT = 3.0  # 盘前涨跌 >= 3% 视为异动
MIN_NEWS_MENTIONS = 2  # 至少 2 条新闻提及
MAX_HOT_PICKS = 5  # 最多推荐 5 只热点
NEWS_LOOKBACK_HOURS = 12  # 扫描最近 12 小时新闻

# ── 排除列表 (ETF / 低流动性) ─────────────────────────────
ETF_SYMBOLS = {
    "SPY", "QQQ", "DIA", "IWM", "UVXY", "VIX", "TQQQ", "SQQQ",
    "SOXL", "SOXS", "ARKK", "XLF", "XLE", "XLK", "XLV", "GLD",
    "TLT", "HYG", "LQD", "VTI", "VOO",
}


class HotDiscovery:
    """盘前热点发现器"""

    def __init__(self, config: Any = None):
        self._config = config
        self._data_router = None

    # ─── 主入口 ──────────────────────────────────────────────
    def discover(
        self,
        battle_plan: Optional[Dict[str, Any]] = None,
        news_summary_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        扫描盘前异动 + 新闻热点，返回 watchlist 外的热点标的。

        Args:
            battle_plan: 当日作战计划 (用于去重)
            news_summary_text: premarket_planner 已抓取的新闻摘要文本

        Returns:
            hot_picks: 最多 MAX_HOT_PICKS 只，按信号强度排序
        """
        battle_plan = battle_plan or {}

        # 已入选标的 (去重)
        plan_targets: Set[str] = set()
        for tgt in battle_plan.get("targets", []):
            sym = self._clean_symbol(tgt.get("symbol", ""))
            if sym:
                plan_targets.add(sym)

        picks: List[Dict[str, Any]] = []

        # 1. 盘前异动扫描
        try:
            movers = self._scan_premarket_movers(plan_targets)
            picks.extend(movers)
            if movers:
                logger.info(f"[HotDiscovery] 盘前异动: {len(movers)} 只")
        except Exception as e:
            logger.debug(f"[HotDiscovery] 盘前异动扫描失败: {e}")

        # 2. 新闻催化扫描
        try:
            news_picks = self._scan_news_catalysts(plan_targets, news_summary_text)
            picks.extend(news_picks)
            if news_picks:
                logger.info(f"[HotDiscovery] 新闻催化: {len(news_picks)} 只")
        except Exception as e:
            logger.debug(f"[HotDiscovery] 新闻催化扫描失败: {e}")

        # 3. 去重 + 排序 + 截断
        merged = self._merge_and_rank(picks)
        result = merged[:MAX_HOT_PICKS]

        if result:
            logger.info(
                f"[HotDiscovery] 最终热点: {[p['symbol'] for p in result]}"
            )
        return result

    # ─── 盘前异动扫描 ────────────────────────────────────────
    def _scan_premarket_movers(
        self, exclude: Set[str]
    ) -> List[Dict[str, Any]]:
        """从 Futu / MarketDataRouter 获取盘前异动股"""
        router = self._get_data_router()
        if router is None:
            return []

        # 获取扩展候选池: watchlist + 热门行业龙头
        candidates = self._get_extended_candidates()

        movers: List[Dict[str, Any]] = []
        for sym in candidates:
            clean = self._clean_symbol(sym)
            if clean in exclude or clean in ETF_SYMBOLS:
                continue
            try:
                quote = router.get_quote(sym if sym.startswith("US.") else f"US.{sym}")
                if not quote:
                    continue
                price = float(quote.get("price", 0) or quote.get("last_price", 0) or 0)
                prev_close = float(quote.get("prev_close", 0) or 0)
                if price <= 0 or prev_close <= 0:
                    continue
                change_pct = (price - prev_close) / prev_close * 100
                if abs(change_pct) >= MIN_PREMARKET_CHANGE_PCT:
                    direction = "看涨" if change_pct > 0 else "看跌"
                    movers.append({
                        "symbol": clean,
                        "change_pct": round(change_pct, 1),
                        "current_price": round(price, 2),
                        "catalyst": f"盘前{direction} {abs(change_pct):.1f}%",
                        "source": "premarket_mover",
                        "rationale": f"盘前异动 {change_pct:+.1f}%，当前价 ${price:.2f}",
                        "signal_strength": min(abs(change_pct) / 10.0, 1.0),
                    })
            except Exception:
                continue

        movers.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)
        return movers[:MAX_HOT_PICKS]

    # ─── 新闻催化扫描 ────────────────────────────────────────
    def _scan_news_catalysts(
        self,
        exclude: Set[str],
        news_summary_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """从新闻 DB 扫描有催化事件的股票"""
        news_db = self._find_news_db()
        if not news_db:
            # fallback: 从 news_summary_text 提取
            if news_summary_text:
                return self._extract_from_text(news_summary_text, exclude)
            return []

        try:
            conn = sqlite3.connect(str(news_db))
            c = conn.cursor()

            cutoff = (datetime.now() - timedelta(hours=NEWS_LOOKBACK_HOURS)).isoformat()
            c.execute(
                "SELECT title, summary, keywords, published_at "
                "FROM us_news WHERE published_at > ? ORDER BY published_at DESC LIMIT 100",
                (cutoff,),
            )
            rows = c.fetchall()
            conn.close()

            if not rows:
                return []

            # 统计每只股票的新闻提及次数 + 催化关键词
            symbol_mentions: Dict[str, Dict[str, Any]] = {}
            for title, summary, keywords_str, pub_at in rows:
                text = f"{title or ''} {summary or ''}"
                keywords = []
                if keywords_str:
                    try:
                        keywords = json.loads(keywords_str)
                    except (json.JSONDecodeError, TypeError):
                        keywords = []

                # 提取股票代码 (从 keywords 和 title)
                symbols_in_news = set()
                for kw in keywords:
                    clean = self._clean_symbol(str(kw))
                    if clean and len(clean) <= 5 and clean not in ETF_SYMBOLS:
                        symbols_in_news.add(clean)

                # 正则提取 US.XXX 格式
                us_matches = re.findall(r'\bUS\.([A-Z]{1,5})\b', text)
                for m in us_matches:
                    if m not in ETF_SYMBOLS:
                        symbols_in_news.add(m)

                # 检测催化关键词
                text_lower = text.lower()
                catalysts_found = []
                for en_key, zh_label in CATALYST_KEYWORDS.items():
                    if en_key.lower() in text_lower:
                        catalysts_found.append(zh_label)

                for sym in symbols_in_news:
                    if sym in exclude:
                        continue
                    if sym not in symbol_mentions:
                        symbol_mentions[sym] = {
                            "count": 0,
                            "catalysts": [],
                            "titles": [],
                        }
                    symbol_mentions[sym]["count"] += 1
                    symbol_mentions[sym]["catalysts"].extend(catalysts_found)
                    if title:
                        symbol_mentions[sym]["titles"].append(title[:60])

            # 筛选: 至少 2 条新闻提及 或 有明确催化
            picks: List[Dict[str, Any]] = []
            for sym, info in symbol_mentions.items():
                catalysts = list(set(info["catalysts"]))
                count = info["count"]
                if count < MIN_NEWS_MENTIONS and not catalysts:
                    continue

                catalyst_str = "、".join(catalysts[:3]) if catalysts else f"{count}条新闻关注"
                title_hint = info["titles"][0] if info["titles"] else ""

                picks.append({
                    "symbol": sym,
                    "change_pct": None,
                    "catalyst": catalyst_str,
                    "source": "news_catalyst",
                    "rationale": f"{catalyst_str} | {title_hint}" if title_hint else catalyst_str,
                    "signal_strength": min(
                        (count * 0.2 + len(catalysts) * 0.3), 1.0
                    ),
                    "news_count": count,
                })

            picks.sort(key=lambda x: x.get("signal_strength", 0), reverse=True)
            return picks[:MAX_HOT_PICKS]

        except Exception as e:
            logger.debug(f"[HotDiscovery] 新闻DB扫描失败: {e}")
            return []

    def _extract_from_text(
        self, text: str, exclude: Set[str]
    ) -> List[Dict[str, Any]]:
        """从 news_summary_text 文本中提取热点 (fallback)"""
        picks: List[Dict[str, Any]] = []
        # 提取 US.XXX 格式
        us_matches = re.findall(r'\bUS\.([A-Z]{1,5})\b', text)
        seen: Set[str] = set()
        for sym in us_matches:
            if sym in exclude or sym in ETF_SYMBOLS or sym in seen:
                continue
            seen.add(sym)
            # 检查附近文本是否有催化关键词
            idx = text.find(f"US.{sym}")
            context = text[max(0, idx - 50):idx + 100].lower()
            catalysts = []
            for en_key, zh_label in CATALYST_KEYWORDS.items():
                if en_key.lower() in context:
                    catalysts.append(zh_label)
            if catalysts:
                picks.append({
                    "symbol": sym,
                    "change_pct": None,
                    "catalyst": "、".join(catalysts[:2]),
                    "source": "news_text",
                    "rationale": "、".join(catalysts[:2]),
                    "signal_strength": 0.5,
                })
        return picks[:3]

    # ─── 合并排序 ────────────────────────────────────────────
    def _merge_and_rank(
        self, picks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """去重 + 按信号强度排序"""
        seen: Dict[str, Dict[str, Any]] = {}
        for p in picks:
            sym = p.get("symbol", "")
            if sym in seen:
                # 合并: 取更高的 signal_strength + 合并 catalyst
                existing = seen[sym]
                if p.get("signal_strength", 0) > existing.get("signal_strength", 0):
                    existing["signal_strength"] = p["signal_strength"]
                if p.get("catalyst") and p["catalyst"] not in existing.get("catalyst", ""):
                    existing["catalyst"] += f" | {p['catalyst']}"
                    existing["rationale"] += f" + {p['rationale']}"
            else:
                seen[sym] = p.copy()

        result = list(seen.values())
        result.sort(key=lambda x: x.get("signal_strength", 0), reverse=True)
        return result

    # ─── 扩展候选池 ──────────────────────────────────────────
    def _get_extended_candidates(self) -> List[str]:
        """
        获取扩展候选池: watchlist + 行业龙头 + 近期活跃股
        目标: 覆盖 ~80 只高流动性美股，确保不遗漏大盘异动
        """
        candidates: Set[str] = set()

        # 1. watchlist (从 config)
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            wl = cfg.get_dotted("watchlist.us_stocks", []) or []
            for item in wl:
                if isinstance(item, dict):
                    code = item.get("code") or item.get("symbol") or ""
                elif isinstance(item, str):
                    code = item
                else:
                    continue
                clean = self._clean_symbol(code)
                if clean:
                    candidates.add(clean)
        except Exception:
            pass

        # 2. 行业龙头补充 (覆盖主要板块)
        SECTOR_LEADERS = [
            # Tech
            "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "AVGO",
            "CRM", "ORCL", "ADBE", "INTC", "QCOM", "ARM", "MRVL", "MU",
            # Financials
            "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW",
            # Healthcare
            "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT",
            # Consumer
            "TSLA", "NKE", "SBUX", "MCD", "WMT", "COST", "HD", "LOW",
            # Energy
            "XOM", "CVX", "COP", "SLB", "OXY",
            # Industrials
            "BA", "CAT", "GE", "LMT", "RTX", "HON", "UPS",
            # Communication
            "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS",
            # Chinese ADR
            "BABA", "PDD", "JD", "NIO", "LI", "BIDU", "TME",
        ]
        candidates.update(SECTOR_LEADERS)

        return list(candidates)

    # ─── 工具方法 ────────────────────────────────────────────
    def _get_data_router(self) -> Any:
        if self._data_router is not None:
            return self._data_router
        try:
            from app.data.market_data_router import MarketDataRouter
            self._data_router = MarketDataRouter(
                twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
                finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
                alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
                futu_opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
                futu_opend_port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
                enable_futu_lv3=True,
            )
            return self._data_router
        except Exception as e:
            logger.debug(f"[HotDiscovery] MarketDataRouter 初始化失败: {e}")
            return None

    def _find_news_db(self) -> Optional[Path]:
        """查找新闻 DB 文件"""
        candidates = [
            Path("data/rsshub_us/news_us.db"),
            Path("data/news_us.db"),
        ]
        # 尝试从项目根目录
        project_root = Path(__file__).resolve().parent.parent.parent
        for rel in candidates:
            p = project_root / rel
            if p.exists():
                return p
        return None

    @staticmethod
    def _clean_symbol(code: str) -> str:
        """剥离 US./HK. 前缀，返回纯 symbol (大写)"""
        if not code:
            return ""
        c = str(code).strip().upper()
        for prefix in ("US.", "HK."):
            if c.startswith(prefix):
                c = c[len(prefix):]
        return c
