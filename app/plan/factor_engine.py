"""
Factor Engine — 量化预筛 (Stage 1 of Battle Plan Pipeline)

5因子等权评分，从~35只活跃股中选出Top 8候选。
纯Python计算，无LLM依赖，可独立验证因子有效性。

因子:
  1. 板块动量 (sector_momentum) — 所属板块ETF 5日涨幅
  2. 相对强度 (relative_strength) — 个股20日涨幅 vs SPY
  3. 均线排列 (ma_alignment) — MA5>MA10>MA20多空信号
  4. 量价突破 (volume_breakout) — 当日量/20日均量
  5. RSI甜区 (rsi_sweet_spot) — RSI 30-50超卖反弹最优

Regime条件权重:
  RISK_ON:  板块30% 相对强度25% 均线20% 量价15% RSI10%
  RISK_OFF: 板块10% 相对强度10% 均线30% 量价10% RSI40%
  ROTATION: 板块40% 相对强度20% 均线15% 量价15% RSI10%
  NEUTRAL:  等权各20%
"""

import logging
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek_us.factor_engine")

# ── 股票池: 活跃池中去掉ETF和非美股 ──
_EXCLUDE_PREFIXES = {"US.HK."}
_EXCLUDE_SYMBOLS = {"US.SPY", "US.QQQ", "US.DIA", "US.IWM",
                    "US.XLK", "US.XLF", "US.XLI", "US.XLV",
                    "US.XLE", "US.XLY", "US.XLP", "US.XLB",
                    "US.XLRE", "US.XLU", "US.XLC"}

# ── 板块映射 (ticker → sector ETF symbol) ──
_SECTOR_MEMBERS = {
    "Technology": ["NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMD", "ORCL", "CRM"],
    "Semiconductors": ["NVDA", "AMD", "AVGO", "TSM", "MU", "QCOM"],
    "Consumer": ["AMZN", "TSLA", "NKE", "WMT", "SBUX"],
    "Financials": ["JPM", "BAC", "GS", "MS", "V", "MA"],
    "Healthcare": ["JNJ", "UNH", "LLY", "PFE", "MRK"],
    "Energy": ["XOM", "CVX", "COP"],
    "China_ADR": ["BABA", "PDD", "JD", "BIDU", "NIO", "NTES", "BILI", "LI"],
    "Communication": ["GOOG", "GOOGL", "META", "NFLX", "DIS"],
}

_TICKER_TO_SECTORS: Dict[str, List[str]] = {}
for _sec, _tickers in _SECTOR_MEMBERS.items():
    for _t in _tickers:
        _TICKER_TO_SECTORS.setdefault(_t, []).append(_sec)

# ── Regime权重矩阵 ──
REGIME_WEIGHTS = {
    "RISK_ON":  {"sector_momentum": 0.30, "relative_strength": 0.25, "ma_alignment": 0.20, "volume_breakout": 0.15, "rsi_sweet_spot": 0.10},
    "RISK_OFF": {"sector_momentum": 0.10, "relative_strength": 0.10, "ma_alignment": 0.30, "volume_breakout": 0.10, "rsi_sweet_spot": 0.40},
    "ROTATION": {"sector_momentum": 0.40, "relative_strength": 0.20, "ma_alignment": 0.15, "volume_breakout": 0.15, "rsi_sweet_spot": 0.10},
    "NEUTRAL":  {"sector_momentum": 0.20, "relative_strength": 0.20, "ma_alignment": 0.20, "volume_breakout": 0.20, "rsi_sweet_spot": 0.20},
}


def _get_stock_pool() -> List[str]:
    """获取候选股票池: active_pool - ETFs - 非美股"""
    pool_path = Path(__file__).parent.parent.parent / "data" / "daily_active_pool.json"
    if not pool_path.exists():
        logger.warning("[FactorEngine] daily_active_pool.json not found")
        return []
    try:
        data = json.loads(pool_path.read_text())
        entries = data.get("entries", [])
        symbols = []
        for e in entries:
            code = e.get("code", "")
            if not code or code in _EXCLUDE_SYMBOLS:
                continue
            if any(code.startswith(p) for p in _EXCLUDE_PREFIXES):
                continue
            if not code.startswith("US."):
                continue
            symbols.append(code)
        return sorted(set(symbols))
    except Exception as e:
        logger.error(f"[FactorEngine] pool read error: {e}")
        return []


