#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gap_alignment_shadow — 跳空对齐度 × 入场窗口 影子观测 (DEB-20260901 P1).

纯只读: 不拦截、不改行为。把每笔 PLAYBOOK_SIM 入场按
  aligned_gap_pct (开盘跳空与方向的对齐度) × 入场窗口 (10:00 ET 前后)
分桶, 关联当日实现盈亏与次日回填的 fwd_return (T+1 运行才有),
逐日追加 data/shadow/gap_alignment_shadow.jsonl, 并刷新累计汇总。

与 -2%~-5% 真空带 A/C 影子门并池语义 (同属"弱开盘多头"假设), 不新造门。
用法: python3 scripts/gap_alignment_shadow.py [--date YYYY-MM-DD]
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"
OUT_JSONL = REPO / "data" / "shadow" / "gap_alignment_shadow.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "gap_alignment_shadow_latest.json"


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def bucket_gap(ag, direction):
    if ag is None:
        return "unknown"
    if direction == "LONG":
        if ag <= 0:
            return "flat_or_down_open"
        if ag < 4:
            return "gap_up_0_4"
        return "gap_up_4plus"
    # SHORT
    if ag < -2:
        return "gap_down_2plus"
    if ag <= 0:
        return "flat_slight_down"
    if ag < 4:
        return "gap_up_0_4"
    return "gap_up_4plus"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    args = ap.parse_args()
    date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    trades = db.execute(
        """SELECT t.id, t.symbol, t.direction, t.entry_time, t.decision_id,
                  t.pnl_net, t.realized_pnl, t.exit_reason, t.metadata_json,
                  i.fwd_return_intraday, i.fwd_return_t1, i.raw_instruction_json
           FROM trade_records t
           LEFT JOIN playbook_instructions i
             ON i.id = (SELECT MIN(i2.id) FROM playbook_instructions i2
                        WHERE i2.decision_id = t.decision_id AND t.decision_id != '')
           WHERE t.source_type='PLAYBOOK_SIM' AND t.trade_date=? AND t.exit_time IS NOT NULL""",
        (date,)).fetchall()

    rows = []
    for t in trades:
        try:
            meta = json.loads(t["metadata_json"] or "{}")
            ag = meta.get("entry_gap", {}).get("aligned_gap_pct")
            ag = float(ag) if ag is not None else None
        except Exception:
            ag = None
        try:
            raw = json.loads(t["raw_instruction_json"] or "{}") if t["raw_instruction_json"] else {}
        except Exception:
            raw = {}
        et_hm = (t["entry_time"] or "")[11:16]
        window = "open_0930_1000" if et_hm and et_hm < "10:00" else "after_1000"
        rows.append({
            "date": date, "schema": "v1", "trade_id": t["id"], "symbol": t["symbol"],
            "direction": t["direction"], "entry_time_et": t["entry_time"],
            "aligned_gap_pct": ag, "gap_bucket": bucket_gap(ag, t["direction"]),
            "window": window,
            "entry_archetype": raw.get("entry_archetype"),
            "pnl_net": t["pnl_net"] if t["pnl_net"] is not None else t["realized_pnl"],
            "exit_reason": t["exit_reason"],
            "fwd_return_intraday": t["fwd_return_intraday"],
            "fwd_return_t1": t["fwd_return_t1"],
        })

    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 累计汇总 (全量重读 jsonl, 文件小)
    all_rows, bad = [], 0
    for line in OUT_JSONL.read_text(encoding="utf-8").splitlines():
        r = _loads(line)
        if r is None:
            bad += 1
            continue
        all_rows.append(r)
    if bad:
        print(f"WARNING: gap_alignment_shadow.jsonl 有 {bad} 行解析失败已跳过")
    agg = {}
    for r in all_rows:
        key = (r["direction"], r["gap_bucket"], r["window"])
        a = agg.setdefault(key, {"n": 0, "net": 0.0, "wins": 0,
                                 "fwd_intraday_sum": 0.0, "fwd_n": 0})
        a["n"] += 1
        a["net"] += float(r["pnl_net"] or 0)
        a["wins"] += 1 if (r["pnl_net"] or 0) > 0 else 0
        if r.get("fwd_return_intraday") is not None:
            a["fwd_intraday_sum"] += float(r["fwd_return_intraday"])
            a["fwd_n"] += 1
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_added": date, "rows_added": len(rows),
        "cumulative": {f"{k[0]}|{k[1]}|{k[2]}": {
            "n": v["n"], "net": round(v["net"], 2),
            "win_rate": round(v["wins"] / v["n"], 3) if v["n"] else None,
            "fwd_intraday_mean": round(v["fwd_intraday_sum"] / v["fwd_n"], 5) if v["fwd_n"] else None,
        } for k, v in sorted(agg.items())},
    }
    OUT_LATEST.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
