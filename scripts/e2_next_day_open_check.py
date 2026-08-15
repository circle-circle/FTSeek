#!/usr/bin/env python3
"""E2 稳健性补充: 次日开盘入场口径 + 前后窗一致性 (LONG-only 焦点变体).

主重放 (e2_swing_replay.py) 用信号日开盘入场; DEB §14.4.1 判据是"信号次一交易日开盘"。
本脚本复用日线缓存重算次日开盘入场 PnL, 并做前后半窗同号检验。
"""
import json, sqlite3, csv
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

REPO = Path("/opt/ftseek/FTSeek-US")
SNAP_DB = REPO / "data" / "signal_snapshots.db"
CACHE_DIR = REPO / "data" / "replay_bars"
OUT_DIR = REPO / "data" / "e2_swing_replay_2026-08-15"
MIN_DATE = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
FEE = 4.47
NOTIONAL = 3457.51
HOLD_DAYS = 5

# 复用主重放的独立信号去重
con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(f"""
    SELECT symbol, trade_date, direction, strength
    FROM signal_snapshots
    WHERE trade_date>='{MIN_DATE}' AND strategy_id IN ('deep_analysis','universe_expansion')
      AND fwd_ret_t5 IS NOT NULL AND direction IN ('LONG','SHORT')
""").fetchall()
con.close()
agg = defaultdict(list)
for r in rows:
    agg[(r["symbol"], r["trade_date"], r["direction"])].append(r)
signals = [{"symbol": s, "date": d, "dir": dr,
            "strength": sum(x["strength"] for x in g) / len(g)}
           for (s, d, dr), g in agg.items()]

# 载入日线缓存
daily = {}
for p in CACHE_DIR.glob("daily_*_*.json"):
    try:
        parts = p.stem.split("_")
        sym = ".".join(parts[1:-2])
        daily[sym] = json.loads(p.read_text())
    except Exception:
        pass
print(f"日线缓存载入: {len(daily)} symbols")

results = []
for s in signals:
    bars = daily.get(s["symbol"])
    if not bars:
        continue
    days = [b[0] for b in bars]
    if s["date"] not in days:
        continue
    idx = days.index(s["date"])
    # 次日开盘入场: idx+1
    if idx + 1 + HOLD_DAYS >= len(bars):
        continue  # 窗口截断
    entry_px = bars[idx + 1][1]
    exit_px = bars[idx + 1 + HOLD_DAYS][4]
    sign = 1.0 if s["dir"] == "LONG" else -1.0
    ret = (exit_px / entry_px - 1) * sign
    net = ret * NOTIONAL - FEE
    results.append({**s, "entry_date": days[idx + 1], "net": round(net, 2),
                    "ret_pct": round(ret * 100, 3)})

def stats(sub):
    n = len(sub)
    if not n:
        return None
    net = sum(r["net"] for r in sub)
    w = sum(1 for r in sub if r["net"] > 0)
    return {"n": n, "net": round(net, 1), "avg": round(net / n, 2), "wr": round(w / n * 100, 1)}

longs = [r for r in results if r["dir"] == "LONG"]
shorts = [r for r in results if r["dir"] == "SHORT"]
print(f"\n=== 次日开盘入场, 持5日 (LONG n={len(longs)}, SHORT n={len(shorts)}) ===")
print("LONG:", stats(longs))
print("SHORT:", stats(shorts))

# 前后窗一致性 (按信号日期排序对半分)
longs.sort(key=lambda r: r["date"])
mid = len(longs) // 2
w1, w2 = longs[:mid], longs[mid:]
print(f"\n=== LONG 前后窗同号检验 (前{len(w1)}笔 vs 后{len(w2)}笔) ===")
s1, s2 = stats(w1), stats(w2)
print("前半窗:", s1)
print("后半窗:", s2)
if s1 and s2:
    same = (s1["avg"] > 0) == (s2["avg"] > 0)
    print(f"→ 双窗均笔{'同号 ✓' if same else '异号 ✗'} ({s1['avg']:+.2f} vs {s2['avg']:+.2f})")

# 对比信号日开盘 vs 次日开盘 (LONG)
print("\n=== 入场口径对照 (LONG 持5日) ===")
main = json.load(open(OUT_DIR / "summary.json"))
a_sig = main["by_dir"]["LONG"]["A"]
print(f"信号日开盘入场: 均笔 {a_sig['avg']:+.2f} (n={a_sig['n']})")
sn = stats(longs)
print(f"次日开盘入场:   均笔 {sn['avg']:+.2f} (n={sn['n']})")
print(f"口径差: {sn['avg'] - a_sig['avg']:+.2f}/笔")

# 落盘
out = {"variant": "next_day_open_entry_hold5", "fee": FEE, "notional": NOTIONAL,
       "long": stats(longs), "short": stats(shorts),
       "long_w1": s1, "long_w2": s2, "per_trade": results}
(OUT_DIR / "next_day_open_variant.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print(f"\n产出: {OUT_DIR / 'next_day_open_variant.json'}")
