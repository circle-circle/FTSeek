#!/usr/bin/env python3
"""Audit IC data quality in signal_snapshots.db"""
import sqlite3
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

conn = sqlite3.connect('data/signal_snapshots.db')
conn.row_factory = sqlite3.Row

total = conn.execute('SELECT COUNT(*) FROM signal_snapshots').fetchone()[0]
live = conn.execute("SELECT COUNT(*) FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live'").fetchone()[0]
with_t1 = conn.execute('SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL').fetchone()[0]
live_with_t1 = conn.execute("SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL AND COALESCE(trade_mode,'simulate')='live'").fetchone()[0]

print(f'Total snapshots: {total}')
print(f'Live mode: {live}')
print(f'With T+1 returns: {with_t1}')
print(f'Live + T+1: {live_with_t1}')

realized = conn.execute('SELECT realized, COUNT(*) FROM signal_snapshots GROUP BY realized').fetchall()
print(f'Realized distribution: {dict(realized)}')

strats = conn.execute("SELECT strategy_id, COUNT(*) FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL GROUP BY strategy_id").fetchall()
print(f'Strategy dist (live+t1): {dict(strats)}')

dirs = conn.execute("SELECT direction, COUNT(*) FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL GROUP BY direction").fetchall()
print(f'Direction dist (live+t1): {dict(dirs)}')

dates = conn.execute("SELECT MIN(trade_date), MAX(trade_date) FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL").fetchone()
print(f'Date range (live+t1): {dates[0]} ~ {dates[1]}')

strengths = conn.execute("SELECT strength FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL").fetchall()
s_vals = [r[0] for r in strengths]
if s_vals:
    print(f'Strength range: {min(s_vals)}-{max(s_vals)}, mean={sum(s_vals)/len(s_vals):.1f}')
    c = Counter(s_vals)
    print(f'Strength Top10: {c.most_common(10)}')

rets = conn.execute("SELECT fwd_ret_t1 FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL").fetchall()
r_vals = [r[0] for r in rets]
if r_vals:
    pos = sum(1 for r in r_vals if r > 0)
    neg = sum(1 for r in r_vals if r < 0)
    zero = sum(1 for r in r_vals if r == 0)
    print(f'fwd_ret_t1: pos={pos} neg={neg} zero={zero}, range={min(r_vals):.4f}~{max(r_vals):.4f}')

wv = conn.execute('SELECT weight_version, COUNT(*) FROM signal_snapshots GROUP BY weight_version').fetchall()
print(f'Weight version: {dict(wv)}')

# Check pending (not backfilled)
pending = conn.execute('SELECT COUNT(*) FROM signal_snapshots WHERE realized = 0').fetchone()[0]
print(f'Pending (not backfilled): {pending}')

# Recent pending samples
recent_pending = conn.execute("SELECT decision_id, symbol, trade_date FROM signal_snapshots WHERE realized = 0 ORDER BY trade_date DESC LIMIT 5").fetchall()
print(f'Recent pending samples:')
for r in recent_pending:
    print(f'  {r["decision_id"]} {r["symbol"]} {r["trade_date"]}')

# IC computation check
from scipy.stats import spearmanr
import math

rows = conn.execute("""
    SELECT strength, fwd_ret_t1, strategy_id, direction, trade_date
    FROM signal_snapshots
    WHERE fwd_ret_t1 IS NOT NULL
      AND strength IS NOT NULL
      AND COALESCE(trade_mode, 'simulate') = 'live'
    ORDER BY trade_date DESC
""").fetchall()

clean = [r for r in rows
         if r["strength"] is not None
         and r["fwd_ret_t1"] is not None
         and not math.isnan(float(r["strength"]))
         and not math.isnan(float(r["fwd_ret_t1"]))]

print(f'\nIC computation input: {len(clean)} clean rows (from {len(rows)} total)')
if len(clean) >= 30:
    strengths = [r["strength"] for r in clean]
    fwd_rets = [r["fwd_ret_t1"] for r in clean]
    ic, pval = spearmanr(strengths, fwd_rets)
    print(f'Global IC = {ic:.4f}, p = {pval:.4f}')
    
    # By direction
    for d in ['LONG', 'SHORT']:
        subset = [r for r in clean if r["direction"] == d]
        if len(subset) >= 5:
            s = [r["strength"] for r in subset]
            f = [r["fwd_ret_t1"] for r in subset]
            dic, dp = spearmanr(s, f)
            print(f'  {d}: IC={dic:.4f}, p={dp:.4f}, n={len(subset)}')
    
    # By strategy
    from collections import defaultdict
    by_strat = defaultdict(list)
    for r in clean:
        by_strat[r["strategy_id"] or "unknown"].append(r)
    for sid, group in by_strat.items():
        if len(group) >= 5:
            s = [r["strength"] for r in group]
            f = [r["fwd_ret_t1"] for r in group]
            dic, dp = spearmanr(s, f)
            print(f'  Strategy {sid}: IC={dic:.4f}, p={dp:.4f}, n={len(group)}')
    
    # Daily IC
    by_date = defaultdict(list)
    for r in clean:
        by_date[r["trade_date"]].append(r)
    daily_ics = []
    for date, day_rows in sorted(by_date.items()):
        if len(day_rows) >= 3:
            s = [r["strength"] for r in day_rows]
            f = [r["fwd_ret_t1"] for r in day_rows]
            try:
                dic, _ = spearmanr(s, f)
                daily_ics.append((date, dic, len(day_rows)))
            except:
                pass
    print(f'\nDaily IC ({len(daily_ics)} days):')
    for date, dic, n in daily_ics:
        print(f'  {date}: IC={dic:.4f} (n={n})')

conn.close()
