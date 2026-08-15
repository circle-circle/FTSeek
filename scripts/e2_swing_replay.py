#!/usr/bin/env python3
"""E2 存量重放 (DEB-20260815 §14.4.3 主力证据) — 2026-08-15 W0.

口径 (DEB §14.4.1 判据): 结算 = 信号日开盘价入场, 持有至 T+5 收盘 (3-5日摆动),
  扣实盘费率 ($4.47/笔, 7/1+ 实测均值); 不用信号日收盘价结算。
信号体: 60天 DA+UE signal_snapshots, LONG/SHORT, 按 (symbol, trade_date, direction)
  独立样本去重 (同组取 mean strength, 首个 close 基准)。
机械出场变体: 变体A=持满5日; 变体B=EOD止损 (任一持仓日收盘 < 入场×0.97 → 次日开盘平)。
诚实披露: 信号日=交易日 (DA 盘前 07:00ET 生成), "信号次一交易日开盘"即信号日开盘;
  8/13 信号的 T+5 窗口截断 (数据到 8/15), 截断样本剔除不推算。
产出: data/e2_swing_replay_2026-08-15/summary.json + per_trade.csv
"""
from __future__ import annotations
import json, sqlite3, sys, time, csv
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "ftseek_us.db"
SNAP_DB = REPO / "data" / "signal_snapshots.db"
OUT_DIR = REPO / "data" / "e2_swing_replay_2026-08-15"
CACHE_DIR = REPO / "data" / "replay_bars"
MIN_DATE = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
FEE = 4.47
NOTIONAL = 3457.51
HOLD_DAYS = 5
EOD_SL = 0.03


def load_daily(ctx, symbol: str, start: str, end: str) -> list:
    """拉日线缓存 (与 gate_a_mfe_replay 同模式: 磁盘缓存+重试+限流)."""
    cache = CACHE_DIR / f"daily_{symbol.replace('.', '_')}_{start}_{end}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    from futu import KLType, RET_OK, AuType
    for attempt in (1, 2):
        ret, data, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_DAY, start=start, end=end,
            max_count=400, autype=AuType.NONE)
        if ret == RET_OK and data is not None and len(data) > 0:
            bars = [[str(row["time_key"])[:10], float(row["open"]), float(row["high"]),
                     float(row["low"]), float(row["close"])] for _, row in data.iterrows()]
            bars.sort(key=lambda b: b[0])
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(bars))
            time.sleep(1.3)
            return bars
        time.sleep(3.0)
    time.sleep(1.3)
    return []


