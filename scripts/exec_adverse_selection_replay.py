#!/usr/bin/env python3.11
"""[DEB-20260924 F11] 执行逆向选择 + 基准统一 只读回放 (T1/T2/T3 三件套).

零副作用: 只读 DB (mode=ro) + 只读 data/shadow/exec_gate_audit.jsonl。
无 cron / 无消费者 / 不改 config / 不写任何文件 (--out 除外)。

预注册判据 (见数前写死于 DEB-20260924 §八 F11, 2026-09-24 深夜):
  T1 主检验 = 事件级两臂 (BLOCKED=skipped_chase_cap / EXEC=executed),
     结局 = close₀/决策价 − 1 (close₀ 由 anchor×(1+fwd_return_intraday) 反签名还原, BUY sign=+1);
     四条全达才算「逆向选择成立」: (a) 日聚类 CI95 不含 0 且为正 (b) 双半窗同号
     (c) 两臂事件日各 ≥6 (d) 留一日符号稳健。任一不达 ⇒ 「无逆向选择证据」并关闭该轴。
  T1 解释不对称 (预注册): BLOCKED 决策价=追高被拒价(局部高价) ⇒ 回复偏倚向下压 BLOCKED 收益;
     「BLOCKED<EXEC」不可分辨偏倚与门价值 ⇒ 反向读数不得用于论证加强 chase_cap。
  T2 探索性: EXEC 臂内成交基差 vs 净$/close₀收益 Spearman — 无论方向与显著性均不触发动作。
  T3 = F9①② 合并: 同日配对 (PULLBACK−BREAKOUT) 于 fwd_return_intraday_open (开盘统一基准),
     判据同 T1 四条; 配对日 <6 直接记「不可达」。旧锚基准 (§三.6 +2.60pp) 仅对照列示, 禁混算。

口径契约 (ic_tracker 写侧, 2026-09-24 核): fwd_return_* 均为已签名收益 (BUY:+1);
  反签名还原 close₀ = entry_price × (1 + fwd_return_intraday) 是文档认可的唯一合法还原式。

用法:
  PYTHONPATH=. ./venv/bin/python3.11 scripts/exec_adverse_selection_replay.py \
      [--since 2026-06-01] [--audit-since 2026-07-17] [--until 2026-09-24] [--out data/exec_adverse_selection_<date>.json]
"""
import argparse
import collections
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
AUDIT = ROOT / "data" / "shadow" / "exec_gate_audit.jsonl"
SEED = 20260924
B = 2000

# 与 scripts/premarket_index_regime_replay.py archetype() 同语义 (R4 修复后口径, 两处同步维护):
# 结构化 entry_archetype 优先 → 模板前缀 → 关键词兜底。
_ARCH_PULL = ("低吸", "回踩", "承接", "企稳接", "分批接")
_ARCH_BREAK = ("突破入场", "突破追入", "追入", "站上", "breakout")


