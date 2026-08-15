#!/usr/bin/env python3
"""E2 alpha剥离: LONG信号收益 vs SPY同期 (剥离大盘beta) + 双窗alpha同号检验."""
import json, sqlite3, time, os, sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

REPO = Path("/opt/ftseek/FTSeek-US")
CACHE_DIR = REPO / "data" / "replay_bars"
OUT_DIR = REPO / "data" / "e2_swing_replay_2026-08-15"
NOTIONAL = 3457.51

MIN_DATE = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
start = (datetime.strptime(MIN_DATE, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")

def load_daily_fresh(sym):
    cache = CACHE_DIR / f"daily_{sym.replace('.', '_')}_{start}_{datetime.now().strftime('%Y-%m-%d')}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from futu import OpenQuoteContext, KLType, RET_OK, AuType
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        for _ in (1, 2):
            ret, data, _ = ctx.request_history_kline(
                sym, ktype=KLType.K_DAY, start=start,
                end=datetime.now().strftime("%Y-%m-%d"), max_count=400, autype=AuType.NONE)
            if ret == RET_OK and data is not None and len(data) > 0:
                bars = [[str(r["time_key"])[:10], float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])] for _, r in data.iterrows()]
                bars.sort(key=lambda b: b[0])
                cache.write_text(json.dumps(bars))
                return bars
            time.sleep(3)
    finally:
        ctx.close()
    return []

spy = load_daily_fresh("US.SPY")
spy_days = {b[0]: b for b in spy}
spy_list = [b[0] for b in spy]
print(f"SPY日线: {len(spy)} bars, {spy[0][0]}~{spy[-1][0]}")

nd = json.load(open(OUT_DIR / "next_day_open_variant.json"))
longs = [r for r in nd["per_trade"] if r["dir"] == "LONG"]

alpha_trades, skipped = [], 0
for r in longs:
    sig_date = r["date"]
    if sig_date not in spy_days:
        skipped += 1
        continue
    sidx = spy_list.index(sig_date)
    if sidx + 6 >= len(spy_list):
        skipped += 1
        continue
    spy_entry_day = spy_list[sidx + 1]
    spy_exit_day = spy_list[sidx + 6]
    spy_ret = (spy_days[spy_exit_day][4] / spy_days[spy_entry_day][1] - 1) * 100
    alpha = r["ret_pct"] - spy_ret
    alpha_trades.append({**r, "spy_ret_pct": round(spy_ret, 3), "alpha_pct": round(alpha, 3)})

print(f"alpha计算: n={len(alpha_trades)}, 跳过{skipped}")

alpha_trades.sort(key=lambda r: r["date"])
mid = len(alpha_trades) // 2
w1, w2 = alpha_trades[:mid], alpha_trades[mid:]

def astats(sub):
    n = len(sub)
    if not n:
        return None
    a = sum(t["alpha_pct"] for t in sub) / n
    w = sum(1 for t in sub if t["alpha_pct"] > 0)
    s = sum(t["spy_ret_pct"] for t in sub) / n
    r = sum(t["ret_pct"] for t in sub) / n
    return {"n": n, "avg_alpha": round(a, 3), "avg_ret": round(r, 3),
            "avg_spy": round(s, 3), "alpha_wr": round(w / n * 100, 1)}

s1, s2 = astats(w1), astats(w2)
print(f"\n前半窗 ({w1[0]['date']}~{w1[-1]['date']}):")
print(f"  信号ret {s1['avg_ret']:+.3f}% vs SPY {s1['avg_spy']:+.3f}% → alpha {s1['avg_alpha']:+.3f}%/笔 (alpha WR {s1['alpha_wr']}%)")
print(f"后半窗 ({w2[0]['date']}~{w2[-1]['date']}):")
print(f"  信号ret {s2['avg_ret']:+.3f}% vs SPY {s2['avg_spy']:+.3f}% → alpha {s2['avg_alpha']:+.3f}%/笔 (alpha WR {s2['alpha_wr']}%)")
same = (s1["avg_alpha"] > 0) == (s2["avg_alpha"] > 0)
print(f"\n→ 双窗alpha{'同号 ✓' if same else '异号 ✗'}")

all_a = astats(alpha_trades)
print(f"全窗: alpha {all_a['avg_alpha']:+.3f}%/笔 (n={all_a['n']}), alpha$/笔 ≈ {all_a['avg_alpha']/100*NOTIONAL:+.2f}")

(OUT_DIR / "alpha_check.json").write_text(json.dumps(
    {"all": all_a, "w1": s1, "w2": s2, "per_trade": alpha_trades},
    ensure_ascii=False, indent=1))
print(f"产出: {OUT_DIR / 'alpha_check.json'}")
