#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regime 的 VIX 分项：代理 vs 真值 的翻转回放 (只读, 2026-11-09)

背景: `market_regime` 的 VIX 分项 (<15 +3 / 15-20 +1 / 20-30 -2 / >=30 -5) 用的是
当轮 VIX — 盘前/盘中通常是 VIXY×0.85 代理 (系统性偏低 16.7% 中位, 最差 36%)。
问: 若把这一项换成"盘前可得的真值"(上一交易日 CBOE 收盘), regime 会不会换档?
换档的那些天, 实际赚亏如何?

口径 (逐条照抄, 防前视):
  - 历史 score / vix / regime 取 `playbook_regime_log` (生成时点的真实写入);
  - score_wo_vix = score - vix_term(生成时 vix)  ← 只替换 VIX 一项, 其余分项不动;
  - 真值 = `market_env_daily.jsonl` 中 **严格早于该日** 的最近一条 vix_close (盘前可得);
  - 当日 PnL = `trade_records` 同日 `pnl_net` 之和 (A10 口径), 仅用于观察方向, 不作因果声明;
  - 缺任一侧 (无真值/无 score) 该日跳过, 不插值。

输出: 翻转日清单 + 翻转/未翻转日的净额对照 + 分档统计。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "market_env_daily.jsonl"
DB = ROOT / "data" / "ftseek_us.db"


def vix_term(vix: Optional[float]) -> Optional[int]:
    if vix is None:
        return None
    if vix < 15:
        return 3
    if vix <= 20:
        return 1
    if vix < 30:
        return -2
    return -5


def regime_of(score: float) -> str:
    if score >= 5:
        return "risk_on"
    if score >= 0:
        return "cautious"
    if score >= -5:
        return "risk_off"
    return "no_trade"


def load_truth() -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not LEDGER.exists():
        return out
    for ln in LEDGER.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        v = r.get("vix_close")
        if v is not None:
            out[r["date"]] = float(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    truth = load_truth()
    truth_dates = sorted(truth)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT date, regime, vix, score FROM playbook_regime_log "
        "WHERE date >= ? ORDER BY date", (args.since,))]
    pnl = {r["trade_date"]: float(r["net"] or 0) for r in conn.execute(
        "SELECT trade_date, ROUND(SUM(COALESCE(pnl_net,0)),2) net "
        "FROM trade_records WHERE trade_date >= ? GROUP BY trade_date", (args.since,))}

    out_rows: List[dict] = []
    for r in rows:
        if r["score"] is None or r["vix"] is None:
            continue
        prev = [d for d in truth_dates if d < r["date"]]
        if not prev:
            continue
        truth_vix = truth[prev[-1]]
        base_term = vix_term(float(r["vix"]))
        new_term = vix_term(truth_vix)
        if base_term is None or new_term is None:
            continue
        score_wo = float(r["score"]) - base_term
        score_new = score_wo + new_term
        out_rows.append({
            "date": r["date"],
            "regime_logged": r["regime"],
            "regime_truth": regime_of(score_new),
            "vix_logged": round(float(r["vix"]), 2),
            "vix_truth": round(truth_vix, 2),
            "truth_date": prev[-1],
            "score_logged": r["score"],
            "score_truth": score_new,
            "flip": regime_of(score_new) != r["regime"],
            "day_net": pnl.get(r["date"]),
        })

    flips = [x for x in out_rows if x["flip"]]
    same = [x for x in out_rows if not x["flip"]]
    print(f"样本: {len(out_rows)} 个 regime 日 (有 score + 有前置真值)")
    print(f"翻转: {len(flips)} 天 ({len(flips)/max(1,len(out_rows))*100:.1f}%)")
    print(f"{'date':12s} {'logged→truth':26s} {'vix(log→true)':18s} {'score':12s} {'day_net':>9s}")
    for x in flips:
        print(f"{x['date']:12s} {x['regime_logged']:>10s}→{x['regime_truth']:<10s} "
              f"{x['vix_logged']:6.2f}→{x['vix_truth']:6.2f}   "
              f"{x['score_logged']:>4}→{x['score_truth']:<4} "
              f"{(x['day_net'] if x['day_net'] is not None else 0):9.2f}")

    def agg(xs, key):
        vals = [x[key] for x in xs if x[key] is not None]
        return (sum(vals), len(vals))

    for name, xs in (("翻转日", flips), ("未翻转日", same)):
        total, n = agg(xs, "day_net")
        print(f"{name}: n={n} 合计 net={total:.2f} 均值={total/max(1,n):.2f}")

    # 方向: 真值让 regime 更保守 / 更激进
    order = {"no_trade": 0, "risk_off": 1, "cautious": 2, "risk_on": 3}
    tighter = [x for x in flips if order[x["regime_truth"]] < order[x["regime_logged"]]]
    looser = [x for x in flips if order[x["regime_truth"]] > order[x["regime_logged"]]]
    for name, xs in (("更保守(truth 更低档)", tighter), ("更激进(truth 更高档)", looser)):
        total, n = agg(xs, "day_net")
        print(f"  {name}: n={n} net={total:.2f}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "n_days": len(out_rows), "n_flips": len(flips),
            "flips": flips, "same_days_net": agg(same, "day_net")[0],
            "tighter_days": len(tighter), "looser_days": len(looser),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写入", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())