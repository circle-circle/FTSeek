#!/usr/bin/env python3
"""
Signal Performance Analysis — runs all 5 queries against signal_snapshots.db
"""
import sqlite3
import json
from collections import defaultdict
from pathlib import Path

DB_PATH = Path("/opt/ftseek/FTSeek-US/data/signal_snapshots.db")

try:
    from scipy.stats import spearmanr
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    print("⚠ scipy not available; IC calculations will be approximate (Pearson-based or manual)")

import math

def spearman_rank(x, y):
    """Compute Spearman rank correlation. Returns (rho, pval)."""
    if HAS_SCIPY:
        return spearmanr(x, y)
    # Manual fallback: rank both arrays, then compute Pearson on ranks
    n = len(x)
    if n < 3:
        return (float('nan'), 1.0)
    # Rank
    def rankify(arr):
        sorted_idx = sorted(range(n), key=lambda i: arr[i])
        ranks = [0] * n
        for rank, idx in enumerate(sorted_idx):
            ranks[idx] = rank + 1
        # Handle ties: average ranks
        i = 0
        while i < n:
            j = i
            while j < n and arr[sorted_idx[j]] == arr[sorted_idx[i]]:
                j += 1
            if j > i + 1:
                avg_rank = sum(range(i+1, j+1)) / (j - i)
                for k in range(i, j):
                    ranks[sorted_idx[k]] = avg_rank
            i = j
        return ranks
    rx = rankify(x)
    ry = rankify(y)
    # Pearson on ranks
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = math.sqrt(sum((rx[i]-mx)**2 for i in range(n)) * sum((ry[i]-my)**2 for i in range(n)))
    if den == 0:
        return (float('nan'), 1.0)
    rho = num / den
    # Simple t-test
    if abs(rho) >= 1.0:
        pval = 0.0
    else:
        t = rho * math.sqrt((n - 2) / (1 - rho * rho))
        # Approximate p-value with normal distribution
        pval = 2 * (1 - _norm_cdf(abs(t)))
    return (rho, pval)

def _norm_cdf(x):
    """Abramowitz and Stegun approximation for normal CDF."""
    import math
    if x < 0:
        return 1 - _norm_cdf(-x)
    b0, b1, b2, b3, b4, b5 = 0.2316419, 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
    t = 1.0 / (1.0 + b0 * x)
    phi = (1.0 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2.0)
    return 1.0 - phi * (b1*t + b2*t*t + b3*t*t*t + b4*t*t*t*t + b5*t*t*t*t*t)