def _get_sector_momentum() -> Dict[str, float]:
    """获取板块ETF 5日涨幅 (from Finnhub quote)
    Returns: {"Technology": 2.3, "Financials": -0.5, ...}
    """
    etf_map = {
        "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
        "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Consumer",
        "XLP": "Consumer Staples", "XLC": "Communication",
    }
    result = {}
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        client = _get_finnhub_client()
        if not client:
            return result
        for etf, sector in etf_map.items():
            try:
                q = client.quote(etf)
                prev = q.get("pc", 0)
                cur = q.get("c", 0)
                if prev and cur and prev > 0:
                    result[sector] = round((cur - prev) / prev * 100, 2)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[FactorEngine] sector momentum error: {e}")
    return result


def _compute_technicals(svc, symbol: str) -> Optional[Dict]:
    """计算单只股票的技术因子 (复用Futu K线)"""
    try:
        from futu import RET_OK, SubType
        ret_sub, _ = svc._quote_ctx.subscribe([symbol], [SubType.K_DAY])
        if ret_sub != RET_OK:
            return None
        time.sleep(0.15)

        ret, data = svc._quote_ctx.get_cur_kline(symbol, 60, ktype="K_DAY")
        if ret != RET_OK or data.empty or len(data) < 20:
            return None

        closes = [float(r["close"]) for _, r in data.iterrows()]
        volumes = [float(r["volume"]) for _, r in data.iterrows()]

        result = {"symbol": symbol, "cur_price": closes[-1]}

        # ── Factor 2: Relative Strength (20d change vs SPY) ──
        if len(closes) >= 20:
            result["change_20d"] = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2)
            result["change_5d"] = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2)

        # ── Factor 3: MA Alignment ──
        if len(closes) >= 20:
            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            ma20 = sum(closes[-20:]) / 20
            result["ma5"] = round(ma5, 2)
            result["ma10"] = round(ma10, 2)
            result["ma20"] = round(ma20, 2)
            if ma5 > ma10 > ma20:
                result["ma_alignment"] = "BULLISH"
            elif ma5 < ma10 < ma20:
                result["ma_alignment"] = "BEARISH"
            else:
                result["ma_alignment"] = "MIXED"

        # ── Factor 4: Volume Breakout ──
        if len(volumes) >= 20:
            vol_avg_20 = sum(volumes[-20:]) / 20
            result["vol_ratio"] = round(volumes[-1] / vol_avg_20, 2) if vol_avg_20 > 0 else 1.0
        else:
            result["vol_ratio"] = 1.0

        # ── Factor 5: RSI-14 ──
        if len(closes) >= 15:
            gains, losses = [], []
            for i in range(-14, 0):
                d = closes[i] - closes[i - 1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            if avg_loss > 0:
                result["rsi14"] = round(100 - (100 / (1 + avg_gain / avg_loss)), 1)
            else:
                result["rsi14"] = 100.0

        # ── Price range context ──
        highs = [float(r["high"]) for _, r in data.iterrows()]
        lows = [float(r["low"]) for _, r in data.iterrows()]
        if len(highs) >= 20:
            result["high_20d"] = round(max(highs[-20:]), 2)
            result["low_20d"] = round(min(lows[-20:]), 2)
        if len(closes) >= 5:
            result["change_5d"] = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2)

        return result

    except Exception as e:
        logger.debug(f"[FactorEngine] technicals error for {symbol}: {e}")
        return None


