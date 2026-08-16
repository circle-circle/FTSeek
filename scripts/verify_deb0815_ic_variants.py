"""Variant matrix: which IC recipe reproduces DEB-20260815 §13.2 numbers?"""
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
q = """SELECT strategy_id, direction, strength, fwd_ret_t1, fwd_ret_t5, alpha_t1, alpha_t5
FROM signal_snapshots
WHERE trade_date>='2026-06-16' AND fwd_ret_t1 IS NOT NULL AND direction IN ('LONG','SHORT') AND strength IS NOT NULL"""
rows = con.execute(q).fetchall()
groups = defaultdict(list)
for s, d, st, r1, r5, a1, a5 in rows:
    sign = 1 if d == "LONG" else -1
    groups[s].append(dict(st=st, dr1=sign * r1, dr5=sign * r5 if r5 is not None else None,
                          da1=sign * a1 if a1 is not None else None, da5=sign * a5 if a5 is not None else None,
                          r1=r1, r5=r5))

TARGETS = {"deep_analysis": (0.1241, 0.1312), "universe_expansion": (-0.0175, 0.2620), "unknown": (-0.1175, -0.1301)}
for s in ["deep_analysis", "universe_expansion", "unknown"]:
    v = groups[s]
    print("=== %s n=%d (target t1=%+.4f t5=%+.4f) ===" % (s, len(v), *TARGETS[s]))
    variants = {
        "spearman_dir_fwd": ("st", "dr1", "dr5", spearman),
        "pearson_dir_fwd": ("st", "dr1", "dr5", pearson),
        "spearman_dir_alpha": ("st", "da1", "da5", spearman),
        "pearson_dir_alpha": ("st", "da1", "da5", pearson),
        "spearman_raw_fwd_nodir": ("st", "r1", "r5", spearman),
        "pearson_raw_fwd_nodir": ("st", "r1", "r5", pearson),
    }
    for name, (kx, k1, k5, fn) in variants.items():
        v1 = [(d[kx], d[k1]) for d in v if d[k1] is not None]
        v5 = [(d[kx], d[k5]) for d in v if d[k5] is not None]
        ic1 = fn([a for a, _ in v1], [b for _, b in v1]) if len(v1) > 10 else None
        ic5 = fn([a for a, _ in v5], [b for _, b in v5]) if len(v5) > 10 else None
        print("  %-24s t1=%s t5=%s" % (name,
              "%+.4f" % ic1 if ic1 is not None else "NA",
              "%+.4f" % ic5 if ic5 is not None else "NA"))
    # strength cardinality
    print("  strength distinct:", len(set(d["st"] for d in v)))
