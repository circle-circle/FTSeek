"""One-off: verify DEB-20260815 §13.2 IC layering from signal_snapshots.db (pure-python Spearman, scipy broken in venv)."""
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


def spearman(x, y):
    rx, ry = rank(x), rank(y)
    n = len(x)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / ((vx * vy) ** 0.5) if vx > 0 and vy > 0 else float("nan")


con = sqlite3.connect("data/signal_snapshots.db")
q = """SELECT strategy_id, direction, strength, fwd_ret_t1, fwd_ret_t5 FROM signal_snapshots
WHERE trade_date>='2026-06-16' AND fwd_ret_t1 IS NOT NULL AND direction IN ('LONG','SHORT') AND strength IS NOT NULL"""
rows = con.execute(q).fetchall()
print("total rows:", len(rows))
groups = defaultdict(list)
for s, d, st, r1, r5 in rows:
    sign = 1 if d == "LONG" else -1
    groups[s].append((st, sign * r1, sign * r5 if r5 is not None else None))
for s, v in sorted(groups.items()):
    ic1 = spearman([x[0] for x in v], [x[1] for x in v])
    v5 = [x for x in v if x[2] is not None]
    ic5 = spearman([x[0] for x in v5], [x[2] for x in v5]) if len(v5) > 10 else None
    print(s, "n=%d" % len(v), "IC_t1=%+.4f" % ic1, "IC_t5=%s" % ("%+.4f" % ic5 if ic5 is not None else "NA"), "n5=%d" % len(v5))