def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Get all rows with forward returns
    rows = conn.execute("""
        SELECT *
        FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL
          AND strength IS NOT NULL
        ORDER BY trade_date ASC, created_at ASC
    """).fetchall()
    print(f"Total rows with valid strength & fwd_ret_t1: {len(rows)}")

    # Convert to list of dicts for easier use
    data = [dict(r) for r in rows]

    # ================================================================
    # 1) IC BY DAY — is it improving or degrading?
    # ================================================================
    print("\n" + "=" * 72)
    print("1) IC BY DAY (Spearman rank correlation: strength vs fwd_ret_t1)")
    print("=" * 72)

    by_date = defaultdict(list)
    for r in data:
        by_date[r['trade_date']].append(r)

    daily_ics = []
    print(f"{'Date':<12} {'N':>5} {'IC(T1)':>8} {'P-value':>8} {'IC(T5)':>8} {'IC(T20)':>8}")
    print("-" * 60)
    for td in sorted(by_date.keys()):
        day_data = by_date[td]
        n = len(day_data)
        strength = [r['strength'] for r in day_data]
        fwd_t1 = [r['fwd_ret_t1'] for r in day_data]
        fwd_t5 = [r['fwd_ret_t5'] for r in day_data if r['fwd_ret_t5'] is not None]
        fwd_t20 = [r['fwd_ret_t20'] for r in day_data if r['fwd_ret_t20'] is not None]

        ic_t1, pv_t1 = spearman_rank(strength, fwd_t1)

        # T5: need matching strengths
        t5_data = [r for r in day_data if r['fwd_ret_t5'] is not None]
        ic_t5 = float('nan')
        if len(t5_data) >= 3:
            ic_t5, _ = spearman_rank([r['strength'] for r in t5_data], [r['fwd_ret_t5'] for r in t5_data])

        t20_data = [r for r in day_data if r['fwd_ret_t20'] is not None]
        ic_t20 = float('nan')
        if len(t20_data) >= 3:
            ic_t20, _ = spearman_rank([r['strength'] for r in t20_data], [r['fwd_ret_t20'] for r in t20_data])

        ic_str_t1 = f"{ic_t1:.4f}" if not math.isnan(ic_t1) else "N/A"
        ic_str_t5 = f"{ic_t5:.4f}" if not math.isnan(ic_t5) else "N/A"
        ic_str_t20 = f"{ic_t20:.4f}" if not math.isnan(ic_t20) else "N/A"
        pv_str = f"{pv_t1:.4f}" if pv_t1 is not None else "N/A"

        print(f"{td:<12} {n:>5} {ic_str_t1:>8} {pv_str:>8} {ic_str_t5:>8} {ic_str_t20:>8}")
        if n >= 3:
            daily_ics.append((td, ic_t1, n))

    # Trend analysis
    valid_ics = [(d, ic, n) for d, ic, n in daily_ics if not math.isnan(ic)]
    if len(valid_ics) >= 3:
        dates_idx = list(range(len(valid_ics)))
        ics = [ic for _, ic, _ in valid_ics]
        trend_ic, trend_pv = spearman_rank(dates_idx, ics)
        print(f"\nIC TREND (correlation of daily IC vs day index): {trend_ic:.4f} (p={trend_pv:.4f})")
        if trend_ic > 0.1:
            print("→ IC is IMPROVING over time")
        elif trend_ic < -0.1:
            print("→ IC is DEGRADING over time")
        else:
            print("→ IC is FLAT (no clear trend)")
        print(f"  IC > 0 days: {sum(1 for ic in ics if ic > 0)}/{len(ics)} = {sum(1 for ic in ics if ic > 0)/len(ics):.1%}")

    # ================================================================
    # 2) IC BY DIRECTION (LONG vs SHORT)
    # ================================================================
    print("\n" + "=" * 72)
    print("2) IC BY DIRECTION (LONG vs SHORT)")
    print("=" * 72)

    for direction in ['LONG', 'SHORT']:
        dir_data = [r for r in data if r['direction'] == direction]
        if len(dir_data) < 5:
            print(f"  {direction}: insufficient data (n={len(dir_data)})")
            continue
        strength = [r['strength'] for r in dir_data]
        fwd_t1 = [r['fwd_ret_t1'] for r in dir_data]
        ic, pv = spearman_rank(strength, fwd_t1)
        print(f"  {direction}: n={len(dir_data):>4}, IC(T1)={ic:.4f}, p={pv:.4f}")

        # Also compute mean forward returns by direction
        avg_fwd = sum(fwd_t1) / len(fwd_t1)
        print(f"           mean fwd_ret_t1 = {avg_fwd:.6f}")

    # ================================================================
    # 3) IC BY STRATEGY
    # ================================================================
    print("\n" + "=" * 72)
    print("3) IC BY STRATEGY")
    print("=" * 72)

    by_strategy = defaultdict(list)
    for r in data:
        sid = r['strategy_id'] or 'unknown'
        by_strategy[sid].append(r)

    for sid in sorted(by_strategy.keys()):
        strat_data = by_strategy[sid]
        if len(strat_data) < 5:
            print(f"  {sid}: insufficient data (n={len(strat_data)})")
            continue
        strength = [r['strength'] for r in strat_data]
        fwd_t1 = [r['fwd_ret_t1'] for r in strat_data]
        ic, pv = spearman_rank(strength, fwd_t1)
        avg_fwd = sum(fwd_t1) / len(fwd_t1)
        print(f"  {sid}: n={len(strat_data):>4}, IC(T1)={ic:.4f}, p={pv:.4f}, avg_fwd={avg_fwd:.6f}")

    # ================================================================
    # 4) DISTRIBUTION OF SIGNAL STRENGTHS vs ACTUAL RETURNS
    # ================================================================
    print("\n" + "=" * 72)
    print("4) DISTRIBUTION OF SIGNAL STRENGTHS vs ACTUAL RETURNS")
    print("=" * 72)

    # Bucket by strength decile
    strength_buckets = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
                         (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]

    print(f"{'Bucket':<15} {'N':>5} {'Mean_Strength':>14} {'Mean_Fwd_T1':>14} {'Median_Fwd_T1':>14} {'Std_Fwd_T1':>12}")
    print("-" * 75)

    for lo, hi in strength_buckets:
        bucket = [r for r in data if lo <= r['strength'] < hi]
        if not bucket:
            continue
        n = len(bucket)
        strengths = [r['strength'] for r in bucket]
        fwds = [r['fwd_ret_t1'] for r in bucket]
        mean_s = sum(strengths) / n
        mean_f = sum(fwds) / n
        fwds_sorted = sorted(fwds)
        median_f = fwds_sorted[n // 2] if n % 2 else (fwds_sorted[n//2-1] + fwds_sorted[n//2]) / 2
        std_f = math.sqrt(sum((f - mean_f)**2 for f in fwds) / n)
        print(f"[{lo:>3}-{hi:<4})  {n:>5}  {mean_s:>13.1f}  {mean_f:>13.6f}  {median_f:>13.6f}  {std_f:>11.6f}")

    # ================================================================
    # 5) WIN RATE BY STRENGTH BUCKET
    # ================================================================
    print("\n" + "=" * 72)
    print("5) WIN RATE BY STRENGTH BUCKET")
    print("=" * 72)
    print("  (Win = direction-adjusted forward return > 0)")
    print(f"{'Bucket':<15} {'N':>5} {'Wins':>5} {'Losses':>7} {'WinRate':>8}")
    print("-" * 50)

    for lo, hi in strength_buckets:
        bucket = [r for r in data if lo <= r['strength'] < hi]
        if not bucket:
            continue
        n = len(bucket)
        wins = sum(1 for r in bucket if r['fwd_ret_t1'] > 0)
        losses = n - wins
        wr = wins / n
        print(f"[{lo:>3}-{hi:<4})  {n:>5}  {wins:>5}  {losses:>7}  {wr:>7.1%}")

    # Compute overall win rate and mean return
    all_wins = sum(1 for r in data if r['fwd_ret_t1'] > 0)
    all_losses = len(data) - all_wins
    avg_ret = sum(r['fwd_ret_t1'] for r in data) / len(data)
    print(f"\nOVERALL: n={len(data)}, wins={all_wins}, losses={all_losses}, "
          f"win_rate={all_wins/len(data):.2%}, mean_fwd_ret={avg_ret:.6f}")

    # Bonus: overall IC
    all_strength = [r['strength'] for r in data]
    all_fwd = [r['fwd_ret_t1'] for r in data]
    overall_ic, overall_pv = spearman_rank(all_strength, all_fwd)
    print(f"OVERALL IC(T1): {overall_ic:.4f} (p={overall_pv:.4f})")

    conn.close()

if __name__ == "__main__":
    main()