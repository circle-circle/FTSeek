#!/usr/bin/env python3
"""Comprehensive IC data quality audit"""
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

conn = sqlite3.connect('data/signal_snapshots.db')
conn.row_factory = sqlite3.Row

print("=" * 80)
print("IC DATA QUALITY AUDIT")
print("=" * 80)

# 1. Overall statistics
print("\n[1] OVERALL STATISTICS")
print("-" * 80)
total = conn.execute('SELECT COUNT(*) FROM signal_snapshots').fetchone()[0]
live = conn.execute("SELECT COUNT(*) FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='live'").fetchone()[0]
simulate = conn.execute("SELECT COUNT(*) FROM signal_snapshots WHERE COALESCE(trade_mode,'simulate')='simulate'").fetchone()[0]
with_t1 = conn.execute('SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL').fetchone()[0]
live_with_t1 = conn.execute("SELECT COUNT(*) FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL AND COALESCE(trade_mode,'simulate')='live'").fetchone()[0]

print(f"Total snapshots: {total}")
print(f"  - Live mode: {live} ({live/total*100:.1f}%)")
print(f"  - Simulate mode: {simulate} ({simulate/total*100:.1f}%)")
print(f"  - With T+1 returns: {with_t1} ({with_t1/total*100:.1f}%)")
print(f"  - Live + T+1 (IC input): {live_with_t1} ({live_with_t1/total*100:.1f}%)")

# 2. Date distribution
print("\n[2] DATE DISTRIBUTION (Live mode)")
print("-" * 80)
rows = conn.execute("""
    SELECT trade_date, COUNT(*) as cnt, 
        SUM(CASE WHEN fwd_ret_t1 IS NOT NULL THEN 1 ELSE 0 END) as with_t1
    FROM signal_snapshots 
    WHERE COALESCE(trade_mode,'simulate')='live'
    GROUP BY trade_date 
    ORDER BY trade_date
""").fetchall()
print(f"{'Date':<12} {'Total':>8} {'With T+1':>10} {'%':>8}")
for r in rows:
    pct = r['with_t1'] / r['cnt'] * 100 if r['cnt'] > 0 else 0
    print(f"{r['trade_date']:<12} {r['cnt']:>8} {r['with_t1']:>10} {pct:>7.1f}%")

# 3. Trade mode by date
print("\n[3] TRADE MODE BY DATE")
print("-" * 80)
rows2 = conn.execute("""
    SELECT trade_date, COALESCE(trade_mode, 'NULL') as mode, COUNT(*) as cnt
    FROM signal_snapshots 
    GROUP BY trade_date, trade_mode 
    ORDER BY trade_date, mode
""").fetchall()
print(f"{'Date':<12} {'Mode':<12} {'Count':>8}")
for r in rows2:
    print(f"{r['trade_date']:<12} {r['mode']:<12} {r['cnt']:>8}")

# 4. Realized status (backfill progress)
print("\n[4] BACKFILL PROGRESS (Realized status)")
print("-" * 80)
realized = conn.execute('SELECT realized, COUNT(*) as cnt FROM signal_snapshots GROUP BY realized ORDER BY realized').fetchall()
print(f"{'Realized':>10} {'Count':>8} {'Description':<40}")
desc_map = {
    0: 'No returns yet',
    1: 'T+1 filled',
    2: 'T+1 and T+5 filled',
    3: 'All returns filled (T+1, T+5, T+20)'
}
for r in realized:
    desc = desc_map.get(r['realized'], 'Unknown')
    print(f"{r['realized']:>10} {r['cnt']:>8} {desc:<40}")

# 5. Pending signals (not backfilled)
print("\n[5] PENDING SIGNALS (realized=0)")
print("-" * 80)
pending = conn.execute("""
    SELECT trade_date, COUNT(*) as cnt
    FROM signal_snapshots 
    WHERE realized = 0
    GROUP BY trade_date
    ORDER BY trade_date
""").fetchall()
print(f"{'Date':<12} {'Pending':>8}")
total_pending = 0
for r in pending:
    print(f"{r['trade_date']:<12} {r['cnt']:>8}")
    total_pending += r['cnt']
print(f"{'TOTAL':<12} {total_pending:>8}")

