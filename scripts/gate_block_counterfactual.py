#!/usr/bin/env python3
"""触发门拦截反事实记分牌 (DEB-20260816-no-trigger R3, 2026-08-16, record-only).

问题 (用户 8/16): 如何确保不被历史信息拦截机会, 也能基于历史真正教训规避重复的坑?
方法: 把 trigger_miss_telemetry.jsonl (每次触发评估被门拦都记一行, 含 inst_id)
JOIN 回 playbook_instructions 的前瞻收益, 按拦截原因聚合 → 每个门的"拦截质量":
  - 被拦集反事实为正 → 门在杀机会
  - 被拦集反事实为负 → 门在真正避坑 (拦住的票本来会亏)

口径纪律 (继承 DEB-20260816-no-trigger §零):
  - 裁决基准 = fwd_return_t1_open (开盘价基准, 可成交反事实)。
    未触发指令的计划 entry_price 不可成交, anchor 基准 (fwd_return_t1/shadow_pnl)
    含纸面 edge, 仅作诊断列输出, 禁止引用。
  - 遥测按 inst_id 去重: 轮询级遥测对同一指令重复记录 (实测 streak_ban 784 事件
    /16 指令 = 49x 放大), 不去重 = 口径事故。
  - TEST 标的: 遥测写入端已过滤 (pipeline 2026-07-30 守卫), 读取端再验一道。
  - 费用: 报 mean 与 mean−fee_drag 两列 (默认 0.0018, DA 期实测)。

只读, 零落库变更。输出: 控制台 + data/gate_block_counterfactual_latest.json
用法: ./venv/bin/python scripts/gate_block_counterfactual.py [--fee-drag 0.0018] [--min-n 10]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
TELEMETRY = ROOT / "data" / "trigger_miss_telemetry.jsonl"
OUT = ROOT / "data" / "gate_block_counterfactual_latest.json"


def _stats(vals):
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 6),
        "median": round(st.median(vals), 6),
        "pos_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee-drag", type=float, default=0.0018)
    ap.add_argument("--min-n", type=int, default=10, help="JOIN 后样本下限")
    args = ap.parse_args()

    if not TELEMETRY.exists():
        print(f"telemetry not found: {TELEMETRY}")
        return 1

    # 1. 遥测按 inst_id 去重 (轮询放大), 记录事件数供审计
    events_by_reason = {}
    inst_reason = {}  # inst_id -> reason (首次出现)
    inst_meta = {}    # inst_id -> (symbol, direction, date)
    n_events = 0
    with TELEMETRY.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            n_events += 1
            iid = r.get("inst_id")
            reason = r.get("reason") or "?"
            sym = str(r.get("symbol") or "")
            if "TEST" in sym.upper():
                continue
            events_by_reason[reason] = events_by_reason.get(reason, 0) + 1
            if iid and iid not in inst_reason:
                inst_reason[iid] = reason
                inst_meta[iid] = (sym, r.get("direction"), r.get("date"))
    print(f"telemetry events: {n_events}, distinct inst_id: {len(inst_reason)} "
          f"(polling amplification {n_events/max(1,len(inst_reason)):.1f}x)")

    # 2. JOIN 结局
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    per_reason = {}
    for iid, reason in inst_reason.items():
        row = con.execute(
            "SELECT direction, fwd_return_t1, fwd_return_t1_open, "
            "fwd_return_intraday_open, shadow_pnl, date, symbol "
            "FROM playbook_instructions WHERE id=?", (iid,)).fetchone()
        if row is None:
            continue
        d = per_reason.setdefault(reason, {
            "events": events_by_reason.get(reason, 0),
            "inst_ids": [], "t1_open": [], "intraday_open": [],
            "t1_anchor_diag_only": [], "shadow_pnl_anchor_diag_only": [],
            "dates": [],
        })
        d["inst_ids"].append(iid)
        d["dates"].append(row["date"])
        if row["fwd_return_t1_open"] is not None:
            d["t1_open"].append(row["fwd_return_t1_open"])
        if row["fwd_return_intraday_open"] is not None:
            d["intraday_open"].append(row["fwd_return_intraday_open"])
        if row["fwd_return_t1"] is not None:
            d["t1_anchor_diag_only"].append(row["fwd_return_t1"])
        if row["shadow_pnl"] is not None:
            d["shadow_pnl_anchor_diag_only"].append(row["shadow_pnl"])
    con.close()

    # 3. 记分牌
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "caliber": {
            "adjudication_basis": "fwd_return_t1_open (开盘基准, 扣费另列)",
            "anchor_columns": "t1/shadow_pnl 为 anchor 基准纸面值, 仅诊断, 禁止引用",
            "dedup": "telemetry 按 inst_id 去重 (轮询放大)",
            "fee_drag": args.fee_drag,
        },
        "gates": {},
    }
    hdr = (f"{'gate':22s} {'events':>7s} {'n_inst':>6s} {'open_t1':>8s} {'net':>8s} "
           f"{'pos%':>5s} {'intraday_o':>10s} {'verdict':12s}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for reason in sorted(per_reason, key=lambda k: -len(per_reason[k]["inst_ids"])):
        d = per_reason[reason]
        n_joined = len(d["inst_ids"])
        s = _stats(d["t1_open"])
        si = _stats(d["intraday_open"])
        if s["n"] < args.min_n:
            verdict = "n<%d 不判" % args.min_n
            net = None
        else:
            net = s["mean"] - args.fee_drag
            verdict = "避坑✓" if net < -0.001 else ("杀机会✗" if net > 0.001 else "中性")
        d_out = {
            "events": d["events"], "n_instructions": n_joined,
            "t1_open": s, "t1_open_net_after_fee": round(net, 6) if net is not None else None,
            "intraday_open": si,
            "t1_anchor_diag_only": _stats(d["t1_anchor_diag_only"]),
            "shadow_pnl_anchor_diag_only": {
                "n": len(d["shadow_pnl_anchor_diag_only"]),
                "sum": round(sum(d["shadow_pnl_anchor_diag_only"]), 2),
            },
            "date_range": [min(d["dates"]) if d["dates"] else None,
                           max(d["dates"]) if d["dates"] else None],
            "verdict": verdict,
        }
        report["gates"][reason] = d_out
        print(f"{reason:22s} {d['events']:7d} {n_joined:6d} "
              f"{s.get('mean',0)*100 if s['n'] else 0:+7.2f}% "
              f"{net*100 if net is not None else 0:+7.2f}% "
              f"{s.get('pos_rate',0)*100 if s['n'] else 0:4.0f}% "
              f"{si.get('mean',0)*100 if si['n'] else 0:+9.2f}% {verdict:12s}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON 落盘: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
