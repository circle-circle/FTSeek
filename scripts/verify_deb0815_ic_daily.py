"""Test mean-daily-IC (per-day Spearman averaged) vs pooled, per DEB-20260815 §13.2 targets."""
import sqlite3
from collections import defaultdict


def rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def pearson(x, y):
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    return cov / ((vx * vy) ** 0.5) if vx > 0 and vy > 0 else float("nan")


def spearman(x, y):
    return pearson(rank(x), rank(y))


con = sqlite3.connect("data/signal_snapshots.db")
q = """SELECT strategy_id, trade_date, direction, strength, fwd_ret_t1, fwd_ret_t5
FROM signal_snapshots
WHERE trade_date>='2026-06-16' AND fwd_ret_t1 IS NOT NULL AND direction IN ('LONG','SHORT') AND strength IS NOT NULL"""
rows = con.execute(q).fetchall()
layers = defaultdict(lambda: defaultdict(list))
for s, td, d, st, r1, r5 in rows:
    sign = 1 if d == "LONG" else -1
    layers[s][td].append((st, sign * r1, sign * r5 if r5 is not None else None))

TARGETS = {"deep_analysis": (0.1241, 0.1312), "universe_expansion": (-0.0175, 0.2620), "unknown": (-0.1175, -0.1301)}
for s in TARGETS:
    days = layers[s]
    for min_n, tag in [(3, "min3"), (5, "min5")]:
        d1 = [spearman([x[0] for x in v], [x[1] for x in v]) for v in days.values() if len(v) >= min_n]
        d5 = [spearman([x[0] for x in v5], [x[2] for x in v5]) for v in days.values() if len([x for x in v if x[2] is not None]) >= min_n for v5 in [[x for x in v if x[2] is not None]]]
        m1 = sum(d1) / len(d1) if d1 else float("nan")
        m5 = sum(d5) / len(d5) if d5 else float("nan")
        print("%-20s %s days=%d mean_daily_t1=%+.4f mean_daily_t5=%+.4f (target %+.4f/%+.4f)" % (
            s, tag, len(d1), m1, m5, *TARGETS[s]))
