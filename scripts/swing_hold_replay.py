#!/usr/bin/env python3
"""用户提案回测: 波段持有制 (2026-08-15, DEB-20260815 §二十后续).

提案: 宽SL/TR(按ATR波动定宽) + 不限时长持有直到TP或SL (无时间退出).
变体 (全部次日开盘入场, 扣费$4.47, alpha=毛-SPY同期):
  V1: SL=2xATR14, TP=4xATR14 (2R), cap=20交易日
  V2: SL=3xATR14, TP=6xATR14 (2R 更宽), cap=20
  V3: SL=3xATR14, TP=6xATR14, cap=40 (真·长持)
  V4: V2 + breakeven锁: 浮盈达1R后SL提到入场价 (用户'落袋不亏损')
触发序: 同bar先判SL后判TP (保守). cap到期按收盘平.
信号体: DA+UE 方向化信号, (symbol,date,dir)去重, 与E2同口径.
产出: data/swing_hold_replay_2026-08-15/summary.json + per_trade.csv
"""
from __future__ import annotations
import json, sqlite3, sys, time, csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SNAP_DB = REPO / "data" / "signal_snapshots.db"
OUT_DIR = REPO / "data" / "swing_hold_replay_2026-08-15"
CACHE_DIR = REPO / "data" / "replay_bars"
FEE = 4.47
NOTIONAL = 3457.51
MIN_DATE = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")


def load_daily(ctx, symbol, start, end):
    cache = CACHE_DIR / f"daily_{symbol.replace('.', '_')}_{start}_{end}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    from futu import KLType, RET_OK, AuType
    for _ in (1, 2):
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