def _score_factor(name: str, value: float, direction: str = "LONG") -> float:
    """将因子原始值转换为0-1标准化得分"""

    if name == "sector_momentum":
        # 板块5日涨幅: -3%→0, 0%→0.5, +3%→1.0
        return max(0, min(1, (value + 3) / 6))

    elif name == "relative_strength":
        # 相对SPY的超额: -5%→0, 0%→0.5, +5%→1.0
        return max(0, min(1, (value + 5) / 10))

    elif name == "ma_alignment":
        # BULLISH=1.0, MIXED=0.5, BEARISH=0.0
        mapping = {"BULLISH": 1.0, "MIXED": 0.5, "BEARISH": 0.0}
        return mapping.get(value, 0.5)

    elif name == "volume_breakout":
        # 量比: 0.5→0, 1.0→0.3, 1.5→0.7, 2.0→1.0
        return max(0, min(1, (value - 0.5) / 1.5))

    elif name == "rsi_sweet_spot":
        # RSI甜区: 30-50最优(1.0), 50-70中等(0.5), <30或>70差(0.2)
        if 30 <= value <= 50:
            return 1.0
        elif 50 < value <= 70:
            return 0.6
        elif 20 <= value < 30:
            return 0.4  # 超卖可能反弹
        else:
            return 0.2  # 极端区域

    return 0.5


def _detect_regime(sector_data: Dict[str, float], spy_change: float = 0) -> str:
    """基于板块离散度和SPY趋势判断市场regime"""
    if not sector_data:
        return "NEUTRAL"

    changes = list(sector_data.values())
    avg = sum(changes) / len(changes) if changes else 0
    spread = max(changes) - min(changes) if changes else 0

    # 板块分散度>4% = 轮动
    if spread > 4:
        return "ROTATION"
    # 大部分板块涨 + SPY涨 = RISK_ON
    elif avg > 0.5 and spy_change > 0:
        return "RISK_ON"
    # 大部分板块跌 = RISK_OFF
    elif avg < -0.5:
        return "RISK_OFF"
    else:
        return "NEUTRAL"


