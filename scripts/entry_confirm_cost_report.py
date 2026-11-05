#!/usr/bin/env python3
"""entry_confirm 真实成本读数 (只读) — 确认价→实际成交价的滑点 + "确认但没成交" 盲区.

为什么需要它 (2026-11-05):
  门自己只记录"确认时刻的价格"(px_at_confirm), 而**真实成交价**是确认后下一轮的
  reprice 结果。两者之差就是这门最核心的代价 (回放口径估 18.5bps 均值), 但 live
  侧从未量过; 同时"确认通过却因别的门没下单"的行在结算器里只会挂在 unresolved,
  看不出原因。本脚本把这两件事一起量出来, 零交易行为变更。

口径:
  · 人群: data/shadow/entry_confirm_forward.jsonl 中 arm=treatment ∧ status=ok ∧
    still_triggers=true 的行 (即门放行的笔)。
  · 实际成交: trade_records 按 decision_id 对齐 (source_type='PLAYBOOK_SIM')。
  · 滑点 cost_bps = 方向归一后的 (entry_price − px_at_confirm)/px_at_confirm × 1e4,
    正 = 确认让你成交更贵 (这是门的成本, 不是毛利的成本)。
  · 确认未成交: 有确认行但 trade_records 无对应成交 ⇒ 列原因
    (playbook_instructions.skip_reason / executed 状态), 不静默丢。

用法: PYTHONPATH=. venv/bin/python scripts/entry_confirm_cost_report.py [--days 20]
产物: data/shadow/entry_confirm_cost_latest.json (供看板/日报挂载)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORWARD = REPO / "data" / "shadow" / "entry_confirm_forward.jsonl"
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "entry_confirm_cost_latest.json"


def _rows(days: int):
    if not FORWARD.exists():
        return [], "NO_FILE"
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for line in FORWARD.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("arm") != "treatment" or r.get("status") != "ok":
            continue
        if not r.get("still_triggers"):
            continue
        if str(r.get("date") or "") < cutoff:
            continue
        out.append(r)
    return out, ("OK" if out else "TOO_EARLY")


def _trades():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {str(r["decision_id"]): dict(r) for r in con.execute(
            "SELECT decision_id, symbol, trade_date, entry_price, entry_time, "
            "       COALESCE(pnl_net, realized_pnl) AS net, exit_reason "
            "  FROM trade_records "
            " WHERE source_type='PLAYBOOK_SIM' AND decision_id IS NOT NULL "
            "   AND COALESCE(entry_price,0) > 0")}
    finally:
        con.close()


def _skips(ids):
    if not ids:
        return {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ",".join("?" for _ in chunk)
            for r in con.execute(
                    f"SELECT decision_id, executed, direction, skip_reason "
                    f"  FROM playbook_instructions WHERE decision_id IN ({q})", chunk):
                out[str(r["decision_id"])] = dict(r)
    finally:
        con.close()
    return out


def _cost_bps(direction, confirm_px, fill_px):
    if not confirm_px or not fill_px:
        return None
    raw = (fill_px - confirm_px) / confirm_px * 1e4
    return raw if str(direction).upper() == "LONG" else -raw


def _agg(vals):
    if not vals:
        return None
    s = sorted(vals)
    # n 很小时 int(0.9*len) 会取到"下一位", 把 P90 报成中位以下 ⇒ 用 round 并夹紧
    idx = min(len(s) - 1, int(round(0.9 * (len(s) - 1))))
    return {"n": len(vals), "mean": round(st.mean(vals), 2),
            "median": round(st.median(vals), 2),
            "p90": round(s[idx], 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    args = ap.parse_args()

    rows, state = _rows(args.days)
    if state != "OK":
        print(f"[entry_confirm_cost] 状态={state} — 无确认放行行 (窗未开/未产生), "
              f"不得据此判断门无效。文件={FORWARD}")
        return

    trades = _trades()
    ids = [str(r.get("decision_id")) for r in rows if r.get("decision_id")]
    skips = _skips(ids)

    matched, missing = [], []
    for r in rows:
        did = str(r.get("decision_id") or "")
        t = trades.get(did)
        if t:
            matched.append((r, t))
        else:
            missing.append(r)

    costs = defaultdict(list)
    by_dir = defaultdict(list)
    for r, t in matched:
        bps = _cost_bps(r.get("direction"), r.get("px_at_confirm"), t.get("entry_price"))
        if bps is None:
            continue
        costs["all"].append(bps)
        by_dir[str(r.get("direction"))].append(bps)
        by_dir[f"arch={r.get('entry_archetype') or '-'}"].append(bps)
        sm = r.get("session_min")
        if isinstance(sm, int):
            bucket = "open<15m" if sm < 15 else ("15-90m" if sm < 90 else ">90m")
            by_dir[f"session={bucket}"].append(bps)

    miss_reason = defaultdict(int)
    for r in missing:
        row = skips.get(str(r.get("decision_id") or "")) or {}
        reason = (row.get("skip_reason") or "").strip()
        if not reason and row:
            reason = f"executed={row.get('executed')} direction={row.get('direction')}"
        miss_reason[reason[:60] or "(指令行未找到)"] += 1

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": args.days,
        "confirmed_released_n": len(rows),
        "executed_n": len(matched),
        "confirmed_not_executed_n": len(missing),
        "confirmed_not_executed_reasons": dict(miss_reason),
        "confirm_to_fill_cost_bps": _agg(costs["all"]),
        "by_bucket": {k: _agg(v) for k, v in sorted(by_dir.items())},
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    c = report["confirm_to_fill_cost_bps"]
    print(f"确认放行 {len(rows)} / 实际成交 {len(matched)} / 未成交 {len(missing)}")
    if c:
        print(f"确认价→成交价 cost: 均值 {c['mean']:+.2f}bps / 中位 {c['median']:+.2f} "
              f"/ P90 {c['p90']:+.2f} (正=确认让你更贵, n={c['n']})")
    else:
        print("确认价→成交价 cost: 无可用配对 (缺 px_at_confirm 或未成交)")
    for k, v in report["by_bucket"].items():
        if v and v["n"] >= 5:
            print(f"  {k:<26} n={v['n']:<3} 均值 {v['mean']:+.2f}bps 中位 {v['median']:+.2f}")
    if miss_reason:
        print("确认未成交原因:", dict(miss_reason))
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()