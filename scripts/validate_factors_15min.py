#!/usr/bin/env python3
"""
15 分钟 K 线因子验证脚本

通过 Futu API 获取 15min K 线数据，验证 SSS/GDIAN/MFFF 因子在日内级别的有效性。

Usage:
    python3 scripts/validate_factors_15min.py
    python3 scripts/validate_factors_15min.py --symbols US.AAPL,US.MSFT,US.SPY
    python3 scripts/validate_factors_15min.py --days 60

依赖: Futu OpenD 服务运行中
"""
import sys
import os
import json
import time
import argparse
import statistics
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ============================================================
#  Futu 数据获取
# ============================================================

def fetch_15min_klines(symbols: List[str], days: int = 60) -> Dict[str, List[Dict]]:
    """通过 Futu API 获取 15min K 线"""
    try:
        from futu import OpenQuoteContext, KLType, SubType
    except ImportError:
        print("❌ futu-api 未安装。尝试 pip install futu-api")
        return {}

    host = os.environ.get("FUTU_HOST", "127.0.0.1")
    port = int(os.environ.get("FUTU_PORT", "11111"))

    print(f"🔌 连接 Futu OpenD {host}:{port}...")
    quote_ctx = OpenQuoteContext(host=host, port=port)

    all_data = {}
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    for symbol in symbols:
        futu_symbol = symbol.replace("US.", "US.")
        print(f"  📊 获取 {symbol} 15min K线 ({start_date} ~ {end_date})...")

        try:
            ret, data, _ = quote_ctx.request_history_kline(
                code=futu_symbol,
                ktype=KLType.K_15M,
                start=start_date,
                end=end_date,
                max_count=1000,
            )

            if ret == 0 and data is not None and len(data) > 0:
                bars = []
                for _, row in data.iterrows():
                    bars.append({
                        "datetime": str(row["time_key"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row["volume"]),
                    })
                all_data[symbol] = bars
                print(f"    ✅ {len(bars)} bars")
            else:
                print(f"    ⚠️ 无数据: {data}")

            time.sleep(0.5)  # 限频

        except Exception as e:
            print(f"    ❌ 错误: {e}")

    quote_ctx.close()
    return all_data


# ============================================================
#  技术指标计算 (复用 validate_factors.py)
# ============================================================

from scripts.validate_factors import compute_indicators, build_market_data


# ============================================================
#  因子验证 (15min 级别)
# ============================================================

def validate_15min_factors(all_klines: Dict[str, List[Dict]]) -> Dict[str, Any]:
    """在 15min K 线上验证因子"""
    from app.factors.sss import SSSFactor
    from app.factors.gdian import GDIANFactor
    from app.factors.mfff import MFFFFactor

    sss_f = SSSFactor()
    gdian_f = GDIANFactor()
    mfff_f = MFFFFactor()

    all_data = []

    for symbol, bars in all_klines.items():
        bars = compute_indicators(bars)

        for i in range(50, len(bars) - 6):  # 50 warmup, 6 forward bars (1.5h)
            md = build_market_data(bars[i])
            sv = sss_f.compute(symbol, md)
            gv = gdian_f.compute(symbol, md)
            mv = mfff_f.compute(symbol, md)

            # Forward returns: 1-bar (15min), 4-bar (1h), 6-bar (1.5h)
            ret_1b = (bars[i+1]["close"] - bars[i]["close"]) / bars[i]["close"]
            ret_4b = (bars[i+4]["close"] - bars[i]["close"]) / bars[i]["close"] if i+4 < len(bars) else None
            ret_6b = (bars[i+6]["close"] - bars[i]["close"]) / bars[i]["close"] if i+6 < len(bars) else None

            all_data.append({
                "symbol": symbol,
                "datetime": bars[i]["datetime"],
                "sss": sv.score,
                "gdian_value": gv.value,
                "gdian_score": gv.score,
                "gdian_direction": gv.direction_hint,
                "mfff": mv.score,
                "ret_1b": ret_1b,
                "ret_4b": ret_4b,
                "ret_6b": ret_6b,
                "price": bars[i]["close"],
                "atr": bars[i]["atr_14"],
            })

    n = len(all_data)
    print(f"\n{'=' * 80}")
    print(f"  15min 因子验证: {n} data points across {len(all_klines)} symbols")
    print(f"{'=' * 80}")

    results = {"n_data": n, "symbols": list(all_klines.keys()), "analyses": {}}

    # ── GDIAN 分层 ──
    print(f"\n--- GDIAN 分层 (15min) ---")
    gdi_bins = [(0, 3), (3, 5), (5, 10), (10, 20), (20, 30), (30, 100)]
    gdi_results = []

    print(f"{'GDIAN区间':<12} {'样本':>6} {'占比':>6} {'15m收益':>8} {'15m WR':>7} {'1h收益':>8} {'1h WR':>7}")
    print("-" * 60)

    for lo, hi in gdi_bins:
        subset = [d for d in all_data if lo <= d["gdian_value"] < hi]
        if len(subset) < 10:
            continue

        r1 = [d["ret_1b"] for d in subset]
        r4 = [d["ret_4b"] for d in subset if d.get("ret_4b") is not None]

        avg_1b = statistics.mean(r1)
        wr_1b = sum(1 for r in r1 if r > 0) / len(r1)
        avg_4b = statistics.mean(r4) if r4 else 0
        wr_4b = sum(1 for r in r4 if r > 0) / len(r4) if r4 else 0

        print(f"  [{lo:2d},{hi:3d})   {len(subset):>5} {len(subset)/n:>5.0%} {avg_1b*100:>+7.4f}% {wr_1b:>6.1%} {avg_4b*100:>+7.4f}% {wr_4b:>6.1%}")
        gdi_results.append({"range": f"[{lo},{hi})", "n": len(subset), "ret_15m": avg_1b, "wr_15m": wr_1b, "ret_1h": avg_4b, "wr_1h": wr_4b})

    results["analyses"]["gdian_bins"] = gdi_results

    # ── MFFF 分层 ──
    print(f"\n--- MFFF 分层 (15min) ---")
    mfff_bins = [(-1, -0.2), (-0.2, -0.05), (-0.05, 0.05), (0.05, 0.2), (0.2, 1)]

    print(f"{'MFFF区间':<14} {'样本':>6} {'占比':>6} {'15m收益':>8} {'15m WR':>7} {'1h收益':>8} {'1h WR':>7}")
    print("-" * 62)

    for lo, hi in mfff_bins:
        subset = [d for d in all_data if lo <= d["mfff"] < hi]
        if len(subset) < 10:
            continue

        r1 = [d["ret_1b"] for d in subset]
        r4 = [d["ret_4b"] for d in subset if d.get("ret_4b") is not None]

        avg_1b = statistics.mean(r1)
        wr_1b = sum(1 for r in r1 if r > 0) / len(r1)
        avg_4b = statistics.mean(r4) if r4 else 0
        wr_4b = sum(1 for r in r4 if r > 0) / len(r4) if r4 else 0

        print(f"  [{lo:+.2f},{hi:+.2f})  {len(subset):>5} {len(subset)/n:>5.0%} {avg_1b*100:>+7.4f}% {wr_1b:>6.1%} {avg_4b*100:>+7.4f}% {wr_4b:>6.1%}")

    # ── 组合信号 ──
    print(f"\n--- 组合信号 (15min) ---")
    combos = [
        ("GDIAN[5,10)+MFFF>0 (均值回归)", lambda d: 5 <= d["gdian_value"] < 10 and d["mfff"] > 0),
        ("GDIAN<5+MFFF>0 (混乱+动量)", lambda d: d["gdian_value"] < 5 and d["mfff"] > 0),
        ("GDIAN>=30 (趋势耗尽做空)", lambda d: d["gdian_value"] >= 30),
        ("SSS<=-0.3+MFFF>0 (超卖+动量)", lambda d: d["sss"] <= -0.3 and d["mfff"] > 0),
        ("GDIAN[5,10)+MFFF>0.1", lambda d: 5 <= d["gdian_value"] < 10 and d["mfff"] > 0.1),
    ]

    print(f"{'组合':<35} {'样本':>5} {'占比':>5} {'15m':>8} {'WR':>5} {'1h':>8} {'WR':>5}")
    print("-" * 75)

    for name, fn in combos:
        signals = [d for d in all_data if fn(d)]
        if len(signals) < 10:
            print(f"  {name:<33} {'<10':>5}")
            continue

        r1 = [d["ret_1b"] for d in signals]
        r4 = [d["ret_4b"] for d in signals if d.get("ret_4b") is not None]

        # For SHORT signal, negate returns
        if "做空" in name:
            r1 = [-r for r in r1]
            r4 = [-r for r in r4]

        avg_1b = statistics.mean(r1)
        wr_1b = sum(1 for r in r1 if r > 0) / len(r1)
        avg_4b = statistics.mean(r4) if r4 else 0
        wr_4b = sum(1 for r in r4 if r > 0) / len(r4) if r4 else 0

        print(f"  {name:<33} {len(signals):>5} {len(signals)/n:>4.0%} {avg_1b*100:>+7.4f}% {wr_1b:>4.0%} {avg_4b*100:>+7.4f}% {wr_4b:>4.0%}")

    # ── 按标的分解 ──
    print(f"\n--- 按标的 (GDIAN[5,10)+MFFF>0) ---")
    for symbol in sorted(all_klines.keys()):
        sym_data = [d for d in all_data if d["symbol"] == symbol and 5 <= d["gdian_value"] < 10 and d["mfff"] > 0]
        if len(sym_data) < 5:
            continue
        r1 = [d["ret_1b"] for d in sym_data]
        avg = statistics.mean(r1)
        wr = sum(1 for r in r1 if r > 0) / len(r1)
        print(f"  {symbol}: {len(sym_data)} signals, 15m avg={avg*100:+.4f}%, WR={wr:.0%}")

    return results


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="15min 因子验证")
    parser.add_argument("--symbols", type=str, default="US.AAPL,US.MSFT,US.SPY,US.NVDA,US.AMZN,US.BABA",
                        help="逗号分隔的symbols")
    parser.add_argument("--days", type=int, default=60, help="获取多少天的数据")
    parser.add_argument("--cache", action="store_true", help="缓存数据到本地")
    parser.add_argument("--from-cache", type=str, default="", help="从缓存加载")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]

    if args.from_cache:
        print(f"📁 从缓存加载: {args.from_cache}")
        with open(args.from_cache) as f:
            all_klines = json.load(f)
    else:
        all_klines = fetch_15min_klines(symbols, args.days)

        if args.cache and all_klines:
            cache_path = f"data/backtest_cache/15min_{datetime.now().strftime('%Y%m%d')}.json"
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(all_klines, f, indent=2)
            print(f"💾 缓存已保存: {cache_path}")

    if not all_klines:
        print("❌ 无数据，退出")
        return

    results = validate_15min_factors(all_klines)

    # 保存结果
    output_path = f"data/debates/15min_validation_{datetime.now().strftime('%Y%m%d')}.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n📄 结果已保存: {output_path}")


if __name__ == "__main__":
    main()
