#!/usr/bin/env python3
"""统一决策视图 (DEB-20260729-position-path-lake P1, 只读)。

按 decision_id 一行一决策: 指令 (状态) × 成交 (动作/结果) × 路径文件指针 × shadow 反事实。
读侧聚合, 零写入 — 联邦存储 + 单一身份 (decision_id) 的物化查询层。
用法:
  PYTHONPATH=. ./venv/bin/python scripts/decision_view.py [--days 14] [--json]
  PYTHONPATH=. ./venv/bin/python scripts/decision_view.py --id DA-20260728... (单决策全链)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import now_et  # noqa: E402

LAKE = REPO / "data" / "position_paths"


def _san(did: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", did)[:180]


def _load_shadow_index() -> dict:
    """shadow jsonl → symbol+date 索引 (shadow 记录无 decision_id, 用 symbol+ts 日期近似挂载)."""
    idx: dict = {}
    for name in ("high_atr_stall_shadow", "breakeven_shadow"):
        f = REPO / "data" / f"{name}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
                key = (r.get("symbol"), str(r.get("ts", ""))[:10])
                idx.setdefault(key, []).append({"kind": name, **{k: r[k] for k in r if k != "symbol"}})
            except Exception:
                pass
    return idx


def build(days: int) -> list[dict]:
    since = (now_et().date() - timedelta(days=days)).isoformat()
    c = sqlite3.connect(f"file:{REPO}/data/ftseek_us.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    insts = {r["decision_id"]: dict(r) for r in c.execute(
        "SELECT decision_id, date, symbol, direction, confidence, entry_low, entry_high, "
        "executed, triggered, skip_reason, vol_bucket, atr_pct FROM playbook_instructions "
        "WHERE date >= ? AND decision_id IS NOT NULL", (since,))}
    trades = c.execute(
        "SELECT decision_id, id, symbol, direction, trade_date, entry_price, exit_price, "
        "realized_pnl, pnl_pct, mfe_pct, mae_pct, exit_reason, entry_time, exit_time, "
        "entry_vix, entry_session, config_version FROM trade_records "
        "WHERE trade_date >= ? AND source_type LIKE 'PLAYBOOK%'", (since,)).fetchall()
    c.close()
    shadow_idx = _load_shadow_index()

    out = []
    seen_dids = set()
    for t in trades:
        did = t["decision_id"] or f"trade_{t['id']}"
        seen_dids.add(did)
        inst = insts.get(did) or {}
        pf = LAKE / f"{_san(did)}.json"
        out.append({
            "decision_id": did, "symbol": t["symbol"], "direction": t["direction"],
            "date": t["trade_date"],
            "state": {"confidence": inst.get("confidence"), "vol_bucket": inst.get("vol_bucket"),
                      "atr_pct": inst.get("atr_pct"), "entry_vix": t["entry_vix"],
                      "entry_session": t["entry_session"],
                      "entry_band": [inst.get("entry_low"), inst.get("entry_high")]},
            "action": {"entry_price": t["entry_price"], "entry_time": t["entry_time"],
                       "config_version": t["config_version"]},
            "outcome": {"exit_price": t["exit_price"], "exit_reason": t["exit_reason"],
                        "realized_pnl": t["realized_pnl"], "pnl_pct": t["pnl_pct"],
                        "mfe_pct": t["mfe_pct"], "mae_pct": t["mae_pct"]},
            "path_file": str(pf) if pf.exists() else None,
            "shadows": shadow_idx.get((t["symbol"], str(t["trade_date"])), []),
        })
    # 未成交指令 (有决策无动作 — 漏斗上半段)
    for did, inst in insts.items():
        if did not in seen_dids:
            out.append({
                "decision_id": did, "symbol": inst["symbol"], "direction": inst["direction"],
                "date": inst["date"],
                "state": {"confidence": inst.get("confidence"), "vol_bucket": inst.get("vol_bucket")},
                "action": None,
                "outcome": {"executed": inst.get("executed"), "triggered": inst.get("triggered"),
                            "skip_reason": inst.get("skip_reason")},
                "path_file": None, "shadows": [],
            })
    out.sort(key=lambda x: (x["date"], x["decision_id"]), reverse=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--id", type=str, default="")
    args = ap.parse_args()
    rows = build(args.days)
    if args.id:
        rows = [r for r in rows if args.id in r["decision_id"]]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
        return 0
    n_trade = sum(1 for r in rows if r["action"])
    n_path = sum(1 for r in rows if r["path_file"])
    print(f"决策视图: {len(rows)} 决策 (近 {args.days} 日) | 成交 {n_trade} | 路径覆盖 {n_path}/{n_trade} "
          f"(G5 join 完整率 {n_path/n_trade*100:.0f}%)" if n_trade else f"{len(rows)} 决策, 零成交")
    for r in rows[:40]:
        if r["action"]:
            o = r["outcome"]
            print(f"  {r['date']} {r['symbol'][3:]:<6} {r['direction'][:5]:<5} "
                  f"pnl={o['realized_pnl']:>8} {(o['exit_reason'] or '')[:14]:<14} "
                  f"path={'✓' if r['path_file'] else '✗'} shadows={len(r['shadows'])} {r['decision_id'][:36]}")
        else:
            print(f"  {r['date']} {r['symbol'][3:]:<6} {r['direction'][:5]:<5} [未成交] "
                  f"skip={str(r['outcome'].get('skip_reason') or '')[:28]} {r['decision_id'][:36]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
