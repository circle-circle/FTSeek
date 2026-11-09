"""
Pre-Market Data Enrichment — 盘前高质量数据采集 (v2 Finnhub)

为 battle plan debate 注入 5 个维度的高质量数据:
1. Sector Rotation — 11 板块 ETF 当日涨跌 + 排名
2. Pre-Market Movers — 异动标的检测 (基于 Finnhub quote)
3. Analyst Sentiment — 分析师评级趋势 (StrongBuy/Buy/Hold/Sell)
4. Earnings Calendar — 本周财报 (Finnhub)
5. Market News Digest — 最新市场新闻 (Finnhub)

数据源: Finnhub (FREE tier, 已安装, API key 已配置)
调用频率: 每日盘前 1 次, ~30 API calls (远低于 60/min 限制)
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 板块 ETF 映射 ──
SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLY": "Consumer Disc.",
    "XLP": "Consumer Staples",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLC": "Communication",
}

# ── 市场指数 ──
MARKET_INDICES = ["SPY", "QQQ", "IWM", "DIA", "VIX"]

# ── 热门标的 (pre-market movers 检测池) ──
MOVER_POOL = [
    "SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "MSFT",
    "AMZN", "META", "GOOGL", "AMD", "NFLX", "BABA", "MU",
    "PLTR", "COIN", "MSTR", "ARM", "SMCI", "AVGO",
]

# ── 缓存 ──
_cache: Dict[str, Any] = {}
_cache_ts: float = 0
_CACHE_TTL = 3600  # 1 小时


def _get_finnhub_client():
    """获取 Finnhub 客户端 (懒加载)

    2026-10-28 (T5): 返回值套了一层**进程级配额代理** —— 同一个 API key 在本进程里
    原本有 3 套互不知情的额度视图(本 SDK 客户端 / FinnhubProvider 每实例桶 / fundamental 裸 requests),
    实际速率 = N × 60/min。代理让每次 SDK 调用先向共享桶要令牌, 429 时触发全局退避。
    """
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        _env_path = Path(__file__).resolve().parent.parent.parent / ".env.us"
        if _env_path.exists():
            load_dotenv(_env_path)
    except Exception:
        pass

    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        return None

    import finnhub
    return _QuotaLimitedClient(finnhub.Client(api_key=api_key))


class _QuotaLimitedClient:
    """把 finnhub SDK 客户端纳入进程级配额桶（薄代理，只拦调用不改编排）。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            from app.data import finnhub_limiter as fh_quota
            if not fh_quota.acquire(n=1, timeout=30.0):
                # 抛异常而不是返回 None: 调方均有 try/except, None 会在 .get() 上炸成无关的 AttributeError
                raise RuntimeError(f"Finnhub 配额排队超时 ({name})")
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                if "429" in str(e):
                    fh_quota.on_429(name)
                raise

        return _wrapped