def atr14(bars, idx):
    """ATR14 截至 bars[idx] (含), 需 idx>=14."""
    if idx < 14:
        return None
    trs = []
    for k in range(idx - 13, idx + 1):
        h, l, pc = bars[k][2], bars[k][3], bars[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def replay_variant(s, bars, spy_days, sl_mult, tp_mult, cap, breakeven_at_1r):
    """返回 (gross_ret, held_days, exit_type) 或 None(数据不足)."""
    days = [b[0] for b in bars]
    if s["date"] not in days:
        return None
    idx = days.index(s["date"])
    eidx = idx + 1
    atr = atr14(bars, idx)
    if atr is None or eidx >= len(bars):
        return None
    entry_px = bars[eidx][1]
    sign = 1.0 if s["dir"] == "LONG" else -1.0
    sl_dist = sl_mult * atr
    tp_dist = tp_mult * atr
    sl_px = entry_px - sign * sl_dist
    tp_px = entry_px + sign * tp_dist
    be_engaged = False
    end = min(eidx + cap, len(bars) - 1)
    for k in range(eidx, end + 1):
        o, h, l, c = bars[k][1], bars[k][2], bars[k][3], bars[k][4]
        # 浮盈达1R → breakeven
        if breakeven_at_1r and not be_engaged:
            fav = (c - entry_px) * sign
            if fav >= sl_dist:
                sl_px = entry_px
                be_engaged = True
        # 先判SL后判TP (保守)
        if (l - sl_px) * sign <= 0 and k > eidx:
            return (sl_px / entry_px - 1) * sign, k - eidx, ("BE" if be_engaged else "SL")
        if k == eidx and (o - sl_px) * sign <= 0:
            return (o / entry_px - 1) * sign, 0, "SL_OPEN"
        if (h - tp_px) * sign >= 0:
            return (tp_px / entry_px - 1) * sign, k - eidx, "TP"
    return (bars[end][4] / entry_px - 1) * sign, end - eidx, "CAP"


def stats(vals):
    n = len(vals)
    if not n:
        return None
    net = sum(vals)
    return {"n": n, "net": round(net, 1), "avg": round(net / n, 2),
            "wr": round(sum(1 for v in vals if v > 0) / n * 100, 1)}


def main():
    con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"""
        SELECT symbol, trade_date, direction, strength
        FROM signal_snapshots
        WHERE trade_date>='{MIN_DATE}' AND strategy_id IN ('deep_analysis','universe_expansion')
          AND direction IN ('LONG','SHORT')
        ORDER BY trade_date
    """).fetchall()
    con.close()
    agg = defaultdict(list)
    for r in rows:
        agg[(r["symbol"], r["trade_date"], r["direction"])].append(r)
    signals = [{"symbol": k[0], "date": k[1], "dir": k[2],
                "strength": sum(x["strength"] for x in v) / len(v)}
               for k, v in agg.items()]
    print(f"独立信号 n={len(signals)}")

    syms = sorted({s["symbol"] for s in signals} | {"US.SPY"})
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(MIN_DATE, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    print(f"拉日线: {len(syms)} symbols (缓存优先)")
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
                print(f"  {i+1}/{len(syms)}")
    finally:
        ctx.close()
    print(f"日线就绪 {len(daily)}/{len(syms)}")
    spy = {b[0]: b for b in daily.get("US.SPY", [])}

    variants = [
        ("V1_sl2r_tp4r_cap20", 2.0, 4.0, 20, False),
        ("V2_sl3r_tp6r_cap20", 3.0, 6.0, 20, False),
        ("V3_sl3r_tp6r_cap40", 3.0, 6.0, 40, False),
        ("V4_V2+breakeven1R", 3.0, 6.0, 20, True),
    ]
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "fee": FEE, "notional": NOTIONAL, "n_signals": len(signals),
           "variants": {}, "by_dir": {}}
    per_trade = []
    for vname, slm, tpm, cap, be in variants:
        nets, nets_alpha, by_dir, exit_types = [], [], defaultdict(list), defaultdict(int)
        for s in signals:
            bars = daily.get(s["symbol"])
            if not bars:
                continue
            res = replay_variant(s, bars, spy, slm, tpm, cap, be)
            if res is None:
                continue
            gross, held, etype = res
            exit_types[etype] += 1
            net = gross * NOTIONAL - FEE
            nets.append(net)
            by_dir[s["dir"]].append(net)
            # alpha
            days = [b[0] for b in bars]
            eidx = days.index(s["date"]) + 1
            eday = days[min(eidx + held, len(days) - 1)]
            spy0, spy1 = spy.get(bars[eidx][0]), spy.get(eday)
            alpha_net = net
            if spy0 and spy1:
                sign = 1.0 if s["dir"] == "LONG" else -1.0
                spy_ret = (spy1[4] / spy0[1] - 1) * sign
                alpha_net = (gross - spy_ret) * NOTIONAL - FEE
            nets_alpha.append(alpha_net)
            if vname == "V2_sl3r_tp6r_cap20":
                per_trade.append({"symbol": s["symbol"], "date": s["date"], "dir": s["dir"],
                                  "gross_pct": round(gross * 100, 2), "net": round(net, 2),
                                  "alpha_net": round(alpha_net, 2), "held": held, "exit": etype})
        out["variants"][vname] = {
            "raw": stats(nets), "alpha": stats(nets_alpha),
            "by_dir_alpha": {d: stats(v) for d, v in by_dir.items()},
            "exit_types": dict(exit_types),
        }
        print(f"\n{vname}: raw={out['variants'][vname]['raw']} alpha={out['variants'][vname]['alpha']}")
        print(f"  by_dir={out['variants'][vname]['by_dir_alpha']} exits={dict(exit_types)}")

    # 双窗 (V2 alpha)
    v2_trades = per_trade
    if v2_trades:
        dates = sorted(t["date"] for t in v2_trades)
        mid = dates[len(dates) // 2]
        w1 = stats([t["alpha_net"] for t in v2_trades if t["date"] <= mid])
        w2 = stats([t["alpha_net"] for t in v2_trades if t["date"] > mid])
        out["V2_dual_window"] = {"w1": w1, "w2": w2}
        print(f"\nV2双窗: w1={w1} w2={w2}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    if per_trade:
        with open(OUT_DIR / "per_trade.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(per_trade[0].keys()))
            w.writeheader()
            for r in per_trade:
                w.writerow(r)


if __name__ == "__main__":
    main()
