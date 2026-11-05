#!/usr/bin/env python3
"""供应侧 conf 判据 + 2min entry_confirm 场景分层 读数器 (只读, 零交易行为).

用途: RFC-159 Q1/Q2 的可复现读数。任何"conf 该不该当门 / 确认门该不该按场景开"
的重新讨论都先跑这个脚本, 不要凭印象。

口径 (与线上判据同源):
  · 人群 = data/shadow/entry_delay_confirm.jsonl 中 delay_min=2 (或 5) 且 status=ok 的行;
    该影子由 scripts/entry_delay_confirm_shadow.py 产出, 判据与 entry_confirm_gate.eval_confirm 同式。
  · 反事实值: 确认(仍成交) Δ = −opp_cost_pct/100 × notional; 弃单 Δ = −pnl_net。
  · 显著性: 按**交易日聚类** bootstrap (同日笔不独立), 2000 次重抽。
  · conf 证据: trade_records 中 source_type='PLAYBOOK_SIM' 且 trade_date ≥ A10 起点 (2026-07-01)。

用法:
  PYTHONPATH=. venv/bin/python scripts/supply_conf_and_confirm_scenario_report.py [--delay 2]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHADOW = REPO / "data" / "shadow" / "entry_delay_confirm.jsonl"
DB = REPO / "data" / "ftseek_us.db"
A10_START = "2026-07-01"
BAR_CACHE = REPO / "data" / "shadow" / "bars_cache.json"


def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _day_ci(rows, key, iters=2000, seed=13):
    """按交易日聚类 bootstrap 的 95% CI (对"笔均"的抽样分布)。"""
    g = defaultdict(list)
    for r in rows:
        g[r["date"]].append(r[key])
    ks = list(g)
    if len(ks) < 3:
        return float("nan"), float("nan")
    random.seed(seed)
    means = []
    for _ in range(iters):
        s = [v for k in random.choices(ks, k=len(ks)) for v in g[k]]
        means.append(sum(s) / len(s))
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def load_rows(delay):
    out = []
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("delay_min") != delay or r.get("status") != "ok":
            continue
        pnl = r.get("pnl_net") or 0.0
        oc = (r.get("opp_cost_pct") or 0.0) / 100.0 * (r.get("notional") or 0.0)
        d = (-oc) if r.get("still_triggers") else (-pnl)
        out.append({
            "date": r["date"], "d": d - pnl, "base": pnl, "row": r,
            "direction": r.get("direction"), "minute": r.get("entry_min") or "",
            "source": r.get("source"), "notional": float(r.get("notional") or 0),
        })
    return out


def plan_anchor_map(conn):
    """(symbol,date) → 计划锚。优先 reprice_audit.reprice_from (re-price 前的原始计划价)。"""
    m = {}
    for row in conn.execute(
        """select date, symbol, direction, entry_price, metadata_json
             from playbook_instructions
            where direction != 'SKIP' and entry_price > 0"""
    ):
        anchor = None
        try:
            ra = (json.loads(row["metadata_json"] or "{}") or {}).get("reprice_audit") or {}
            v = ra.get("reprice_from")
            if isinstance(v, (int, float)) and v > 0:
                anchor = float(v)
        except Exception:
            anchor = None
        m.setdefault((row["symbol"], row["date"]), []).append(
            (row["direction"], anchor if anchor else float(row["entry_price"]))
        )
    return m


def vol_map():
    try:
        bars = json.loads(BAR_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for sym, rows in bars.items():
        for r in rows:
            try:
                out[(sym, r[0])] = (r[2] - r[3]) / max(r[4], 1e-9) * 100
            except Exception:
                continue
    return out


def table(name, rows, keyf, minn=30):
    g = defaultdict(list)
    for x in rows:
        k = keyf(x)
        if k is None:
            continue
        g[k].append(x)
    print(f"\n== {name} ==")
    for k in sorted(g, key=str):
        v = g[k]
        if len(v) < minn:
            continue
        lo, hi = _day_ci(v, "d")
        ds = [x["d"] for x in v]
        mark = "✅" if lo > 0 else ("❌" if hi < 0 else "~")
        print(f"  {str(k):<18} n={len(v):<4} 门Δ笔均=${st.mean(ds):>7.2f} "
              f"中位=${st.median(ds):>7.2f} CI=[{lo:>7.2f},{hi:>7.2f}] {mark}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=int, default=2)
    args = ap.parse_args()

    rows = load_rows(args.delay)
    if not rows:
        print(f"零行: delay={args.delay} 无可评估行 (三态: TOO_EARLY / DEAD / 无该腿)")
        return
    total = sum(x["d"] for x in rows)
    lo, hi = _day_ci(rows, "d")
    oc = [(x["row"].get("opp_cost_pct") or 0) * 100 for x in rows if x["row"].get("still_triggers")]
    print(f"### delay={args.delay}min n={len(rows)}")
    print(f"  Δ合计=${total:,.0f} 笔均=${total/len(rows):.2f} 中位=${st.median([x['d'] for x in rows]):.2f} "
          f"日聚类95%CI=[{lo:.2f},{hi:.2f}]")
    if oc:
        print(f"  确认滑点 n={len(oc)} 均值={st.mean(oc):.2f}bps 中位={st.median(oc):.2f}bps "
              f"P90={sorted(oc)[int(0.9*len(oc))]:.2f}bps")
    lost = [x for x in rows if not x["row"].get("still_triggers")]
    print(f"  弃单 {len(lost)}/{len(rows)} = {len(lost)/len(rows)*100:.1f}% "
          f"(本会亏 {sum(1 for x in lost if x['base']<0)} 笔省 ${-sum(x['base'] for x in lost if x['base']<0):,.0f} / "
          f"本会赚 {sum(1 for x in lost if x['base']>0)} 笔少赚 ${sum(x['base'] for x in lost if x['base']>0):,.0f})")
    k = max(1, len(rows) // 20)
    worst = sorted(rows, key=lambda x: x["base"])[:k]
    best = sorted(rows, key=lambda x: -x["base"])[:k]
    print(f"  尾部5%: 最差 基线${sum(x['base'] for x in worst):,.0f}→加门${sum(x['base']+x['d'] for x in worst):,.0f} "
          f"| 最好 基线${sum(x['base'] for x in best):,.0f}→加门${sum(x['base']+x['d'] for x in best):,.0f}")

    table("方向", rows, lambda x: x["direction"])
    table("时段", rows, lambda x: ("开盘<09:45" if x["minute"] < "09:45" else
                                   ("09:45-11:00" if x["minute"] < "11:00" else
                                    ("11:00-14:00" if x["minute"] < "14:00" else ">=14:00"))))
    table("来源", rows, lambda x: x["source"])
    table("名义额", rows, lambda x: ("<$3k" if x["notional"] < 3000 else
                                     ("3-6k" if x["notional"] < 6000 else
                                      ("6-12k" if x["notional"] < 12000 else ">=12k"))))

    conn = _conn()
    anchors = plan_anchor_map(conn)
    vols = vol_map()

    def chase(x):
        plans = anchors.get((x["row"]["symbol"], x["date"])) or []
        want = "BUY" if x["direction"] == "LONG" else "SELL_SHORT"
        p = next((a for d, a in plans if d == want), None)
        if not p:
            return None
        c = (x["row"]["entry_price"] - p) / p * 100
        return -c if x["direction"] == "SHORT" else c

    for x in rows:
        x["chase"] = chase(x)
        x["vol"] = vols.get((x["row"]["symbol"], x["date"]))
    table("追价幅度(成交 vs 计划锚)", rows,
          lambda x: None if x["chase"] is None else
          ("<-1% 反抽" if x["chase"] < -1 else ("-1~0.3% 贴合" if x["chase"] < 0.3 else
                                                ("0.3-1% 追" if x["chase"] < 1 else ">=1% 追高"))))
    table("当日波幅(日线range%)", rows,
          lambda x: None if x["vol"] is None else
          ("<1.5%" if x["vol"] < 1.5 else ("1.5-3%" if x["vol"] < 3 else
                                           ("3-6%" if x["vol"] < 6 else ">=6%"))))

    print("\n== conf 预测力 (A10, PLAYBOOK_SIM) ==")
    rs = [dict(r) for r in conn.execute(
        """select signal_confidence conf, realized_pnl pnl from trade_records
            where trade_date >= ? and source_type='PLAYBOOK_SIM' and realized_pnl is not null""",
        (A10_START,))]
    xs = [r["conf"] for r in rs if r["conf"] is not None]
    ys = [r["pnl"] for r in rs if r["conf"] is not None]
    if xs:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
        print(f"  IC(conf,$pnl)={num/den if den else float('nan'):+.3f}  n={len(xs)}")
        g = defaultdict(list)
        for r in rs:
            if r["conf"] is not None:
                g["<40" if r["conf"] < 40 else ("40-55" if r["conf"] < 55 else ">=55")].append(r["pnl"])
        for k2 in ("<40", "40-55", ">=55"):
            v = g.get(k2) or []
            if v:
                print(f"  conf {k2:<6} n={len(v):<4} 笔均=${sum(v)/len(v):>7.2f} "
                      f"胜率={sum(1 for x in v if x>0)/len(v)*100:>5.1f}%")


if __name__ == "__main__":
    main()