def collect_premarket_data(watchlist_symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    收集盘前全维度数据。

    Args:
        watchlist_symbols: 观察列表 (可带 US. 前缀或裸名)

    Returns:
        结构化 dict
    """
    global _cache, _cache_ts
    now = time.time()
    if now - _cache_ts < _CACHE_TTL and _cache:
        return _cache

    from app.utils.timezone import now_utc
    result = {"collected_at": now_utc().isoformat()}

    # ── 0. Futu Market Snapshot (主数据源 — 板块轮动+异动+基本面) ──
    futu_data = _collect_futu_market_data(watchlist_symbols)
    if futu_data:
        result.update(futu_data)

    # ── 1. Sector Rotation ──
    try:
        client = _get_finnhub_client()
        if client:
            result.setdefault("sector_rotation", _collect_sector_rotation(client))
    except Exception as e:
        logger.warning(f"[PreMarket] sector rotation failed: {e}")

    # ── 2. Pre-Market Movers ──
    try:
        client = _get_finnhub_client()
        if client:
            result.setdefault("premarket_movers", _collect_premarket_movers(client, watchlist_symbols))
    except Exception as e:
        logger.warning(f"[PreMarket] premarket movers failed: {e}")

    # ── 2b. Futu 全市场盘前涨跌榜 (2026-08-29: 覆盖 20只池→全市场) ──
    try:
        rank = _collect_futu_premarket_rank()
        if rank:
            result["futu_premarket_rank"] = rank
    except Exception as e:
        logger.warning(f"[PreMarket] Futu premarket rank failed: {e}")

    # ── 2c. 借券拥挤度 (2026-08-29 RFC-062: record-only 决策参考) ──
    try:
        from app.data.borrow_crowding import scan as _borrow_scan
        _bc_syms = None
        if watchlist_symbols:
            _bc_syms = sorted({
                (s if str(s).startswith("US.") else f"US.{s}").upper()
                for s in watchlist_symbols})
        _bs = _borrow_scan(symbols=_bc_syms)
        if _bs.get("flagged"):
            result["borrow_crowding"] = _bs
    except Exception as e:
        logger.warning(f"[PreMarket] borrow crowding failed: {e}")

    # ── 3. Analyst Sentiment (Finnhub + Futu) ──
    if watchlist_symbols:
        try:
            client = _get_finnhub_client()
            if client:
                result["analyst_sentiment"] = _collect_analyst_sentiment(client, watchlist_symbols)
        except Exception as e:
            logger.warning(f"[PreMarket] analyst sentiment failed: {e}")

        # 补充: Futu 分析师共识 (目标价 + 评级)
        try:
            futu_ac = _collect_futu_analyst_consensus(watchlist_symbols)
            if futu_ac:
                result["futu_analyst"] = futu_ac
        except Exception as e:
            logger.warning(f"[PreMarket] Futu analyst consensus failed: {e}")

    # ── 4. Earnings Calendar ──
    try:
        client = _get_finnhub_client()
        if client:
            result.setdefault("earnings_calendar", _collect_earnings_calendar(client))
    except Exception as e:
        logger.warning(f"[PreMarket] earnings calendar failed: {e}")
        result.setdefault("earnings_calendar", "Unable to fetch")

    # ── 5. Market News ──
    try:
        client = _get_finnhub_client()
        if client:
            result.setdefault("market_news", _collect_market_news(client))
    except Exception as e:
        logger.warning(f"[PreMarket] market news failed: {e}")
        result.setdefault("market_news", [])

    # ── 6. Economic Calendar (hardcoded) ──
    result["economic_calendar"] = _get_economic_calendar()

    # ── 7. FRED Treasury Yields (FREE, no key) ──
    try:
        result["treasury_yields"] = _collect_treasury_yields()
    except Exception as e:
        logger.warning(f"[PreMarket] treasury yields failed: {e}")

    # ── 8. Insider Trading (OpenInsider, FREE) ──
    try:
        result["insider_trading"] = _collect_insider_trading()
    except Exception as e:
        logger.warning(f"[PreMarket] insider trading failed: {e}")

    _cache = result
    _cache_ts = now

    # Log summary
    sectors = result.get("sector_rotation", {})
    n_sectors = len(sectors.get("ranked", []))
    movers = result.get("premarket_movers", {})
    n_news = len(result.get("market_news", []))
    n_analysts = len(result.get("futu_analyst", {}))
    logger.info(
        f"[PreMarket] 数据采集完成: {n_sectors} sectors, "
        f"gainers={len(movers.get('gainers', []))}, "
        f"losers={len(movers.get('losers', []))}, "
        f"news={n_news}, futu_analysts={n_analysts}"
    )

    return result


def _collect_futu_market_data(watchlist_symbols: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """从 Futu 获取盘前市场数据 (板块轮动+异动+基本面快照)
    主数据源 — 不依赖外部API key
    """
    try:
        # 2026-07-25: 改单例 + 不 disconnect (handoff 07-24 解锁spam同款反模式收敛)
        from app.web_dashboard.manual_order_service import get_manual_order_service
        from futu import RET_OK
        svc = get_manual_order_service()
        if not svc.connect():
            logger.warning("[PreMarket] Futu connect failed")
            return None

        result = {}

        # 1. Sector ETF snapshots (板块轮动)
        sector_etfs = {
            "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
            "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Consumer Discretionary",
            "XLP": "Consumer Staples", "XLU": "Utilities", "XLB": "Materials",
            "XLRE": "Real Estate", "XLC": "Communication",
        }
        etf_symbols = [f"US.{etf}" for etf in sector_etfs.keys()]
        ret, data = svc._quote_ctx.get_market_snapshot(etf_symbols)
        if ret == RET_OK and not data.empty:
            ranked = []
            for _, row in data.iterrows():
                etf_code = row.get("code", "").replace("US.", "")
                sector = sector_etfs.get(etf_code, etf_code)
                prev_close = row.get("prev_close_price", 0)
                last = row.get("last_price", 0)
                chg_pct = round((last - prev_close) / prev_close * 100, 2) if prev_close else 0
                ranked.append({"etf": etf_code, "sector": sector, "change_pct": chg_pct, "price": last})
            ranked.sort(key=lambda x: x["change_pct"], reverse=True)
            result["sector_rotation"] = {"ranked": ranked, "summary": f"Top: {ranked[0]['sector']} +{ranked[0]['change_pct']}%" if ranked else "N/A"}

        # 2. Watchlist pre-market movers
        if watchlist_symbols:
            movers = []
            for sym in watchlist_symbols[:20]:
                if not sym.startswith("US."):
                    sym = f"US.{sym}"
                ret, data = svc._quote_ctx.get_market_snapshot([sym])
                if ret == RET_OK and not data.empty:
                    row = data.iloc[0].to_dict()
                    prev_close = row.get("prev_close_price", 0)
                    last = row.get("last_price", 0)
                    chg_pct = round((last - prev_close) / prev_close * 100, 2) if prev_close else 0
                    pre_change = row.get("pre_change_rate", 0)
                    movers.append({
                        "symbol": sym, "price": last, "change_pct": chg_pct,
                        "premarket_change": pre_change, "volume_ratio": row.get("volume_ratio", 1.0),
                    })
            movers.sort(key=lambda x: abs(x.get("premarket_change", 0)), reverse=True)
            result["premarket_movers"] = {"gainers": [m for m in movers if m["premarket_change"] > 0],
                                           "losers": [m for m in movers if m["premarket_change"] < 0]}

        # 3. SPY/QQQ/DIA market context
        index_symbols = ["US.SPY", "US.QQQ", "US.DIA"]
        ret, idx_data = svc._quote_ctx.get_market_snapshot(index_symbols)
        if ret == RET_OK and not idx_data.empty:
            indices = {}
            for _, row in idx_data.iterrows():
                sym = row.get("code", "").replace("US.", "")
                prev_close = row.get("prev_close_price", 0)
                last = row.get("last_price", 0)
                chg_pct = round((last - prev_close) / prev_close * 100, 2) if prev_close else 0
                indices[sym] = {"price": last, "change_pct": chg_pct}
            result["market_indices"] = indices

        return result

    except Exception as e:
        logger.warning(f"[PreMarket] Futu market data error: {e}")
        return None


def _collect_sector_rotation(client) -> Dict[str, Any]:
    """板块轮动: 11 ETF 当日涨跌 + 排名"""
    ranked = []
    for etf, sector in SECTOR_ETFS.items():
        try:
            q = client.quote(etf)
            price = q.get("c", 0) or 0
            change = q.get("d", 0) or 0
            change_pct = q.get("dp", 0) or 0
            prev_close = q.get("pc", 0) or 0
            high = q.get("h", 0) or 0
            low = q.get("l", 0) or 0

            if price > 0:
                ranked.append({
                    "etf": etf,
                    "sector": sector,
                    "price": round(price, 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "prev_close": round(prev_close, 2),
                    "intraday_range": round((high - low) / prev_close * 100, 2) if prev_close > 0 else 0,
                })
        except Exception:
            continue

    # Sort by change_pct (descending)
    ranked.sort(key=lambda x: x["change_pct"], reverse=True)

    leaders = [r["sector"] for r in ranked[:3]] if len(ranked) >= 3 else []
    laggards = [r["sector"] for r in ranked[-3:]] if len(ranked) >= 3 else []

    return {
        "ranked": ranked,
        "leaders": leaders,
        "laggards": laggards,
        "summary": f"Leading: {', '.join(leaders)} | Lagging: {', '.join(laggards)}" if leaders else "",
    }


def _collect_futu_premarket_rank(top_n: int = 15) -> Optional[Dict[str, Any]]:
    """富途全市场盘前涨跌榜 (2026-08-29 能力利用率审计接线).

    此前盘前异动仅扫硬编码 20 只 MOVER_POOL (Finnhub quote 轮询),
    本维度一次调用覆盖全市场 (~18000 只) 盘前涨跌幅排名。
    record-only 增量维度, 不替换既有 MOVER_POOL 检测 (保留指数/观察池口径)。
    回滚: futu_channels.premarket_rank_enabled=false + 重启。

    2026-08-29 (用户裁"按建议执行" 盘前榜过滤规则):
      ① 修复 losers 恒空 bug — 接口默认降序只返涨幅榜 (探针实锤: count=50
         返回 50 行全是 +6.8%~+77.9%), 领跌榜须显式 RankSortDir.ASCENDING。
      ② 质量门候选持久化 — 全榜单仅保留涨幅>±2%者进质量漏斗:
         盘前成交额≥$5M → 价格≥$5 → 市值≥$2B (ETF total_market_val=NaN 顺带剔除)
         → |涨幅|>25% 影子打标不入围 → 每向≤5 候选, 写
         data/premarket_rank_candidates_{ET}.json 供 UE 线索源消费。
         探针依据: 默认榜 33/50 盘前成交额<$1M (BESS $515), 混 5 只杠杆 ETF。
    """
    try:
        from app.core.config_container import get_config
        if not bool(get_config().get_dotted(
                "futu_channels.premarket_rank_enabled", True)):
            return None
    except Exception:
        pass
    from futu import OpenQuoteContext, RET_OK
    ctx = None
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        gainers_all, universe = _fetch_rank_rows(ctx, ascending=False)
        losers_all, _ = _fetch_rank_rows(ctx, ascending=True)
        if not gainers_all and not losers_all:
            logger.warning("[PreMarket] Futu 盘前榜双向均为空")
            return None
        gainers_all.sort(key=lambda x: x["change_pct"], reverse=True)
        losers_all.sort(key=lambda x: x["change_pct"])
        # 质量门候选持久化 (失败不影响展示维度)
        try:
            _persist_premarket_rank_candidates(ctx, gainers_all, losers_all)
        except Exception as e:
            logger.warning(f"[PreMarket] 盘前榜质量门候选持久化失败 (降级): {e}")
        return {
            "source": "futu_us_pre_market_rank",
            "universe": universe,
            "gainers": gainers_all[:top_n],
            "losers": losers_all[:top_n],
        }
    except Exception as e:
        logger.warning(f"[PreMarket] Futu 盘前榜异常: {e}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


# ── 盘前榜质量门参数 (2026-08-29, 探针实锤依据见 _collect_futu_premarket_rank) ──
_PM_MIN_TURNOVER_USD = 5_000_000.0   # 层1 流动性: 盘前成交额下限
_PM_MIN_PRICE = 5.0                  # 层2 仙股门 (对齐 factor_engine._QUALITY_GATE_MIN_PRICE)
_PM_MIN_MCAP_USD = 2_000_000_000.0   # 层3 市值门 (对齐 factor_engine._QUALITY_GATE_MIN_MCAP)
_PM_EXTREME_PCT = 25.0               # 层4 极端涨幅带: 影子打标不入围 (预注册攒样, 非硬禁)
_PM_MAX_CANDIDATES = 5               # 层6 每方向每日候选上限 (防稀释下游注意力)
_PM_ETF_NAME_KEYWORDS = ("ETF", "SPDR", "ISHARES", "ULTRA", "PROSHARES", "DIREXION",
                         "T-REX", "TRADR", "INDEX", "FUND", "TRUST")
_RANK_CANDIDATES_DIR = Path(__file__).resolve().parent.parent.parent / "data"


class _RankMcapFilter:
    """盘前榜服务端市值预过滤 (2026-08-29 探针实锤).

    根因修复: 默认涨跌幅榜 top50 天然全是小票 (实弹: 质量门后候选 0,
    35/50 盘前成交额<$1M)。服务端按市值≥$2B 预过滤后榜单即标的池
    (实弹: 领跌榜直接出 PYPL -13%/$328M, IREN -7.7%/$187M)。
    ⚠️ 服务端市值过滤不排除 ETF (实弹混入 IYH/PFFD), 客户端 name/mcap 门保留。
    """
    min_val = 2_000_000_000.0  # 与 _PM_MIN_MCAP_USD 同源, 避免循环引用提前绑定

    def fill_request_pb(self, filter_pb):
        from futu.common.pb import Qot_GetUSPreMarketRank_pb2 as _pb
        filter_pb.indicatorType = _pb.IndicatorType_MarketCap
        filter_pb.indicatorValue.filterMin.value = self.min_val
        filter_pb.indicatorValue.filterMin.includes = True
        filter_pb.indicatorValue.filterMax.value = 1e15
        filter_pb.indicatorValue.filterMax.includes = True
        from futu import RET_OK
        return RET_OK, ""


def _fetch_rank_rows(ctx, ascending: bool) -> Tuple[List[Dict[str, Any]], int]:
    """拉取盘前榜一侧 (降序=领涨, 升序=领跌), 只保留 |涨跌|>2% 且价格>0 的行。

    服务端市值预过滤 ≥$2B (治"涨跌幅榜全是小票"根因), 客户端再精过滤。
    Returns: (rows, universe_total)
    """
    from futu import RET_OK
    try:
        from futu.common.constant import RankSortDir
        sort_dir = RankSortDir.ASCENDING if ascending else RankSortDir.DESCENDING
        ret, out = ctx.get_us_pre_market_rank(
            sort_dir=sort_dir, count=50, filter_list=[_RankMcapFilter()])
    except ImportError:
        if ascending:
            return [], 0  # SDK 无 RankSortDir: 领跌榜降级为空
        ret, out = ctx.get_us_pre_market_rank(count=50)
    if ret != RET_OK or not isinstance(out, tuple) or len(out) < 2:
        logger.warning(f"[PreMarket] Futu 盘前榜拉取失败 (ascending={ascending}): ret={ret}")
        return [], 0
    _total, df = out
    if df is None or not len(df):
        return [], int(_total or 0)
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        try:
            sym = str(r.get("security", "")).replace("US.", "")
            chg = float(r.get("pre_market_change_ratio", 0) or 0)
            px = float(r.get("pre_market_price", 0) or 0)
            to = float(r.get("pre_market_turnover", 0) or 0)
            if not sym or px <= 0:
                continue
            if ascending and chg >= -2.0:
                continue
            if not ascending and chg <= 2.0:
                continue
            rows.append({"symbol": sym, "price": round(px, 2),
                         "change_pct": round(chg, 2),
                         "turnover_usd": round(to)})
        except Exception:
            continue
    return rows, int(_total or 0)


def _fetch_mcap_map(ctx, codes: List[str]) -> Dict[str, float]:
    """code -> total_market_val (USD 单位, 探针实锤)。
    ETF/无效标的市值为 NaN 或 name 命中关键词 → 哨兵值 -1 (剔除)。

    ⚠️ 批次隔离 (2026-08-29 实弹根因): 快照接口遇单只无效标的 (如 OTC 票 LGFRY
    "暂不提供美股 OTC 市场行情") 会整批失败 ret=-1, 曾致 28 只候选全部被当无市值
    剔除 (候选=0)。失败批次二分递归隔离坏票, 单只失败 → -1 剔除 (fail-close)。
    """
    from futu import RET_OK
    mcap: Dict[str, float] = {}

    def _snapshot_batch(batch: List[str], depth: int = 0) -> None:
        if not batch:
            return
        try:
            ret, df = ctx.get_market_snapshot(batch)
        except Exception as e:
            ret, df = -1, str(e)
        if ret == RET_OK and df is not None:
            for _, r in df.iterrows():
                code = str(r.get("code", ""))
                name = str(r.get("name", "")).upper()
                try:
                    mv = float(r.get("total_market_val"))
                except (TypeError, ValueError):
                    mv = float("nan")
                if math.isnan(mv) or any(k in name for k in _PM_ETF_NAME_KEYWORDS):
                    mcap[code] = -1.0
                else:
                    mcap[code] = mv
            return
        # 失败: 单只 → 限流可能误杀, 延迟重试 1 次仍失败才剔除; 多只 → 二分隔离 (上限 8 层防退化)
        if len(batch) == 1 or depth > 8:
            if depth <= 12:  # 给递归最深处的单只一次重试机会
                time.sleep(0.5)
                try:
                    ret2, df2 = ctx.get_market_snapshot(batch)
                    if ret2 == RET_OK and df2 is not None and len(df2):
                        r = df2.iloc[0]
                        code = str(r.get("code", ""))
                        name = str(r.get("name", "")).upper()
                        try:
                            mv = float(r.get("total_market_val"))
                        except (TypeError, ValueError):
                            mv = float("nan")
                        mcap[code] = -1.0 if (math.isnan(mv) or
                                              any(k in name for k in _PM_ETF_NAME_KEYWORDS)) else mv
                        return
                except Exception:
                    pass
            logger.warning(f"[PreMarket] 市值快照单只失败 (ret={ret}): "
                           f"{batch[0] if batch else ''} — 剔除")
            for c in batch:
                mcap[c] = -1.0
            return
        mid = len(batch) // 2
        _snapshot_batch(batch[:mid], depth + 1)
        _snapshot_batch(batch[mid:], depth + 1)

    for i in range(0, len(codes), 200):
        if i > 0:
            time.sleep(0.3)  # 批次间限流保护
        _snapshot_batch(codes[i:i + 200])
    return mcap


def apply_rank_quality_gates(gainers: List[Dict], losers: List[Dict],
                             mcap_map: Dict[str, float],
                             earnings_set: set) -> Dict[str, Any]:
    """纯函数质量门 (可测): 成交额→价格→市值/ETF→极端带→事件标记→上限。

    Args:
        gainers/losers: _fetch_rank_rows 输出 (已排序)
        mcap_map: _fetch_mcap_map 输出 (缺失=按剔除处理)
        earnings_set: 未来3日财报标的集合 (裸 ticker 大写)
    Returns:
        {gainers: {candidates, extreme, rejected}, losers: {...}, stats: {...}}
    """
    def _side(rows: List[Dict]) -> Dict[str, Any]:
        candidates, extreme, rejected = [], [], []
        rank = 0
        for e in rows:
            if e["turnover_usd"] < _PM_MIN_TURNOVER_USD:
                rejected.append({"symbol": e["symbol"], "reason": "turnover"})
                continue
            if e["price"] < _PM_MIN_PRICE:
                rejected.append({"symbol": e["symbol"], "reason": "price"})
                continue
            rank += 1
            mv = mcap_map.get(f"US.{e['symbol']}", -1.0)
            if mv < _PM_MIN_MCAP_USD:
                rejected.append({"symbol": e["symbol"],
                                 "reason": "etf_or_smallcap" if mv < 0 else "mcap"})
                continue
            entry = dict(e)
            entry.update({"rank": rank, "market_cap_usd": int(mv), "tags": []})
            if e["symbol"].upper() in earnings_set:
                entry["tags"].append("EVENT_RISK")
            if abs(e["change_pct"]) > _PM_EXTREME_PCT:
                entry["tags"].append("EXTREME")
                extreme.append(entry)
            else:
                candidates.append(entry)
        return {"candidates": candidates[:_PM_MAX_CANDIDATES],
                "extreme": extreme, "rejected": rejected}

    return {"gainers": _side(gainers), "losers": _side(losers),
            "stats": {"raw_gainers": len(gainers), "raw_losers": len(losers)}}


def _earnings_risk_set(symbols: List[str]) -> set:
    """未来3日财报标的集合 (裸 ticker 大写)。失败降级空集 (不标记)。"""
    if not symbols:
        return set()
    try:
        from app.data.earnings_calendar import EarningsCalendar
        futu_syms = [f"US.{s}" for s in symbols]
        ups = EarningsCalendar().get_watchlist_earnings(
            watchlist_symbols=futu_syms, days_ahead=3)
        return {str(u.get("ticker", "")).upper() for u in ups if u.get("ticker")}
    except Exception as e:
        logger.warning(f"[PreMarket] 财报交叉标记失败 (降级不标记): {e}")
        return set()


def _persist_premarket_rank_candidates(ctx, gainers_all: List[Dict],
                                       losers_all: List[Dict]) -> Optional[Path]:
    """质量门过滤 + 原子写 data/premarket_rank_candidates_{ET}.json (重跑幂等覆盖)。

    两遍式 (2026-08-29 实弹优化): 先用零 API 的 成交额+价格门 收缩快照目标
    (100只→约25只), 再只对幸存者查市值快照 — 既避开大量低成交额小票/OTC 垃圾,
    也把快照调用量压到富途限流安全区 (此前全量快照触发限流级联误杀真标的)。
    """
    def _cheap_pass(rows: List[Dict]) -> List[Dict]:
        return [e for e in rows
                if e["turnover_usd"] >= _PM_MIN_TURNOVER_USD and e["price"] >= _PM_MIN_PRICE]

    surv_symbols = [e["symbol"] for e in _cheap_pass(gainers_all) + _cheap_pass(losers_all)]
    mcap_map = _fetch_mcap_map(ctx, [f"US.{s}" for s in surv_symbols]) if surv_symbols else {}
    earnings_set = _earnings_risk_set(surv_symbols)
    gated = apply_rank_quality_gates(gainers_all, losers_all, mcap_map, earnings_set)
    try:
        from app.utils.timezone import now_et
        date_str = now_et().strftime("%Y-%m-%d")
    except Exception:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    payload = {
        "date": date_str,
        "collected_at": datetime.utcnow().isoformat() + "Z",
        "gates": {"min_turnover_usd": _PM_MIN_TURNOVER_USD, "min_price": _PM_MIN_PRICE,
                  "min_mcap_usd": _PM_MIN_MCAP_USD, "extreme_pct": _PM_EXTREME_PCT,
                  "max_per_side": _PM_MAX_CANDIDATES},
        "gainers": gated["gainers"],
        "losers": gated["losers"],
        "stats": gated["stats"],
    }
    _RANK_CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RANK_CANDIDATES_DIR / f"premarket_rank_candidates_{date_str}.json"
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    n_g = len(gated["gainers"]["candidates"])
    n_l = len(gated["losers"]["candidates"])
    logger.info(f"[PreMarket] 盘前榜质量门: 候选 LONG {n_g} / SHORT {n_l} "
                f"(原始 {gated['stats']['raw_gainers']}/{gated['stats']['raw_losers']}) → {out_path.name}")
    return out_path


def ensure_premarket_rank_candidates(date_str: Optional[str] = None) -> Optional[Path]:
    """幂等保证当日盘前榜质量门候选文件存在 (供跨进程消费者调用)。

    文件已存在 → 直接返回; 不存在 → 触发一次采集 (内部持久化)。失败返回 None。
    """
    try:
        from app.utils.timezone import now_et
        d = date_str or now_et().strftime("%Y-%m-%d")
    except Exception:
        d = date_str or datetime.utcnow().strftime("%Y-%m-%d")
    path = _RANK_CANDIDATES_DIR / f"premarket_rank_candidates_{d}.json"
    if path.exists():
        return path
    try:
        rank = _collect_futu_premarket_rank()
        if rank and path.exists():
            return path
    except Exception as e:
        logger.warning(f"[PreMarket] ensure_premarket_rank_candidates 失败: {e}")
    return None


def get_premarket_rank_entries() -> Dict[str, List[Dict[str, Any]]]:
    """当日盘前榜 entries (发现链消费口径): 进程内缓存优先, 冷缓存回退当日持久化文件。

    2026-08-29 复查加固: factor_engine Source 1b 此前直读模块 _cache — 主服务盘中
    重启后缓存清空, 该源当日静默零贡献 (仅 debug 日志, "日志正常但逻辑失效"族)。
    本函数封装"缓存→文件"两级口径, 消费者不再触碰私有 _cache。
    口径差异: 缓存=原始榜 top15/向; 文件=质量门后候选 ≤5/向 (对发现池均可用)。
    永不读非当日文件 (旧文件=陈旧语义, 宁缺毋滥)。统一返回 {"gainers": [...], "losers": [...]}。
    """
    try:
        cached = _cache.get("futu_premarket_rank") if isinstance(_cache, dict) else None
        if isinstance(cached, dict) and (cached.get("gainers") or cached.get("losers")):
            return {"gainers": cached.get("gainers") or [],
                    "losers": cached.get("losers") or []}
    except Exception:
        pass
    try:
        from app.utils.timezone import now_et
        d = now_et().strftime("%Y-%m-%d")
    except Exception:
        d = datetime.utcnow().strftime("%Y-%m-%d")
    path = _RANK_CANDIDATES_DIR / f"premarket_rank_candidates_{d}.json"
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {"gainers": (payload.get("gainers") or {}).get("candidates") or [],
                    "losers": (payload.get("losers") or {}).get("candidates") or []}
    except Exception as e:
        logger.debug(f"[PreMarket] 盘前榜回退读文件失败: {e}")
    return {"gainers": [], "losers": []}


def _collect_premarket_movers(client, watchlist_symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """Pre-market movers: 从 MOVER_POOL + watchlist 中检测异动"""
    # Merge pools
    all_symbols = set(MOVER_POOL)
    if watchlist_symbols:
        for s in watchlist_symbols:
            clean = s.replace("US.", "").replace("HK.", "")
            all_symbols.add(clean)

    gainers = []
    losers = []
    market_indices = {}

    for sym in all_symbols:
        try:
            q = client.quote(sym)
            price = q.get("c", 0) or 0
            change_pct = q.get("dp", 0) or 0

            if price <= 0:
                continue

            # Track market indices separately
            if sym in ("SPY", "QQQ", "IWM", "DIA", "VIX"):
                market_indices[sym] = {
                    "price": round(price, 2),
                    "change_pct": round(change_pct, 2),
                }

            entry = {
                "symbol": sym,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
            }

            if change_pct > 2.0:
                gainers.append(entry)
            elif change_pct < -2.0:
                losers.append(entry)

        except Exception:
            continue

    gainers.sort(key=lambda x: x["change_pct"], reverse=True)
    losers.sort(key=lambda x: x["change_pct"])

    return {
        "gainers": gainers[:8],
        "losers": losers[:8],
        "market_indices": market_indices,
        "summary": _summarize_movers(gainers, losers, market_indices),
    }


def _summarize_movers(gainers, losers, indices) -> str:
    parts = []
    # Market summary
    spy = indices.get("SPY", {})
    qqq = indices.get("QQQ", {})
    vix = indices.get("VIX", {})
    if spy:
        parts.append(f"SPY {spy['change_pct']:+.2f}%")
    if qqq:
        parts.append(f"QQQ {qqq['change_pct']:+.2f}%")
    if vix:
        parts.append(f"VIX={vix['price']:.1f}")

    if gainers:
        top = gainers[0]
        parts.append(f"Top ▲ {top['symbol']} {top['change_pct']:+.1f}%")
    if losers:
        bot = losers[0]
        parts.append(f"Top ▼ {bot['symbol']} {bot['change_pct']:+.1f}%")

    return " | ".join(parts) if parts else "No significant movers"


def _collect_analyst_sentiment(client, watchlist_symbols: List[str]) -> Dict[str, Any]:
    """分析师评级趋势 (最近 3 个月)"""
    results = {}
    for sym in watchlist_symbols[:10]:  # Limit to 10 to stay under rate limit
        clean = sym.replace("US.", "")
        try:
            recs = client.recommendation_trends(clean)
            if recs:
                # Latest month
                latest = recs[0]
                strong_buy = latest.get("strongBuy", 0)
                buy = latest.get("buy", 0)
                hold = latest.get("hold", 0)
                sell = latest.get("sell", 0)
                strong_sell = latest.get("strongSell", 0)
                total = strong_buy + buy + hold + sell + strong_sell
                if total > 0:
                    bull_pct = round((strong_buy + buy) / total * 100, 1)
                    # Trend: compare with previous month
                    trend = "stable"
                    if len(recs) >= 2:
                        prev = recs[1]
                        prev_bull = (prev.get("strongBuy", 0) + prev.get("buy", 0))
                        prev_total = prev_bull + prev.get("hold", 0) + prev.get("sell", 0) + prev.get("strongSell", 0)
                        if prev_total > 0:
                            prev_bull_pct = prev_bull / prev_total * 100
                            diff = bull_pct - prev_bull_pct
                            if diff > 3:
                                trend = "improving ↑"
                            elif diff < -3:
                                trend = "deteriorating ↓"

                    results[clean] = {
                        "bull_pct": bull_pct,
                        "total_analysts": total,
                        "strong_buy": strong_buy,
                        "buy": buy,
                        "hold": hold,
                        "sell": sell + strong_sell,
                        "trend": trend,
                    }
        except Exception:
            continue

    return results


def _collect_earnings_calendar(client) -> str:
    """本周财报日历"""
    from app.utils.timezone import now_et
    today = now_et().date()
    week_end = today + timedelta(days=7)

    try:
        ec = client.earnings_calendar(
            _from=today.strftime("%Y-%m-%d"),
            to=week_end.strftime("%Y-%m-%d"),
            symbol="",
        )
        events = ec.get("earningsCalendar", [])
        # Filter for notable companies (revenue > 0 or EPS estimate present)
        notable = [
            e for e in events
            if e.get("epsEstimate") is not None or (e.get("revenueEstimate") and e["revenueEstimate"] > 100_000_000)
        ]
        notable.sort(key=lambda x: x.get("revenueEstimate", 0) or 0, reverse=True)

        if notable:
            lines = [f"📊 This week earnings ({len(notable)} notable):"]
            for e in notable[:10]:
                sym = e.get("symbol", "?")
                date = e.get("date", "?")
                eps = e.get("epsEstimate", "N/A")
                rev = e.get("revenueEstimate", 0)
                rev_str = f"${rev/1e9:.1f}B" if rev and rev > 1e9 else (f"${rev/1e6:.0f}M" if rev else "N/A")
                lines.append(f"  {sym:6} | {date} | EPS est: {eps} | Rev est: {rev_str}")
            return "\n".join(lines)
        else:
            return f"No notable earnings this week ({len(events)} minor events)"
    except Exception as e:
        return f"Earnings calendar unavailable: {e}"


def _collect_market_news(client) -> List[Dict]:
    """最新市场新闻 (top 10)"""
    try:
        news = client.general_news("general", min_id=0)
        results = []
        for n in news[:10]:
            results.append({
                "headline": n.get("headline", "")[:120],
                "source": n.get("source", ""),
                "datetime": n.get("datetime", 0),
                "url": n.get("url", ""),
            })
        return results
    except Exception:
        return []


def _collect_futu_analyst_consensus(watchlist_symbols: List[str]) -> Dict[str, Any]:
    """
    Futu 分析师评级共识 — 补充 Finnhub recommendation_trends

    优势：提供目标价 (highest/avg/lowest)，Finnhub 免费计划不提供
    数据来源：Futu OpenD get_research_analyst_consensus
    """
    try:
        from app.data.providers.futu_extended_provider import FutuExtendedProvider
        provider = FutuExtendedProvider()
        results = {}
        for sym in watchlist_symbols[:10]:
            code = sym if sym.startswith("US.") else f"US.{sym}"
            ac = provider.get_analyst_consensus(code)
            if ac.get("status") == "ok":
                d = ac.get("data", {})
                ticker = sym.replace("US.", "")
                results[ticker] = {
                    "rating": d.get("rating"),
                    "rating_label": d.get("rating_label"),
                    "total_analysts": d.get("total_analysts"),
                    "target_avg": d.get("target_avg"),
                    "target_high": d.get("target_high"),
                    "target_low": d.get("target_low"),
                    "buy_pct": d.get("buy_pct"),
                    "hold_pct": d.get("hold_pct"),
                    "sell_pct": d.get("sell_pct"),
                    "update_date": d.get("update_date"),
                }
        provider.close()
        return results
    except Exception as e:
        logger.warning(f"[PreMarket] Futu analyst consensus error: {e}")
        return {}


def _get_economic_calendar() -> str:
    """未来 7 天美国高/中重要性事件，使用动态日历而非日期猜测。"""
    from app.utils.timezone import now_et
    source = "Futu/get_economic_calendar"
    try:
        # 2026-11-09: 生产 prompt 不再依赖影子脚本的下划线私有函数 (见 app/data/economic_calendar.py)
        from app.data.economic_calendar import fetch_us_event_schedule
        schedule = fetch_us_event_schedule(now_et())
    except Exception as exc:
        return f"Economic calendar unavailable ({type(exc).__name__}); source={source}; do not infer no events"
    if schedule.get("status") != "ok":
        return (f"Economic calendar unavailable (status={schedule.get('status', 'unknown')}); "
                f"source={source}; do not infer no events")
    events = schedule.get("events") or []
    if not events:
        return f"Dynamic calendar returned 0 US HIGH/MEDIUM events in next 7 days; source={source}"
    return "\n".join(
        f"⚡ title={event.get('title') or 'N/A'} | ts_et={event.get('ts_et') or 'N/A'} ET | "
        f"star={event.get('star') or 'N/A'} | source={source}"
        for event in events
    )


def _collect_treasury_yields() -> Dict[str, Any]:
    """FRED Treasury yields + VIX + Fed Funds (FREE, no API key)"""
    import requests as _req

    series_map = {
        "DGS10": "10Y",
        "DGS2": "2Y",
        "DGS5": "5Y",
        "VIXCLS": "VIX",
        "DFF": "FedFunds",
    }

    results = {}
    observation_dates = {}
    from app.utils.timezone import now_et as _now_et_fred
    today = _now_et_fred().date()
    start = (today - timedelta(days=14)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    for sid, label in series_map.items():
        try:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?chart_type=line&id={sid}&cosd={start}&coed={end}"
            r = _req.get(url, timeout=10)
            r.raise_for_status()
            from app.data.fred_series import latest_valid_observation
            as_of, value = latest_valid_observation(r.text)
            if as_of is not None and value is not None:
                results[label] = round(value, 2)
                observation_dates[label] = as_of
        except Exception:
            continue
    results["as_of"] = observation_dates
    results["source"] = "FRED/fredgraph.csv"

    # Compute spread
    y10 = results.get("10Y")
    y2 = results.get("2Y")
    spread = None
    curve_shape = "unknown"
    if (y10 is not None and y2 is not None
            and observation_dates.get("10Y") == observation_dates.get("2Y")):
        spread = round(y10 - y2, 2)
        if spread > 0.5:
            curve_shape = "steep (growth expected)"
        elif spread > 0:
            curve_shape = "normal (balanced)"
        elif spread > -0.3:
            curve_shape = "flat (uncertainty)"
        else:
            curve_shape = "INVERTED (recession signal)"
    elif y10 is not None and y2 is not None:
        curve_shape = (f"unavailable (observation dates differ: 10Y={observation_dates.get('10Y')}, "
                       f"2Y={observation_dates.get('2Y')})")

    results["spread_10y_2y"] = spread
    results["curve_shape"] = curve_shape

    # Interpretation
    parts = []
    if y10 is not None:
        if y10 > 5.0:
            parts.append(f"10Y={y10}% — high, equity headwind")
        elif y10 > 4.0:
            parts.append(f"10Y={y10}% — elevated, growth pressure")
        elif y10 > 3.0:
            parts.append(f"10Y={y10}% — moderate, balanced")
        else:
            parts.append(f"10Y={y10}% — low, equity-friendly")
    if spread is not None:
        parts.append(f"10Y-2Y={spread}% ({curve_shape})")
    results["interpretation"] = "; ".join(parts)

    return results


def _collect_insider_trading() -> Dict[str, Any]:
    """OpenInsider SEC Form 4 recent buys (FREE, no key)"""
    import requests as _req
    import re as _re

    try:
        url = "http://openinsider.com/screener?s=&o=&pl=&ph=&st=&fd=7"
        r = _req.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        html = r.text

        rows = _re.findall(r'<tr[^>]*>(.*?)</tr>', html, _re.DOTALL)
        buys = [row for row in rows if 'Purchase' in row]

        notable = []
        for buy in buys[:15]:
            ticker_m = _re.search(r'<td class="tiny"><b>(.*?)</b></td>', buy)
            value_m = _re.search(r'\$([0-9,]+)', buy)
            if ticker_m:
                ticker = ticker_m.group(1)
                value = value_m.group(1) if value_m else "?"
                notable.append({"ticker": ticker, "value": f"${value}"})

        return {
            "total_buys_7d": len(buys),
            "notable": notable[:10],
            "summary": f"{len(buys)} insider buys in last 7 days" + (
                f", notable: {', '.join(n['ticker'] for n in notable[:5])}" if notable else ""
            ),
        }
    except Exception as e:
        return {"error": str(e), "total_buys_7d": 0}


def format_for_debate(enrichment: Dict[str, Any]) -> str:
    """格式化为 debate_context 文本 (控制在 2000 字以内)"""
    lines = ["═══ PRE-MARKET DATA ENRICHMENT ═══\n"]

    # Market Indices
    pm = enrichment.get("premarket_movers", {})
    indices = pm.get("market_indices", {})
    if indices:
        lines.append("📈 MARKET SNAPSHOT:")
        for sym in ["SPY", "QQQ", "IWM", "DIA", "VIX"]:
            if sym in indices:
                d = indices[sym]
                prefix = "  " if sym != "VIX" else "  ⚡"
                lines.append(f"{prefix}{sym}: ${d['price']} ({d['change_pct']:+.2f}%)")
        lines.append("")

    # Sector Rotation
    sr = enrichment.get("sector_rotation", {})
    ranked = sr.get("ranked", [])
    if ranked:
        lines.append("📊 SECTOR ROTATION (daily change):")
        for r in ranked:
            bar = "▲" if r["change_pct"] > 0 else "▼"
            lines.append(f"  {bar} {r['sector']:20} ({r['etf']}): {r['change_pct']:+.2f}%")
        lines.append(f"  Leaders: {', '.join(sr.get('leaders', []))}")
        lines.append(f"  Laggards: {', '.join(sr.get('laggards', []))}")
        lines.append("")

    # Pre-Market Movers
    gainers = pm.get("gainers", [])
    losers = pm.get("losers", [])
    if gainers or losers:
        lines.append("🔥 PRE-MARKET MOVERS:")
        if gainers:
            lines.append("  Gainers:")
            for g in gainers[:5]:
                lines.append(f"    ▲ {g['symbol']}: ${g['price']} ({g['change_pct']:+.1f}%)")
        if losers:
            lines.append("  Losers:")
            for l in losers[:5]:
                lines.append(f"    ▼ {l['symbol']}: ${l['price']} ({l['change_pct']:+.1f}%)")
        lines.append("")

    # Analyst Sentiment (compact: only show notable changes)
    analyst = enrichment.get("analyst_sentiment", {})
    if analyst:
        notable = []
        for sym, data in analyst.items():
            if data.get('trend') != 'stable' or data['bull_pct'] < 50 or data['bull_pct'] > 90:
                notable.append(f"  {sym}: {data['bull_pct']:.0f}% bullish [{data['trend']}]")
        if notable:
            lines.append("🎯 ANALYST SENTIMENT (notable only):")
            lines.extend(notable[:6])
            lines.append("")

    # Earnings Calendar (compact: top 5)
    ec = enrichment.get("earnings_calendar", "")
    if ec and ec != "Unable to fetch" and "No notable" not in ec:
        # Only include first 7 lines (header + top 5 + blank)
        ec_lines = ec.split("\n")[:7]
        lines.extend(ec_lines)
        lines.append("")

    # Economic Calendar
    econ = enrichment.get("economic_calendar", "")
    if econ:
        lines.append(f"📅 ECONOMIC CALENDAR:\n  {econ}")
        lines.append("")

    # News Headlines (top 3)
    news = enrichment.get("market_news", [])
    if news:
        lines.append("📰 TOP NEWS:")
        for n in news[:3]:
            lines.append(f"  [{n['source']}] {n['headline']}")
        lines.append("")

    # Treasury Yields (FRED)
    ty = enrichment.get("treasury_yields", {})
    if ty:
        lines.append("🏦 TREASURY YIELDS (FRED):")
        as_of = ty.get("as_of") or {}
        for label in ["2Y", "5Y", "10Y"]:
            if label in ty:
                lines.append(f"  {label}: {ty[label]}% (as of {as_of.get(label, 'N/A')})")
        if ty.get("spread_10y_2y") is not None:
            lines.append(f"  10Y-2Y Spread: {ty['spread_10y_2y']}% → {ty.get('curve_shape', '')} "
                         f"(as of {as_of.get('10Y')})")
        else:
            lines.append(f"  10Y-2Y Spread: N/A ({ty.get('curve_shape', 'observation unavailable')})")
        if ty.get("VIX") is not None:
            lines.append(f"  VIX: {ty['VIX']} (FRED close, as of {as_of.get('VIX', 'N/A')})")
        if ty.get("FedFunds") is not None:
            lines.append(f"  Fed Funds: {ty['FedFunds']}% (as of {as_of.get('FedFunds', 'N/A')})")
        lines.append(f"  → {ty.get('interpretation', '')}")
        lines.append("")

    # Insider Trading (OpenInsider)
    it = enrichment.get("insider_trading", {})
    if it and it.get("total_buys_7d", 0) > 0:
        lines.append("🕵️ INSIDER TRADING (7D):")
        lines.append(f"  {it.get('summary', '')}")
        for n in it.get("notable", [])[:5]:
            lines.append(f"    {n['ticker']}: {n['value']}")
        lines.append("")

    # 借券拥挤度 (2026-08-29 RFC-062, record-only 决策参考)
    bc = enrichment.get("borrow_crowding", {})
    if bc.get("flagged"):
        from app.data.borrow_crowding import format_for_context
        _bc_txt = format_for_context(bc)
        if _bc_txt:
            lines.append(_bc_txt)
            lines.append("")

    return "\n".join(lines)
