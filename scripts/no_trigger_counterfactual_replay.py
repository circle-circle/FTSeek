#!/usr/bin/env python3
"""未触发指令反事实回放 (DEB-20260816-no-trigger-direction-edge, 2026-08-16).

问题: LLM 方向信号在"未触发(价格未达入场确认带)"的指令集里是否仍有预测力?
即: 触发确认门 (BUY=突破当日高点, pipeline.py L4022) 是正过滤器还是负过滤器?

双基准口径 (本脚本的核心纪律):
  - anchor 基准 (fwd_return_t1): 以指令的计划 entry_price 为起点。未触发单的
    entry_price 是"从未成交的限价", 该基准制造纸面 edge (ic_tracker L223 注释
    自证: "未执行指令 limit 价不可成交, entry 基准会制造纸面 edge 幻觉")。
    只可用作诊断, 禁止作为反事实收益引用。
  - open 基准 (fwd_return_t1_open): 以当日开盘价为起点 (ic_tracker 2026-08-01
    落库的孪生列)。这是"若开盘进场"的可成交反事实, 是本脚本的裁决基准。
    费用拖累另计 (--fee-drag, 默认 0.0018 = DA 期 227 笔实测均值)。

方向调整: fwd_return_* 列在 ic_tracker 回填时已乘方向符号 (BUY=+1, SHORT=-1),
正值=方向判断正确。NEUTRAL 基线来自 signal_snapshots (基准=price_at_signal,
无 open 孪生列, 口径不同, 仅作粗略参照)。

只读, 零落库变更, 零行为改动。输出: 控制台表 + data/no_trigger_counterfactual_latest.json

用法: ./venv/bin/python scripts/no_trigger_counterfactual_replay.py [--fee-drag 0.0018]
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
SNAP_DB = ROOT / "data" / "signal_snapshots.db"
OUT = ROOT / "data" / "no_trigger_counterfactual_latest.json"

NOT_TRIGGER = "未触发%"  # skip_reason LIKE


def _stats(vals):
    if not vals:
        return {"n": 0}
    vals = sorted(vals)
    n = len(vals)
    return {
        "n": n,
        "mean": round(sum(vals) / n, 6),
        "median": round(st.median(vals), 6),
        "pos_rate": round(sum(1 for v in vals if v > 0) / n, 4),
    }


def _split_is_oos(rows, col):
    dates = sorted(set(r["date"] for r in rows))
    if len(dates) < 4:
        return None, None
    mid = dates[len(dates) // 2]
    is_v = [r[col] for r in rows if r["date"] <= mid and r[col] is not None]
    oos_v = [r[col] for r in rows if r["date"] > mid and r[col] is not None]
    return _stats(is_v), _stats(oos_v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee-drag", type=float, default=0.0018,
                    help="单笔费用拖累 (默认 0.0018 = DA期227笔 fees/notional 实测均值)")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    groups = {
        "no_trigger_BUY": ("executed!=1 AND direction='BUY' AND skip_reason LIKE ?", (NOT_TRIGGER,)),
        "executed_BUY": ("executed=1 AND direction='BUY'", ()),
        "no_trigger_SHORT": ("executed!=1 AND direction='SELL_SHORT' AND skip_reason LIKE ?", (NOT_TRIGGER,)),
        "executed_SHORT": ("executed=1 AND direction='SELL_SHORT'", ()),
        "trigger_no_exec_BUY": ("executed!=1 AND direction='BUY' AND skip_reason LIKE '已触发未执行%'", ()),
    }
    cols = ["fwd_return_t1", "fwd_return_t1_open",
            "fwd_return_intraday", "fwd_return_intraday_open"]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "caliber": {
            "adjudication_basis": "fwd_return_t1_open (开盘价基准, 可成交反事实)",
            "anchor_basis_warning": "fwd_return_t1 对未触发单=计划限价基准, 纸面edge, 禁止引用",
            "direction_adjusted": True,
            "fee_drag": args.fee_drag,
        },
        "groups": {},
    }

    print("=" * 78)
    print("未触发指令反事实回放 (双基准)")
    print(f"  裁决基准: fwd_return_t1_open (开盘进场) | 费用拖累: {args.fee_drag*100:.3f}%")
    print("=" * 78)
    hdr = f"{'group':22s} {'basis':14s} {'n':>4s} {'mean':>8s} {'median':>8s} {'pos%':>5s} {'net(-fee)':>9s}"
    print(hdr)
    print("-" * 78)

    for name, (where, params) in groups.items():
        rows = con.execute(
            f"SELECT date, market_regime, {', '.join(cols)} FROM playbook_instructions WHERE {where}",
            params).fetchall()
        g = {"rows": len(rows)}
        for col in cols:
            vals = [r[col] for r in rows if r[col] is not None]
            s = _stats(vals)
            g[col] = s
            net = (s["mean"] - args.fee_drag) if s["n"] else None
            g[col + "_net_after_fee"] = round(net, 6) if net is not None else None
            if s["n"]:
                tag = "OPEN " if "_open" in col else "anchor"
                print(f"{name:22s} {col.replace('fwd_return_',''):14s} {s['n']:4d} "
                      f"{s['mean']*100:+7.2f}% {s['median']*100:+7.2f}% {s['pos_rate']*100:4.0f}% "
                      f"{net*100:+8.2f}%")
        # IS/OOS on adjudication basis
        is_s, oos_s = _split_is_oos(rows, "fwd_return_t1_open")
        g["t1_open_IS"] = is_s
        g["t1_open_OOS"] = oos_s
        if is_s and oos_s:
            print(f"{'':22s} t1_open IS/OOS: IS mean={is_s['mean']*100:+.2f}% (n={is_s['n']}) | "
                  f"OOS mean={oos_s['mean']*100:+.2f}% (n={oos_s['n']})")
        # regime split (open basis)
        reg = {}
        for r in rows:
            if r["fwd_return_t1_open"] is None:
                continue
            reg.setdefault(r["market_regime"] or "?", []).append(r["fwd_return_t1_open"])
        g["t1_open_by_regime"] = {k: _stats(v) for k, v in reg.items()}
        # monthly
        mon = {}
        for r in rows:
            if r["fwd_return_t1_open"] is None:
                continue
            mon.setdefault(r["date"][:7], []).append(r["fwd_return_t1_open"])
        g["t1_open_by_month"] = {k: _stats(v) for k, v in sorted(mon.items())}
        print()
        report["groups"][name] = g

    # NEUTRAL baseline (snapshots, 不同基准, 仅参照)
    try:
        sn = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
        sn.row_factory = sqlite3.Row
        vals = [r["fwd_ret_t1"] for r in sn.execute(
            "SELECT fwd_ret_t1 FROM signal_snapshots WHERE direction='NEUTRAL' AND fwd_ret_t1 IS NOT NULL")]
        report["neutral_baseline_snapshot_basis"] = _stats(vals)
        s = _stats(vals)
        print(f"{'NEUTRAL(快照基准)':22s} {'t1':14s} {s['n']:4d} {s['mean']*100:+7.2f}% "
              f"{s['median']*100:+7.2f}% {s['pos_rate']*100:4.0f}%   (不同基准,仅参照)")
        sn.close()
    except Exception as e:
        print(f"NEUTRAL baseline unavailable: {e}")

    # 核心差值 (裁决用)
    nb = report["groups"].get("no_trigger_BUY", {}).get("fwd_return_t1_open", {})
    eb = report["groups"].get("executed_BUY", {}).get("fwd_return_t1_open", {})
    if nb.get("n") and eb.get("n"):
        gap = nb["mean"] - eb["mean"]
        report["verdict_gap_BUY_open_basis"] = {
            "no_trigger_minus_executed": round(gap, 6),
            "net_after_fee": round(gap - args.fee_drag, 6),
            "note": "未触发组相对已执行组的开盘基准边际, 已扣单边费用拖累",
        }
        print("\n" + "=" * 78)
        print(f"裁决差值 (BUY, open基准): 未触发 - 已执行 = {gap*100:+.2f}pp "
              f"(扣费后 {max(0, gap - args.fee_drag)*100 if False else (gap-args.fee_drag)*100:+.2f}pp)")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON 落盘: {OUT}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
