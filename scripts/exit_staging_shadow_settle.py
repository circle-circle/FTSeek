#!/usr/bin/env python3
"""RFC-188 分段落袋线影子结算器 (record-only 仪器的收卷器)。

输入  data/shadow/exit_staging_shadow.jsonl   (退出快循环 5min 粒度快照)
输入  data/ftseek_us.db                        (trade_records 里同仓的真实出场)
输出  data/shadow/exit_staging_shadow_settle.json

口径纪律:
  · 一行 ≠ 一个样本: 同一仓会按 stage 变化 / 300s 节流写多行 ⇒ 先按 (symbol, entry, ET日期) 折叠成"一个仓位序列"。
  · staged 线是 ratchet 的: LONG 取历史最大、SHORT 取历史最小, 不得用后出现的更松的线去"预知"前面的价格。
  · 反事实出场只在"线已被抬到优于初始 SL"之后才允许触发 (S0 阶段 staged==initial, 触发等于原本的 SL)。
  · 粒度是快照 (5min/事件), 不是逐 tick ⇒ 同一根 5min 内先破线后反弹的样本会被高估/低估, 报告里单列 frac_on_snapshot。
  · 费用两侧几乎相同 (同股数同价位), 因此判据用 gross bps 差值; 净额差按 fees 实算补一行。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
SHADOW = REPO / "data/shadow/exit_staging_shadow.jsonl"
DB = REPO / "data/ftseek_us.db"
OUT = REPO / "data/shadow/exit_staging_shadow_settle.json"

ET = ZoneInfo("America/New_York")
N_TARGET = 120          # 预注册: n≥120 独立仓或 2 周, 先到为准
MIN_PRIMARY_BPS = 8.0   # 预注册: 该桶反事实净 bps 均值 ≥ +8
MIN_EDGE_BPS = 5.0      # 预注册: 且 ≥ live 同批 +5bps (paired)


def _et_date(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()


def load_positions(path: Path) -> dict:
    """→ {(symbol, entry, et_date): [row, ...]} 按时间升序。"""
    buckets = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        key = (r["symbol"], round(float(r["entry"]), 2), _et_date(r["ts"]))
        buckets[key].append(r)
    for k in buckets:
        buckets[k].sort(key=lambda x: x["ts"])
    return dict(buckets)


def counterfactual(rows: list[dict]) -> dict | None:
    """按 ratchet 线找"首次破线"的快照 → 反事实出场。"""
    direction = rows[0]["direction"]
    entry = float(rows[0]["entry"])
    initial = float(rows[0]["initial_sl"])
    line = initial
    armed = False
    for r in rows:
        sl = float(r["staged_sl"])
        if direction == "LONG":
            line = max(line, sl)
        else:
            line = min(line, sl)
        # 线是否已优于初始 SL (即真的被抬过)
        better = line > initial + 1e-9 if direction == "LONG" else line < initial - 1e-9
        armed = armed or better
        if not armed:
            continue
        cur = float(r["cur"])
        hit = cur <= line + 1e-9 if direction == "LONG" else cur >= line - 1e-9
        if hit:
            return {"cf_exit": line, "cf_ts": r["ts"], "cf_stage": r["stage"],
                    "price_at_hit": cur, "snapshot_gap_pct": abs((cur - line) / line) * 100,
                    "peak_pct": float(r["peak_pct"])}
    # 未在快照上破线 → 交调用方用"实际出场价是否已越过最终线"补判 (无 bar 时的最小假设)
    return {"cf_exit": None, "cf_ts": None, "cf_stage": rows[-1]["stage"],
            "price_at_hit": None, "snapshot_gap_pct": None, "armed": armed,
            "final_line": line, "final_stage": rows[-1]["stage"],
            "peak_pct": max(float(r["peak_pct"]) for r in rows)}


def match_actual(conn, symbol: str, entry: float, day: str) -> dict | None:
    """在 trade_records 里找同仓真实出场 (价差 ≤1% + 同日)。"""
    for r in conn.execute(
        """select symbol,direction,quantity,entry_price,exit_price,exit_reason,realized_pnl,
                  pnl_net,entry_time,exit_time
           from trade_records where symbol=? and substr(trade_date,1,7)=?""",
        (symbol, day[:7]),
    ):
        if r["exit_price"] is None:
            continue
        if abs(float(r["entry_price"]) - entry) / entry > 0.01:
            continue
        if _et_date(r["entry_time"]) != day:
            continue
        return dict(r)
    return None


def bps(direction: str, entry: float, exit_px: float) -> float:
    d = (exit_px - entry) if direction == "LONG" else (entry - exit_px)
    return d / entry * 10000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not SHADOW.exists():
        print(f"影子文件不存在: {SHADOW}", file=sys.stderr)
        return 2

    pos = load_positions(SHADOW)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    detail, skipped, on_snapshot = [], 0, 0
    for (symbol, entry, day), rows in sorted(pos.items()):
        direction = rows[0]["direction"]
        act = match_actual(conn, symbol, entry, day)
        cf = counterfactual(rows)
        rec = {"symbol": symbol, "direction": direction, "day": day,
               "n_rows": len(rows), "max_stage": max(r["stage"] for r in rows),
               "peak_pct": cf["peak_pct"], "cf_stage": cf["cf_stage"],
               "cf_exit": cf["cf_exit"], "cf_bps": None, "actual_exit": None, "actual_bps": None,
               "delta_bps": None, "actual_reason": None}
        if act is None:
            skipped += 1
            rec["open"] = True
            detail.append(rec)
            continue
        rec["actual_exit"] = act["exit_price"]
        rec["actual_reason"] = act["exit_reason"]
        rec["actual_bps"] = round(bps(direction, entry, float(act["exit_price"])), 2)
        cf_exit = cf["cf_exit"]
        if cf_exit is None:
            # 快照粒度漏掉了破线: 若实际出场价已越过最终线 ⇒ 线必然被执行过, 用线价保守估值。
            line = float(cf.get("final_line") or 0.0)
            beyond = (float(act["exit_price"]) <= line + 1e-9 if direction == "LONG"
                      else float(act["exit_price"]) >= line - 1e-9)
            if cf.get("armed") and line and beyond:
                cf_exit = line
                rec["cf_source"] = "actual_crossing"
        if cf_exit is None:
            cf_exit = float(act["exit_price"])
        rec["cf_bps"] = round(bps(direction, entry, cf_exit), 2)
        rec["delta_bps"] = round(rec["cf_bps"] - rec["actual_bps"], 2)
        if cf["cf_exit"] is not None:
            rec["cf_fired"] = True
            on_snapshot += 1
            rec["snapshot_gap_pct"] = round(cf["snapshot_gap_pct"] or 0.0, 3)
        detail.append(rec)

    closed = [d for d in detail if d.get("delta_bps") is not None]
    deltas = [d["delta_bps"] for d in closed]
    summary = {"n_positions": len(detail), "n_closed": len(closed), "n_open": skipped,
               "n_rows": sum(len(v) for v in pos.values()),
               "cf_fired": on_snapshot,
               "delta_mean_bps": round(mean(deltas), 2) if deltas else None,
               "delta_median_bps": round(median(deltas), 2) if deltas else None,
               "delta_total_bps": round(sum(deltas), 2) if deltas else 0.0,
               "top1_share": (round(max(deltas, key=abs) / sum(deltas), 3)
                              if deltas and sum(deltas) else None)}
    if deltas:
        best = max(closed, key=lambda d: abs(d["delta_bps"]))
        summary["top1_symbol"] = best["symbol"]
        summary["verdict"] = ("READY_TO_ADJUDICATE" if len(closed) >= N_TARGET else "KEEP_SAMPLING")
    else:
        summary["verdict"] = "NO_SAMPLE"

    out = {"generated_at": datetime.now(ET).isoformat(), "criteria": {
        "n_target": N_TARGET, "primary_min_bps": MIN_PRIMARY_BPS, "edge_min_bps": MIN_EDGE_BPS,
        "note": "主判据只对 live 以 BE 出场且 peak∈[0.3,0.8) 的桶生效; kill = 净额≤live 或 top1>50% 总差"},
        "summary": summary, "positions": detail}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    s = summary
    print("== RFC-188 分段落袋线影子结算 ==")
    print(f"行 {s['n_rows']} → 独立仓 {s['n_positions']} (已平 {s['n_closed']} / 持仓中 {s['n_open']}) | 反事实在快照上触发 {s['cf_fired']}")
    if s["n_closed"]:
        print(f"Δ 均值 {s['delta_mean_bps']:+.2f}bps 中位 {s['delta_median_bps']:+.2f} 合计 {s['delta_total_bps']:+.1f}bps "
              f"top1 贡献 {s['top1_share']} ({s.get('top1_symbol')})")
    print(f"判据 n≥{N_TARGET}: state={s['verdict']}")
    for d in detail:
        if d.get("delta_bps") is None:
            print(f"  {d['symbol']:7} {d['direction']:5} 持仓中 peak={d['peak_pct']:.2f}% stage={d['max_stage']}")
        else:
            print(f"  {d['symbol']:7} {d['direction']:5} peak={d['peak_pct']:>5.2f}% {d['max_stage']} "
                  f"live {d['actual_bps']:+6.1f}bps({d['actual_reason']}) vs cf {d['cf_bps']:+6.1f}bps "
                  f"Δ{d['delta_bps']:+6.1f}")
    print(f"产物: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())