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
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

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
    """获取 Finnhub 客户端 (懒加载)"""
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
    return finnhub.Client(api_key=api_key)


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

    client = _get_finnhub_client()
    if not client:
        logger.warning("[PreMarket] Finnhub API key not configured")
        return {"error": "Finnhub not configured"}

    result = {"collected_at": datetime.utcnow().isoformat() + "Z"}

    # ── 1. Sector Rotation ──
    try:
        result["sector_rotation"] = _collect_sector_rotation(client)
    except Exception as e:
        logger.warning(f"[PreMarket] sector rotation failed: {e}")
        result["sector_rotation"] = {"error": str(e)}

    # ── 2. Pre-Market Movers ──
    try:
        result["premarket_movers"] = _collect_premarket_movers(client, watchlist_symbols)
    except Exception as e:
        logger.warning(f"[PreMarket] premarket movers failed: {e}")
        result["premarket_movers"] = {"error": str(e)}

    # ── 3. Analyst Sentiment ──
    if watchlist_symbols:
        try:
            result["analyst_sentiment"] = _collect_analyst_sentiment(client, watchlist_symbols)
        except Exception as e:
            logger.warning(f"[PreMarket] analyst sentiment failed: {e}")

    # ── 4. Earnings Calendar ──
    try:
        result["earnings_calendar"] = _collect_earnings_calendar(client)
    except Exception as e:
        logger.warning(f"[PreMarket] earnings calendar failed: {e}")
        result["earnings_calendar"] = "Unable to fetch"

    # ── 5. Market News ──
    try:
        result["market_news"] = _collect_market_news(client)
    except Exception as e:
        logger.warning(f"[PreMarket] market news failed: {e}")
        result["market_news"] = []

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
    logger.info(
        f"[PreMarket] 数据采集完成: {n_sectors} sectors, "
        f"gainers={len(movers.get('gainers', []))}, "
        f"losers={len(movers.get('losers', []))}, "
        f"news={n_news}"
    )

    return result


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
    today = datetime.utcnow().date()
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


def _get_economic_calendar() -> str:
    """本周重要经济数据 (硬编码规则 — 最可靠)"""
    today = datetime.utcnow().date()
    week_end = today + timedelta(days=7)
    events = []

    # FOMC 2026 dates
    fomc_dates = [
        (2026, 1, 28), (2026, 3, 18), (2026, 5, 6), (2026, 6, 17),
        (2026, 7, 29), (2026, 9, 16), (2026, 11, 4), (2026, 12, 16),
    ]
    for y, m, d in fomc_dates:
        fomc = datetime(y, m, d).date()
        if today <= fomc <= week_end:
            events.append(f"⚡ FOMC Decision: {fomc.strftime('%b %d')} — HIGH IMPACT")

    weekday = today.weekday()
    if today.day <= 7 and weekday == 4:
        events.append("📊 NFP Jobs Report TODAY (8:30 AM ET)")
    if 8 <= today.day <= 15 and weekday in (1, 2):
        events.append("📊 CPI Release likely this week (8:30 AM ET)")
    if weekday == 3:
        events.append("📊 Weekly Jobless Claims TODAY (8:30 AM ET)")

    if not events:
        events.append("No major scheduled events this week")

    return "\n".join(events)


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
    today = datetime.utcnow().date()
    start = (today - timedelta(days=14)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    for sid, label in series_map.items():
        try:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?chart_type=line&id={sid}&cosd={start}&coed={end}"
            r = _req.get(url, timeout=10)
            lines = r.text.strip().split('\n')
            if len(lines) >= 2:
                last = lines[-1].split(',')
                if len(last) == 2 and last[1] != '.':
                    results[label] = round(float(last[1]), 2)
        except Exception:
            continue

    # Compute spread
    y10 = results.get("10Y")
    y2 = results.get("2Y")
    spread = None
    curve_shape = "unknown"
    if y10 is not None and y2 is not None:
        spread = round(y10 - y2, 2)
        if spread > 0.5:
            curve_shape = "steep (growth expected)"
        elif spread > 0:
            curve_shape = "normal (balanced)"
        elif spread > -0.3:
            curve_shape = "flat (uncertainty)"
        else:
            curve_shape = "INVERTED (recession signal)"

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
    if ty and "10Y" in ty:
        lines.append("🏦 TREASURY YIELDS (FRED):")
        for label in ["2Y", "5Y", "10Y"]:
            if label in ty:
                lines.append(f"  {label}: {ty[label]}%")
        if ty.get("spread_10y_2y") is not None:
            lines.append(f"  10Y-2Y Spread: {ty['spread_10y_2y']}% → {ty.get('curve_shape', '')}")
        if ty.get("VIX"):
            lines.append(f"  VIX: {ty['VIX']}")
        if ty.get("FedFunds"):
            lines.append(f"  Fed Funds: {ty['FedFunds']}%")
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

    return "\n".join(lines)
