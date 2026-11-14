#!/usr/bin/env python3
"""入场几何与执行延迟只读分析（不改交易行为）。

回答三个问题：
1. 计划锚 → 实际成交是否发生顺向追价，若应用候选锚带，哪些单会被跳过；
2. entry_confirm arm 到实际成交的等待，确认窗与后续排队各占多少；
3. 宽限期大位移、收养仓 ATR/MFE 的口径是否一致。

净额只描述真实已平仓记录。候选锚带/灾难线只输出 affected 名单，
不使用缺失的分钟 bar 虚构反事实 PnL。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NET = "COALESCE(pnl_net, realized_pnl - COALESCE(fees_est, 0))"


def _rows(db: Path, sql: str, args=()):
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def _meta(row):
    try:
        value = json.loads(row.get("metadata_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _pct(value):
    return round(value, 4) if value is not None else None


def _median(values):
    return round(statistics.median(values), 3) if values else None


def _mean(values):
    return round(statistics.mean(values), 3) if values else None


def _adverse_pct(planned, fill, direction):
    if not planned or not fill:
        return None
    # LONG 更高价更差；SHORT 更低价更差。正数=顺向追价。
    sign = -1.0 if str(direction).upper() in ("SHORT", "SELL_SHORT") else 1.0
    return sign * (float(fill) - float(planned)) / float(planned) * 100.0


def _seconds(start, end):
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def _load_arms(path: Path, day: str):
    if not path.exists():
        return {}
    arms = {}
    bad_lines = 0
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        if row.get("type") == "entry_confirm" and row.get("date") == day:
            arms[int(row["inst_id"])] = row
    return arms, bad_lines


def build_review(day: str, db_path: Path, arms_path: Path, anchor_cap_pct: float = 2.0) -> dict:
    instructions = {
        int(row["id"]): row
        for row in _rows(db_path, "SELECT * FROM playbook_instructions WHERE date=?", (day,))
    }
    trades = _rows(db_path, f"""SELECT id,trade_date,symbol,direction,entry_time,exit_time,
        entry_price,exit_price,quantity,realized_pnl,fees_est,{NET} AS net_pnl,
        holding_hours,mfe_pct,mae_pct,metadata_json,decision_id,source_type
        FROM trade_records WHERE trade_date=? ORDER BY id""", (day,))
    arms, arms_bad_lines = _load_arms(arms_path, day)
    instruction_by_decision = {}
    for row in instructions.values():
        did = str(row.get("decision_id") or "")
        if did and did not in instruction_by_decision:
            instruction_by_decision[did] = row

    reprice_rows = []
    latency_rows = []
    recovery_rows = []
    grace_rows = []
    for trade in trades:
        meta = _meta(trade)
        did = str(trade.get("decision_id") or "")
        inst = instruction_by_decision.get(did, {})
        from app.playbook.pipeline_parts.intraday_indicators import _resolve_generated_anchor
        is_short = str(trade["direction"]).upper() in ("SHORT", "SELL_SHORT")
        planned, anchor_source = _resolve_generated_anchor(inst, is_short)
        if planned is None:
            planned = float(inst.get("entry_price") or 0)
            anchor_source = "db_entry_price_fallback"
        effective = float((meta.get("reprice_audit") or {}).get("reprice_to")
                          or inst.get("entry_price") or planned)
        fill = float(trade["entry_price"] or 0)
        adverse = _adverse_pct(planned, fill, trade["direction"])
        if planned and fill:
            reprice_rows.append({
                "symbol": trade["symbol"], "direction": trade["direction"],
                "decision_id": did, "planned_entry": planned,
                "anchor_source": anchor_source,
                "effective_entry": effective, "fill_entry": fill,
                "adverse_fill_vs_planned_pct": _pct(adverse),
                "adverse_fill_vs_effective_pct": _pct(_adverse_pct(effective, fill, trade["direction"])),
                "anchor_cap_pct": anchor_cap_pct,
                "would_anchor_skip": bool(adverse is not None and adverse > anchor_cap_pct),
                "net_pnl": trade["net_pnl"],
                "exit_time": trade["exit_time"],
            })
        source = str(meta.get("entry_price_source") or "")
        stop = float(meta.get("stop_loss") or 0)
        atr = float(meta.get("atr") or 0)
        distance = abs(fill - stop) if fill and stop else 0.0
        recovery_rows.append({
            "symbol": trade["symbol"], "entry_price_source": source or "missing",
            "atr": atr or None, "sl_distance": distance or None,
            "atr_over_sl_distance": _pct(atr / distance) if atr and distance else None,
            "peak_price": meta.get("peak_price"),
            "mfe_pct": trade.get("mfe_pct"),
        })
        inst_id = inst.get("id")
        arm = arms.get(int(inst_id)) if inst_id is not None else None
        if arm:
            post_confirm = _seconds(arm.get("ts"), trade["entry_time"])
            confirm_delay = arm.get("delay_actual_sec")
            if str(arm.get("arm")) == "treatment" and confirm_delay is not None:
                # treatment 的 ts 是确认落盘时刻；arm→confirm 与 confirm→fill 必须相加。
                total = post_confirm + float(confirm_delay) if post_confirm is not None else None
                queue = post_confirm
            else:
                # holdout 的 ts 是 arm/触发时刻，没有 120s 确认段。
                total = post_confirm
                queue = None
            latency_rows.append({
                "symbol": trade["symbol"], "arm": arm.get("arm") or "unknown",
                "arm_ts": arm.get("ts"), "fill_ts": trade["entry_time"],
                "total_delay_sec": total, "confirm_delay_sec": confirm_delay,
                "post_confirm_queue_sec": queue,
                "outcome": arm.get("outcome"),
            })
        held_min = _seconds(trade["entry_time"], trade["exit_time"])
        if held_min is not None and held_min / 60.0 <= 15.0:
            mae = trade.get("mae_pct")
            grace_rows.append({
                "symbol": trade["symbol"], "held_min": round(held_min / 60.0, 2),
                "mae_pct": mae, "mfe_pct": trade.get("mfe_pct"),
                "net_pnl": trade["net_pnl"],
                "mae_ge_2pct": bool(mae is not None and abs(mae) >= 2.0),
            })

    adverse_values = [r["adverse_fill_vs_planned_pct"] for r in reprice_rows
                      if r["adverse_fill_vs_planned_pct"] is not None]
    skipped = [r for r in reprice_rows if r["would_anchor_skip"]]
    latency_groups = {}
    for name in ("treatment", "holdout"):
        rows = [r for r in latency_rows if r["arm"] == name]
        total = [r["total_delay_sec"] for r in rows if r["total_delay_sec"] is not None]
        queue = [r["post_confirm_queue_sec"] for r in rows if r["post_confirm_queue_sec"] is not None]
        latency_groups[name] = {
            "n": len(rows), "median_total_delay_sec": _median(total),
            "median_post_confirm_queue_sec": _median(queue),
            "max_post_confirm_queue_sec": max(queue) if queue else None,
            "rows": rows,
        }
    backfills = [r for r in recovery_rows if r["entry_price_source"] == "sync_recovery_backfill"]
    ratios = [r["atr_over_sl_distance"] for r in backfills if r["atr_over_sl_distance"] is not None]
    closed = [r for r in trades if r["exit_time"]]
    return {
        "schema": "entry_geometry_review_v1",
        "date": day,
        "summary": {
            "n_entry_rows": len(trades), "n_closed": len(closed),
            "n_open": len(trades) - len(closed),
            "wins": sum(1 for r in closed if (r["net_pnl"] or 0) > 0),
            "gross_pnl": round(sum(float(r["realized_pnl"] or 0) for r in closed), 2),
            "fees_est": round(sum(float(r["fees_est"] or 0) for r in closed), 2),
            "net_pnl": round(sum(float(r["net_pnl"] or 0) for r in closed), 2),
        },
        "reprice": {
            "n": len(reprice_rows),
            "median_adverse_pct": _median(adverse_values),
            "mean_adverse_pct": _mean(adverse_values),
            "n_adverse_gt_1pct": sum(x > 1 for x in adverse_values),
            "n_adverse_gt_2pct": sum(x > 2 for x in adverse_values),
            "n_adverse_gt_3pct": sum(x > 3 for x in adverse_values),
            "n_adverse_gt_4pct": sum(x > 4 for x in adverse_values),
            "candidate_anchor_cap_pct": anchor_cap_pct,
            "would_skip_n": len(skipped),
            "would_skip_symbols": [r["symbol"] for r in skipped],
            "rows": reprice_rows,
        },
        "dispatch": latency_groups,
        "grace": {
            "n_short_closed": len(grace_rows),
            "n_mae_ge_2pct": sum(r["mae_ge_2pct"] for r in grace_rows),
            "rows": grace_rows,
        },
        "recovery": {
            "n_sync_recovery_backfill": len(backfills),
            "median_atr_over_sl_distance": _median(ratios),
            "rows": recovery_rows,
        },
        "data_quality": {"arms_bad_lines": arms_bad_lines},
        "caveats": [
            "候选锚带和灾难线只输出 affected 名单，不切换任何生产行为。",
            "缺少分钟 bar 时不计算反事实 PnL；真实成交后路径不能由计划价假设替代。",
            "sync_recovery_backfill 的 ATR/peak/MFE 可能是收养时近似值，须先修口径再评估退出。",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "ftseek_us.db")
    parser.add_argument("--arms", type=Path, default=ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl")
    parser.add_argument("--anchor-cap-pct", type=float, default=2.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = build_review(args.date, args.db, args.arms, args.anchor_cap_pct)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.as_json:
        print(text)
    else:
        s, r, d = result["summary"], result["reprice"], result["dispatch"]
        print(f"{args.date}: closed={s['n_closed']} wins={s['wins']} net={s['net_pnl']}")
        print(f"reprice adverse median={r['median_adverse_pct']}% "
              f"cap={r['candidate_anchor_cap_pct']}% would_skip={r['would_skip_n']} "
              f"{r['would_skip_symbols']}")
        for arm in ("treatment", "holdout"):
            print(f"{arm}: total median={d[arm]['median_total_delay_sec']}s "
                  f"queue median={d[arm]['median_post_confirm_queue_sec']}s "
                  f"max={d[arm]['max_post_confirm_queue_sec']}s")
        print(f"grace MAE>=2%: {result['grace']['n_mae_ge_2pct']}/{result['grace']['n_short_closed']}")
        print("仅只读分析，候选规则未生效，不计算反事实 PnL。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