def main():
    con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"""
        SELECT symbol, trade_date, direction, strength
        FROM signal_snapshots
        WHERE trade_date>='{MIN_DATE}' AND strategy_id IN ('deep_analysis','universe_expansion')
          AND fwd_ret_t5 IS NOT NULL AND direction IN ('LONG','SHORT')
        ORDER BY trade_date
    """).fetchall()
    con.close()
    print(f"信号行 n={len(rows)} ({MIN_DATE}起)")

    # 独立样本去重
    from collections import defaultdict
    agg = defaultdict(list)
    for r in rows:
        agg[(r["symbol"], r["trade_date"], r["direction"])].append(r)
    signals = []
    for (sym, d, direction), grp in agg.items():
        signals.append({"symbol": sym, "date": d, "dir": direction,
                        "strength": sum(x["strength"] for x in grp) / len(grp),
                        "n_signals": len(grp)})
    print(f"独立信号 (symbol-day-dir): n={len(signals)}")

    # 拉日线
    syms = sorted({s["symbol"] for s in signals})
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(MIN_DATE, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    print(f"拉日线: {len(syms)} symbols, {start}~{end}")
    from futu import OpenQuoteContext
    import os
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    daily = {}
    try:
        for i, sym in enumerate(syms):
            bars = load_daily(ctx, sym, start, end)
            if bars:
                daily[sym] = bars
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(syms)} symbols fetched")
    finally:
        ctx.close()
    print(f"日线就绪: {len(daily)}/{len(syms)}")

    # 重放
    def trading_days(sym):
        return [b[0] for b in daily.get(sym, [])]

    results, excluded = [], []
    for s in signals:
        bars = daily.get(s["symbol"])
        if not bars:
            excluded.append(f"{s['symbol']} {s['date']} no_bars")
            continue
        days = [b[0] for b in bars]
        if s["date"] not in days:
            excluded.append(f"{s['symbol']} {s['date']} sig_date_missing")
            continue
        idx = days.index(s["date"])
        # 需要信号日开盘 + 后续 HOLD_DAYS 个交易日收盘
        if idx + HOLD_DAYS >= len(bars):
            excluded.append(f"{s['symbol']} {s['date']} window_truncated")
            continue
        entry_bar = bars[idx]
        entry_px = entry_bar[1]  # 信号日开盘
        sign = 1.0 if s["dir"] == "LONG" else -1.0
        # 变体A: 持满5日
        exit_px_a = bars[idx + HOLD_DAYS][4]
        ret_a = (exit_px_a / entry_px - 1) * sign
        net_a = ret_a * NOTIONAL - FEE
        # 变体B: EOD止损 -3%
        net_b = net_a
        exit_day_b = idx + HOLD_DAYS
        for k in range(idx + 1, idx + HOLD_DAYS + 1):
            close_k = bars[k][4]
            if (close_k / entry_px - 1) * sign <= -EOD_SL:
                # 次日开盘平
                if k + 1 < len(bars):
                    exit_px_b = bars[k + 1][1]
                    exit_day_b = k + 1
                else:
                    exit_px_b = close_k
                    exit_day_b = k
                ret_b = (exit_px_b / entry_px - 1) * sign
                net_b = ret_b * NOTIONAL - FEE
                break
        results.append({
            "symbol": s["symbol"], "date": s["date"], "dir": s["dir"],
            "strength": round(s["strength"], 1), "n_signals": s["n_signals"],
            "entry_px": round(entry_px, 4),
            "exit_px_a": round(exit_px_a, 4), "ret_a_pct": round(ret_a * 100, 3),
            "net_a": round(net_a, 2), "net_b": round(net_b, 2),
            "held_days_b": exit_day_b - idx,
        })

    def stats(sub, key):
        n = len(sub)
        if not n:
            return None
        net = sum(r[key] for r in sub)
        w = sum(1 for r in sub if r[key] > 0)
        return {"n": n, "net": round(net, 1), "avg": round(net / n, 2),
                "wr": round(w / n * 100, 1)}

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": MIN_DATE, "fee": FEE, "notional": NOTIONAL,
        "n_signals": len(signals), "n_replayed": len(results),
        "n_excluded": len(excluded), "excluded_sample": excluded[:10],
        "variant_A_hold5_all": stats(results, "net_a"),
        "variant_B_eod_sl3_all": stats(results, "net_b"),
        "by_dir": {},
        "per_trade": results,
    }
    for d in ("LONG", "SHORT"):
        sub = [r for r in results if r["dir"] == d]
        out["by_dir"][d] = {"A": stats(sub, "net_a"), "B": stats(sub, "net_b")}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    cols = ["symbol", "date", "dir", "strength", "entry_px", "exit_px_a",
            "ret_a_pct", "net_a", "net_b", "held_days_b"]
    with open(OUT_DIR / "per_trade.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    print("\n=== E2 重放结果 ===")
    for k in ("variant_A_hold5_all", "variant_B_eod_sl3_all"):
        v = out[k]
        if v:
            print(f"{k}: n={v['n']} 净{v['net']:+.0f} 均笔{v['avg']:+.2f} WR{v['wr']}%")
    for d, vv in out["by_dir"].items():
        print(f"  {d}: A={vv['A']} B={vv['B']}")
    print(f"剔出 {len(excluded)} (截断/缺bar), 样例: {excluded[:5]}")


if __name__ == "__main__":
    main()