def run_factor_screen(top_n: int = 8) -> Dict:
    """
    执行5因子量化预筛。
    
    Returns: {
        "timestamp": "...",
        "regime": "RISK_ON|...",
        "regime_weights": {...},
        "candidates": [
            {
                "symbol": "US.NVDA",
                "composite_score": 0.78,
                "factors": {
                    "sector_momentum": {"raw": 2.1, "score": 0.85, "weight": 0.20},
                    ...
                },
                "technicals": {...},
                "sectors": ["Technology", "Semiconductors"],
                "direction_hint": "LONG"
            },
            ...
        ],
        "pool_size": 34,
        "sector_momentum": {...},
        "spy_change_20d": 1.2
    }
    """
    t0 = time.time()

    # 1. Get stock pool
    pool = _get_stock_pool()
    if not pool:
        return {"error": "empty_pool", "candidates": [], "timestamp": datetime.now(timezone.utc).isoformat()}

    # 2. Get sector momentum
    sector_momentum = _get_sector_momentum()

    # 3. Get SPY 20d change for regime detection
    spy_change_20d = 0
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        svc = ManualOrderService()
        if svc.connect():
            from futu import RET_OK, SubType
            svc._quote_ctx.subscribe(["US.SPY"], [SubType.K_DAY])
            time.sleep(0.3)
            ret, spy_data = svc._quote_ctx.get_cur_kline("US.SPY", 25, ktype="K_DAY")
            if ret == RET_OK and not spy_data.empty and len(spy_data) >= 20:
                spy_closes = [float(r["close"]) for _, r in spy_data.iterrows()]
                spy_change_20d = round((spy_closes[-1] - spy_closes[-20]) / spy_closes[-20] * 100, 2)

            # 4. Compute technicals for all pool stocks
            technicals = {}
            for symbol in pool:
                tech = _compute_technicals(svc, symbol)
                if tech:
                    technicals[symbol] = tech

            svc.disconnect()
    except Exception as e:
        logger.error(f"[FactorEngine] broker error: {e}")
        technicals = {}

    # 5. Detect regime
    regime = _detect_regime(sector_momentum, spy_change_20d)
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["NEUTRAL"])

    # 6. Score each stock
    candidates = []
    for symbol in pool:
        tech = technicals.get(symbol)
        if not tech:
            continue

        short = symbol.replace("US.", "")
        stock_sectors = _TICKER_TO_SECTORS.get(short, [])

        # Factor scores
        factor_results = {}

        # F1: Sector momentum (best sector among memberships)
        best_sector_chg = 0
        best_sector_name = ""
        for sec in stock_sectors:
            chg = sector_momentum.get(sec, 0)
            if chg > best_sector_chg or not best_sector_name:
                best_sector_chg = chg
                best_sector_name = sec
        factor_results["sector_momentum"] = {
            "raw": best_sector_chg,
            "score": _score_factor("sector_momentum", best_sector_chg),
            "weight": weights["sector_momentum"],
            "detail": f"{best_sector_name} {best_sector_chg:+.1f}%" if best_sector_name else "N/A",
        }

        # F2: Relative strength
        rs = tech.get("change_20d", 0) - spy_change_20d
        factor_results["relative_strength"] = {
            "raw": round(rs, 2),
            "score": _score_factor("relative_strength", rs),
            "weight": weights["relative_strength"],
            "detail": f"{tech.get('change_20d', 0):+.1f}% vs SPY {spy_change_20d:+.1f}%",
        }

        # F3: MA alignment
        ma_align = tech.get("ma_alignment", "MIXED")
        factor_results["ma_alignment"] = {
            "raw": ma_align,
            "score": _score_factor("ma_alignment", ma_align),
            "weight": weights["ma_alignment"],
            "detail": f"MA5={tech.get('ma5',0):.0f} MA10={tech.get('ma10',0):.0f} MA20={tech.get('ma20',0):.0f}",
        }

        # F4: Volume breakout
        vol_ratio = tech.get("vol_ratio", 1.0)
        factor_results["volume_breakout"] = {
            "raw": vol_ratio,
            "score": _score_factor("volume_breakout", vol_ratio),
            "weight": weights["volume_breakout"],
            "detail": f"{vol_ratio:.1f}x avg",
        }

        # F5: RSI sweet spot
        rsi = tech.get("rsi14", 50)
        factor_results["rsi_sweet_spot"] = {
            "raw": rsi,
            "score": _score_factor("rsi_sweet_spot", rsi),
            "weight": weights["rsi_sweet_spot"],
            "detail": f"RSI={rsi:.0f}",
        }

        # Composite score
        composite = sum(f["score"] * f["weight"] for f in factor_results.values())

        # Direction hint from factors
        bullish_signals = sum(1 for f in factor_results.values() if f["score"] >= 0.7)
        bearish_signals = sum(1 for f in factor_results.values() if f["score"] <= 0.3)
        if bullish_signals > bearish_signals:
            direction_hint = "LONG"
        elif bearish_signals > bullish_signals:
            direction_hint = "SHORT"
        else:
            direction_hint = "NEUTRAL"

        candidates.append({
            "symbol": symbol,
            "composite_score": round(composite, 3),
            "factors": factor_results,
            "technicals": {k: v for k, v in tech.items() if k != "symbol"},
            "sectors": stock_sectors,
            "direction_hint": direction_hint,
        })

    # 7. Sort by composite score (descending)
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "regime_weights": weights,
        "candidates": candidates[:top_n],
        "pool_size": len(pool),
        "scored_count": len(candidates),
        "sector_momentum": sector_momentum,
        "spy_change_20d": spy_change_20d,
        "elapsed_sec": round(time.time() - t0, 1),
        # all_scores: lightweight list for watchlist/dashboard (symbol + score only)
        "all_scores": [{"symbol": c["symbol"], "composite_score": c["composite_score"], "direction_hint": c.get("direction_hint", "")} for c in candidates],
    }

    # 8. Save to file
    try:
        out_path = Path(__file__).parent.parent.parent / "data" / "factor_screen_latest.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    except Exception as e:
        logger.debug(f"[FactorEngine] save error: {e}")

    logger.info(
        f"[FactorEngine] ✅ {len(pool)}只→评分{len(candidates)}只→Top{top_n} | "
        f"Regime={regime} | {result['elapsed_sec']}s"
    )
    return result


if __name__ == "__main__":
    """CLI: python app/plan/factor_engine.py"""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = run_factor_screen(top_n=int(sys.argv[1]) if len(sys.argv) > 1 else 8)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