# 6. Sample pending signals
print("\n[6] SAMPLE PENDING SIGNALS (most recent)")
print("-" * 80)
recent_pending = conn.execute("""
    SELECT decision_id, symbol, trade_date, strength, direction
    FROM signal_snapshots 
    WHERE realized = 0 
    ORDER BY trade_date DESC, decision_id DESC 
    LIMIT 10
""").fetchall()
print(f"{'Decision ID':<35} {'Symbol':<12} {'Date':<12} {'Str':>5} {'Dir':<8}")
for r in recent_pending:
    print(f"{r['decision_id']:<35} {r['symbol']:<12} {r['trade_date']:<12} {r['strength']:>5} {r['direction']:<8}")

# 7. Strategy distribution (live + T+1)
print("\n[7] STRATEGY DISTRIBUTION (Live + T+1)")
print("-" * 80)
strats = conn.execute("""
    SELECT COALESCE(strategy_id, 'NULL') as strat, COUNT(*) as cnt
    FROM signal_snapshots 
    WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL 
    GROUP BY strategy_id
    ORDER BY cnt DESC
""").fetchall()
print(f"{'Strategy':<35} {'Count':>8} {'%':>8}")
for r in strats:
    pct = r['cnt'] / live_with_t1 * 100 if live_with_t1 > 0 else 0
    print(f"{r['strat']:<35} {r['cnt']:>8} {pct:>7.1f}%")

# 8. Direction distribution (live + T+1)
print("\n[8] DIRECTION DISTRIBUTION (Live + T+1)")
print("-" * 80)
dirs = conn.execute("""
    SELECT direction, COUNT(*) as cnt
    FROM signal_snapshots 
    WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL 
    GROUP BY direction
""").fetchall()
print(f"{'Direction':<12} {'Count':>8} {'%':>8}")
for r in dirs:
    pct = r['cnt'] / live_with_t1 * 100 if live_with_t1 > 0 else 0
    print(f"{r['direction']:<12} {r['cnt']:>8} {pct:>7.1f}%")

# 9. Strength distribution
print("\n[9] STRENGTH DISTRIBUTION (Live + T+1)")
print("-" * 80)
from collections import Counter
strengths = conn.execute("""
    SELECT strength 
    FROM signal_snapshots 
    WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL
""").fetchall()
s_vals = [r['strength'] for r in strengths]
if s_vals:
    print(f"Range: {min(s_vals)} - {max(s_vals)}, Mean: {sum(s_vals)/len(s_vals):.1f}, Median: {sorted(s_vals)[len(s_vals)//2]}")
    c = Counter(s_vals)
    print(f"\nTop 10 strength values:")
    print(f"{'Strength':>10} {'Count':>8} {'%':>8}")
    for val, cnt in c.most_common(10):
        pct = cnt / len(s_vals) * 100
        print(f"{val:>10} {cnt:>8} {pct:>7.1f}%")

# 10. Forward return distribution
print("\n[10] FORWARD RETURN DISTRIBUTION (Live + T+1)")
print("-" * 80)
rets = conn.execute("""
    SELECT fwd_ret_t1 
    FROM signal_snapshots 
    WHERE COALESCE(trade_mode,'simulate')='live' AND fwd_ret_t1 IS NOT NULL
""").fetchall()
r_vals = [r['fwd_ret_t1'] for r in rets]
if r_vals:
    pos = sum(1 for r in r_vals if r > 0)
    neg = sum(1 for r in r_vals if r < 0)
    zero = sum(1 for r in r_vals if r == 0)
    print(f"Positive: {pos} ({pos/len(r_vals)*100:.1f}%)")
    print(f"Negative: {neg} ({neg/len(r_vals)*100:.1f}%)")
    print(f"Zero: {zero} ({zero/len(r_vals)*100:.1f}%)")
    print(f"Range: {min(r_vals):.4f} to {max(r_vals):.4f}")
    print(f"Mean: {sum(r_vals)/len(r_vals):.4f}")
    print(f"Median: {sorted(r_vals)[len(r_vals)//2]:.4f}")

# 11. Weight version
print("\n[11] WEIGHT VERSION DISTRIBUTION")
print("-" * 80)
wv = conn.execute('SELECT COALESCE(weight_version, "NULL") as wv, COUNT(*) as cnt FROM signal_snapshots GROUP BY weight_version ORDER BY cnt DESC').fetchall()
print(f"{'Version':<12} {'Count':>8} {'%':>8}")
for r in wv:
    pct = r['cnt'] / total * 100
    print(f"{r['wv']:<12} {r['cnt']:>8} {pct:>7.1f}%")

conn.close()
print("\n" + "=" * 80)
print("AUDIT COMPLETE")
print("=" * 80)