def archetype(row) -> str:
    ea = str(row.get("entry_archetype") or "").upper()
    if ea == "PULLBACK_LIMIT":
        return "PULLBACK"
    if ea == "BREAKOUT":
        return "BREAKOUT"
    txt = str(row.get("thesis") or "")
    if txt.startswith("突破入场"):
        return "BREAKOUT"
    if txt.startswith("趋势回调"):
        return "PULLBACK"
    if any(k in txt for k in _ARCH_BREAK):
        return "BREAKOUT"
    if any(k in txt for k in _ARCH_PULL):
        return "PULLBACK"
    return "OTHER"


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None, 0

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    a = rank([p[0] for p in pairs])
    b = rank([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return (num / den if den else None), n


def day_cluster_boot_diff(rows_a, rows_b, key, b=B, seed=SEED):
    """两臂日聚类 bootstrap 均值差 (a−b): 重抽单位=交易日, 返回 (diff, lo, hi, p)."""
    rnd = random.Random(seed)
    days_a = collections.defaultdict(list)
    days_b = collections.defaultdict(list)
    for r in rows_a:
        if r.get(key) is not None:
            days_a[r["date"]].append(r[key])
    for r in rows_b:
        if r.get(key) is not None:
            days_b[r["date"]].append(r[key])
    all_days = sorted(set(days_a) | set(days_b))
    if len(all_days) < 3:
        return None
    point = _mean([v for d in days_a for v in days_a[d]])
    point_b = _mean([v for d in days_b for v in days_b[d]])
    if point is None or point_b is None:
        return None
    diff = point - point_b
    diffs = []
    for _ in range(b):
        sa = []
        sb = []
        for _i in range(len(all_days)):
            d = rnd.choice(all_days)
            sa.extend(days_a.get(d, []))
            sb.extend(days_b.get(d, []))
        if sa and sb:
            diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    if not diffs:
        return None
    diffs.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[min(int(0.975 * len(diffs)), len(diffs) - 1)]
    p = 2 * min(sum(1 for x in diffs if x <= 0), sum(1 for x in diffs if x >= 0)) / len(diffs)
    return {"diff": round(diff, 5), "ci95": [round(lo, 5), round(hi, 5)],
            "p": round(min(p, 1.0), 4), "days_a": len(days_a), "days_b": len(days_b),
            "n_a": sum(len(v) for v in days_a.values()), "n_b": sum(len(v) for v in days_b.values())}


def day_cluster_boot_mean(rows, key, b=B, seed=SEED):
    """单臂日聚类 bootstrap 均值 (vs 0)."""
    rnd = random.Random(seed)
    by_day = collections.defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by_day[r["date"]].append(r[key])
    days = sorted(by_day)
    if not days:
        return None
    vals = [v for d in days for v in by_day[d]]
    point = sum(vals) / len(vals)
    means = []
    for _ in range(b):
        s = []
        for _i in range(len(days)):
            s.extend(by_day[rnd.choice(days)])
        means.append(sum(s) / len(s))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(int(0.975 * len(means)), len(means) - 1)]
    p = 2 * min(sum(1 for x in means if x <= 0), sum(1 for x in means if x >= 0)) / len(means)
    return {"mean": round(point, 5), "ci95": [round(lo, 5), round(hi, 5)],
            "p": round(min(p, 1.0), 4), "days": len(days), "n": len(vals)}


def leave_one_day_sign(rows_a, rows_b, key):
    """留一日稳健性: 逐日剔除重算点估计差, 返回 (全样本符号, 是否全部保持)."""
    days = sorted({r["date"] for r in rows_a if r.get(key) is not None}
                  | {r["date"] for r in rows_b if r.get(key) is not None})
    full = _mean([r[key] for r in rows_a if r.get(key) is not None])
    full_b = _mean([r[key] for r in rows_b if r.get(key) is not None])
    if full is None or full_b is None:
        return None, False, []
    d_full = full - full_b
    sign = 1 if d_full > 0 else -1
    worst = []
    for d in days:
        a = _mean([r[key] for r in rows_a if r.get(key) is not None and r["date"] != d])
        bb = _mean([r[key] for r in rows_b if r.get(key) is not None and r["date"] != d])
        if a is not None and bb is not None:
            worst.append((round(a - bb, 5), d))
    stable = all((x * sign) > 0 for x, _ in worst) if worst else False
    return sign, stable, sorted(worst)[:2] + sorted(worst)[-2:]


def half_windows(rows_a, rows_b, key):
    days = sorted({r["date"] for r in rows_a} | {r["date"] for r in rows_b})
    if len(days) < 4:
        return None, None
    mid = len(days) // 2
    out = []
    for half in (days[:mid], days[mid:]):
        hs = set(half)
        a = _mean([r[key] for r in rows_a if r.get(key) is not None and r["date"] in hs])
        b_ = _mean([r[key] for r in rows_b if r.get(key) is not None and r["date"] in hs])
        out.append(round(a - b_, 5) if (a is not None and b_ is not None) else None)
    return out[0], out[1]


def load(since, until):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    inst = {}
    rows = []
    for r in c.execute(
            """select id, date, symbol, decision_id, entry_price, execution_price,
                      fwd_return_intraday, fwd_return_intraday_open, fwd_return_t1,
                      thesis, raw_instruction_json
               from playbook_instructions
               where direction='BUY' and date>=? and date<=?""", (since, until)):
        d = dict(r)
        try:
            raw = json.loads(d.pop("raw_instruction_json") or "{}") or {}
        except Exception:
            raw = {}
        d["entry_archetype"] = raw.get("entry_archetype")
        # 反签名还原价位 (ic_tracker 契约认可式; BUY 全体 sign=+1)
        d["close0"] = (d["entry_price"] * (1 + d["fwd_return_intraday"])
                       if d.get("fwd_return_intraday") is not None and d.get("entry_price") else None)
        d["close_t1"] = (d["entry_price"] * (1 + d["fwd_return_t1"])
                         if d.get("fwd_return_t1") is not None and d.get("entry_price") else None)
        inst[d["id"]] = d
        rows.append(d)
    # 净$ SUM 聚合 (R4-C 同款)
    tr = {}
    for r in c.execute("""select decision_id, pnl_net, fees_est from trade_records
                          where decision_id is not null and decision_id!=''"""):
        t = tr.setdefault(r["decision_id"], {"pnl_net": None, "fees_est": None, "fills": 0})
        t["fills"] += 1
        for k in ("pnl_net", "fees_est"):
            if r[k] is not None:
                t[k] = (t[k] or 0.0) + float(r[k])
    for d in rows:
        t = tr.get(d.get("decision_id") or "", {})
        d["pnl_net"] = t.get("pnl_net")
    c.close()
    return rows, inst


def load_events(audit_since, until, inst):
    blocked, executed = [], []
    n_rows = n_join_miss = 0
    for line in open(AUDIT, encoding="utf-8"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("date", "") < audit_since or e.get("date", "") > until:
            continue
        act = e.get("action")
        if act not in ("skipped_chase_cap", "executed"):
            continue
        n_rows += 1
        i = inst.get(e.get("inst_id"))
        if i is None or i.get("close0") is None or not e.get("entry"):
            n_join_miss += 1
            continue
        px = float(e["entry"])
        ev = {"date": e["date"], "symbol": e.get("symbol"), "inst_id": e.get("inst_id"),
              "ts": e.get("ts"), "event_price": px,
              "r_close": i["close0"] / px - 1.0,
              "r_t1": (i["close_t1"] / px - 1.0) if i.get("close_t1") else None}
        (executed if act == "executed" else blocked).append(ev)
    return blocked, executed, n_rows, n_join_miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01")
    ap.add_argument("--audit-since", default="2026-07-17")
    ap.add_argument("--until", default="2026-09-24")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows, inst = load(args.since, args.until)
    blocked, executed, n_rows, n_join_miss = load_events(args.audit_since, args.until, inst)

    print("== 完整性哨兵 ==")
    print(f"指令母体 BUY {args.since}..{args.until}: {len(rows)} 条; "
          f"fwd_intraday 非空 {sum(1 for r in rows if r.get('fwd_return_intraday') is not None)}; "
          f"fwd_open 非空 {sum(1 for r in rows if r.get('fwd_return_intraday_open') is not None)}")
    print(f"审计事件 {args.audit_since}..{args.until}: 两 action 共 {n_rows} 行, "
          f"不可关联/缺 close0 {n_join_miss}; BLOCKED 事件 {len(blocked)} / EXEC 事件 {len(executed)}")
    b_days = len({e['date'] for e in blocked})
    e_days = len({e['date'] for e in executed})
    print(f"事件覆盖交易日: BLOCKED {b_days} / EXEC {e_days}")

    out = {"window": [args.since, args.until], "audit_window": [args.audit_since, args.until],
           "universe_n": len(rows), "blocked_events": len(blocked), "executed_events": len(executed),
           "blocked_days": b_days, "exec_days": e_days}

    # ── T1 主检验: BLOCKED vs EXEC, close₀ 毛口径 (辅: T+1) ──
    print("\n== T1 事件级两臂 (主判据: 当日 close₀ 毛收益, 决策价=审计 entry) ==")
    t1 = day_cluster_boot_diff(blocked, executed, "r_close")
    t1_t1 = day_cluster_boot_diff(blocked, executed, "r_t1")
    b_only = day_cluster_boot_mean(blocked, "r_close")
    e_only = day_cluster_boot_mean(executed, "r_close")
    for label, x in (("BLOCKED−EXEC close₀", t1), ("BLOCKED−EXEC T+1", t1_t1),
                     ("BLOCKED vs 0", b_only), ("EXEC vs 0", e_only)):
        print(f"  {label}: {x}")
    h1, h2 = half_windows(blocked, executed, "r_close")
    sign, stable, loo = leave_one_day_sign(blocked, executed, "r_close")
    print(f"  双半窗差: {h1} / {h2}; 留一日: 全样本符号={sign} 稳健={stable} 极值={loo}")
    # 敏感性: BLOCKED 臂每指令-日仅首事件 (去重复轮询)
    first_ev = {}
    for e in sorted(blocked, key=lambda x: x["ts"] or ""):
        first_ev.setdefault((e["inst_id"], e["date"]), e)
    dedup = list(first_ev.values())
    t1_dedup = day_cluster_boot_diff(dedup, executed, "r_close")
    print(f"  [敏感性] BLOCKED 去重(每指令-日首事件, n={len(dedup)}): {t1_dedup}")
    if t1:
        ca = t1["ci95"][0] > 0
        cb = (h1 is not None and h2 is not None and h1 > 0 and h2 > 0)
        cc = b_days >= 6 and e_days >= 6
        cd = stable and sign == 1
        verdict = "逆向选择成立(进入执行时序裁决)" if (ca and cb and cc and cd) else "无逆向选择证据(本窗口)→按预注册关闭该轴"
        print(f"  判据自检: (a)CI不含0且为正={ca} (b)双半窗同正={cb} (c)两臂日≥6={cc} (d)留一日稳健={cd} ⇒ {verdict}")
        out["T1"] = {"main": t1, "t1_horizon": t1_t1, "blocked_vs0": b_only, "exec_vs0": e_only,
                     "halves": [h1, h2], "loo_stable": stable, "dedup": t1_dedup,
                     "criteria": {"a": ca, "b": cb, "c": cc, "d": cd}, "verdict": verdict}

    # ── T2 探索性: EXEC 臂内成交基差 vs 结局 ──
    print("\n== T2 (探索性, 不触发动作) EXEC 臂成交基差 vs 结局 ==")
    ex_rows = []
    for e in executed:
        i = inst.get(e["inst_id"]) or {}
        anchor = i.get("entry_price")
        if anchor and e.get("event_price"):
            ex_rows.append({"date": e["date"], "basis": e["event_price"] / anchor - 1.0,
                            "pnl_net": i.get("pnl_net"), "r_close": e.get("r_close")})
    if len(ex_rows) >= 3:
        rho_net, n1 = _spearman([r["basis"] for r in ex_rows], [r["pnl_net"] for r in ex_rows])
        rho_rc, n2 = _spearman([r["basis"] for r in ex_rows], [r["r_close"] for r in ex_rows])
        print(f"  n={len(ex_rows)}: rho(basis, pnl_net)={rho_net and round(rho_net,4)} (n={n1}); "
              f"rho(basis, r_close)={rho_rc and round(rho_rc,4)} (n={n2})")
        out["T2"] = {"n": len(ex_rows), "rho_basis_pnl_net": rho_net, "rho_basis_rclose": rho_rc}
    else:
        print("  样本不足")
        out["T2"] = None

    # ── T3: 同日配对 PULLBACK−BREAKOUT @ 开盘统一基准 ──
    print("\n== T3 同日配对 (PULLBACK−BREAKOUT) @ fwd_return_intraday_open (开盘统一基准) ==")
    arch_cnt = collections.Counter(archetype(r) for r in rows)
    print(f"  分型计数 (R4 新口径): {dict(arch_cnt)}")
    by_day_arch = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        a = archetype(r)
        if a in ("PULLBACK", "BREAKOUT") and r.get("fwd_return_intraday_open") is not None:
            by_day_arch[r["date"]][a].append(r["fwd_return_intraday_open"])
    pair_rows = []
    for d, g in sorted(by_day_arch.items()):
        if g.get("PULLBACK") and g.get("BREAKOUT"):
            pair_rows.append({"date": d, "diff": _mean(g["PULLBACK"]) - _mean(g["BREAKOUT"]),
                              "n_p": len(g["PULLBACK"]), "n_b": len(g["BREAKOUT"])})
    print(f"  配对日 n={len(pair_rows)} (判据门槛 ≥6)")
    if len(pair_rows) >= 6:
        rnd = random.Random(SEED)
        diffs = [p["diff"] for p in pair_rows]
        point = sum(diffs) / len(diffs)
        boots = []
        for _ in range(B):
            s = [rnd.choice(diffs) for _ in diffs]
            boots.append(sum(s) / len(s))
        boots.sort()
        lo = boots[int(0.025 * len(boots))]
        hi = boots[min(int(0.975 * len(boots)), len(boots) - 1)]
        pv = 2 * min(sum(1 for x in boots if x <= 0), sum(1 for x in boots if x >= 0)) / len(boots)
        print(f"  配对差(开盘基准): {round(point,5)} CI95 [{round(lo,5)}, {round(hi,5)}] p={round(min(pv,1),4)}")
        # 对照列示: 旧锚基准 (禁与新口径混算)
        anchor_pairs = []
        by_day_arch2 = collections.defaultdict(lambda: collections.defaultdict(list))
        for r in rows:
            a = archetype(r)
            if a in ("PULLBACK", "BREAKOUT") and r.get("fwd_return_intraday") is not None:
                by_day_arch2[r["date"]][a].append(r["fwd_return_intraday"])
        for d, g in sorted(by_day_arch2.items()):
            if g.get("PULLBACK") and g.get("BREAKOUT"):
                anchor_pairs.append(_mean(g["PULLBACK"]) - _mean(g["BREAKOUT"]))
        print(f"  [对照·旧锚基准, 仅列示] 配对日 {len(anchor_pairs)}: 均值差 "
              f"{round(sum(anchor_pairs)/len(anchor_pairs),5) if anchor_pairs else None}")
        out["T3"] = {"pair_days": len(pair_rows), "diff_open_basis": round(point, 5),
                     "ci95": [round(lo, 5), round(hi, 5)], "p": round(min(pv, 1), 4)}
    else:
        print("  配对日 <6 ⇒ 按预注册记「不可达」, 不读显著性")
        out["T3"] = {"pair_days": len(pair_rows), "verdict": "不可达(配对日<6)"}

    out["archetype_counts"] = dict(arch_cnt)
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n产物已写: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
