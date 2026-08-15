#!/usr/bin/env python3
"""DEB-20260815 §13.2 IC 数值仲裁脚本 (Hermes 落盘, 2026-08-15).

背景: Qoder §十四 称无法逐字复现 §13.2 的 IC 数值 (DA t5: Hermes +0.1312 vs Qoder +0.0724)。
本脚本为规范仲裁实现, 裁决两件事:
  A. 方向处理: DB 的 fwd_ret_t1/t5 是否已方向化? 若是, 计算 IC 时不得再乘方向符号。
     (§13.1 已验证 100% 方向化; get_ic_summary 代码注释 2026-07-19 亦明言不可双重调整)
  B. tie 处理: Spearman 对同值 strength 必须用平均秩 (标准定义)。
     §13.2 原计算用唯一秩 (tie 任意拆分), 属实现瑕疵, 本脚本修正并报告规范值。

口径 (§13.2 行集逐字对齐):
  trade_date >= now-60d, fwd_ret_t1 NOT NULL, strength NOT NULL,
  direction IN (LONG, SHORT) (排除 NEUTRAL), strategy_id 排除 disabled 名单。
IC = spearman(raw_strength, fwd_ret), fwd_ret 为 DB 存储值 (已方向化)。
同时输出 Qoder 式双重调整口径 (sign*fwd_ret) 作对照, 证明分歧来源。
"""
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

DB = "data/signal_snapshots.db"
DISABLED = ["BATTLE_PLAN_FAST_PATH", "EARNINGS_REVISION_MOMENTUM",
            "TECHNICAL_MULTI_FACTOR", "trend_first", "voting_llm"]


def rank_avg(vals):
    """标准 Spearman 秩: 同值取平均秩."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def rank_unique(vals):
    """§13.2 原实现: 同值按稳定排序取唯一秩 (非标准, tie 任意拆分)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    for p, idx in enumerate(order):
        r[idx] = float(p + 1)
    return r


def corr_from_ranks(rx, ry):
    n = len(rx)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / ((vx * vy) ** 0.5) if vx > 0 and vy > 0 else float("nan")


def spearman(x, y, tie="avg"):
    rf = rank_avg if tie == "avg" else rank_unique
    return corr_from_ranks(rf(x), rf(y))


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    ph = ",".join(f"'{s}'" for s in DISABLED)

    # 方向化自检 (A): fwd_ret 与价格方向一致性
    chk = con.execute("""
        SELECT direction,
          SUM(CASE WHEN (close_t1/close_t0-1)*fwd_ret_t1 > 0 THEN 1 ELSE 0 END) same,
          COUNT(*) n FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL AND close_t0>0 AND close_t1>0
        GROUP BY direction""").fetchall()
    print("=== A. fwd_ret 方向化自检 (LONG应同向/SHORT应反向) ===")
    for r in chk:
        ok = (r["direction"] == "LONG" and r["same"] == r["n"]) or \
             (r["direction"] == "SHORT" and r["same"] == 0)
        print(f"  {r['direction']:8s} n={r['n']:5d} 同向{r['same']:5d} → {'已方向化✓' if ok else '异常✗'}")

    rows = con.execute(f"""
        SELECT strategy_id, direction, strength, fwd_ret_t1, fwd_ret_t5, trade_date, symbol
        FROM signal_snapshots
        WHERE trade_date>='{cutoff}' AND fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL
          AND direction IN ('LONG','SHORT') AND strategy_id NOT IN ({ph})
    """).fetchall()
    print(f"\n行集: n={len(rows)} (cutoff={cutoff}, 排除disabled+NEUTRAL)")

    layers = defaultdict(list)
    for r in rows:
        layers[r["strategy_id"]].append(r)

    print("\n=== B. IC 仲裁: raw strength vs DB fwd_ret (规范口径, 不双重调整) ===")
    print(f"{'layer':26s} {'n':>5s} {'IC_t1_avgTie':>13s} {'IC_t1_uniqTie':>14s} "
          f"{'IC_t5_avgTie':>13s} {'IC_t5_uniqTie':>14s}")
    results = {}
    for s in sorted(layers, key=lambda k: -len(layers[k])):
        sub = layers[s]
        if len(sub) < 30:
            continue
        st = [float(r["strength"]) for r in sub]
        f1 = [float(r["fwd_ret_t1"]) for r in sub]
        sub5 = [r for r in sub if r["fwd_ret_t5"] is not None]
        st5 = [float(r["strength"]) for r in sub5]
        f5 = [float(r["fwd_ret_t5"]) for r in sub5]
        r1a = spearman(st, f1, "avg"); r1u = spearman(st, f1, "unique")
        r5a = spearman(st5, f5, "avg") if len(sub5) > 10 else None
        r5u = spearman(st5, f5, "unique") if len(sub5) > 10 else None
        results[s] = (len(sub), r1a, r1u, r5a, r5u, len(sub5))
        print(f"{s:26s} {len(sub):>5d} {r1a:>+13.4f} {r1u:>+14.4f} "
              f"{('%+.4f' % r5a) if r5a is not None else 'NA':>13s} "
              f"{('%+.4f' % r5u) if r5u is not None else 'NA':>14s}")

    print("\n=== C. 对照: Qoder 式双重调整 (sign*fwd_ret, 对已方向化数据=错误) ===")
    for s in sorted(layers, key=lambda k: -len(layers[k])):
        sub = layers[s]
        if len(sub) < 30:
            continue
        st = [float(r["strength"]) for r in sub]
        f1d = [float(r["fwd_ret_t1"]) * (1 if r["direction"] == "LONG" else -1) for r in sub]
        sub5 = [r for r in sub if r["fwd_ret_t5"] is not None]
        st5 = [float(r["strength"]) for r in sub5]
        f5d = [float(r["fwd_ret_t5"]) * (1 if r["direction"] == "LONG" else -1) for r in sub5]
        r1 = spearman(st, f1d, "avg")
        r5 = spearman(st5, f5d, "avg") if len(sub5) > 10 else None
        print(f"{s:26s} IC_t1={r1:+.4f} IC_t5={('%+.4f' % r5) if r5 is not None else 'NA'}")

    # 独立样本去重 (trade_date, symbol, direction)
    print("\n=== D. 独立样本去重 IC (规范口径) ===")
    for s in ("deep_analysis", "universe_expansion", "unknown"):
        if s not in layers:
            continue
        m = {}
        for r in layers[s]:
            k = (r["trade_date"], r["symbol"], r["direction"])
            m.setdefault(k, []).append((float(r["strength"]), float(r["fwd_ret_t1"])))
        i_s = [sum(x[0] for x in v) / len(v) for v in m.values()]
        i_f = [v[0][1] for v in m.values()]
        print(f"  {s:26s} n_indep={len(m)} IC_t1={spearman(i_s, i_f):+.4f}")

    print("\n=== 结论 (脚本自输出) ===")
    da = results.get("deep_analysis")
    if da:
        print(f"deep_analysis 规范值: IC_t1={da[1]:+.4f} (平均秩), IC_t5={('%+.4f' % da[3]) if da[3] else 'NA'}")
        print("§13.2 原报 +0.1241/+0.1312 为唯一秩口径; 规范口径以本行为准。")


if __name__ == "__main__":
    main()
