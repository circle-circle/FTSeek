#!/usr/bin/env python3
"""持仓路径湖采集器 (DEB-20260729-position-path-lake P0, T2 修正版: 盘后批量, 用户裁执行)。

设计 (§九 辩论修正): 不挂平仓钩子 — 盘后 cron 批量采当日 (或 --days N 存量) 平仓笔的
分钟级路径, 与退出链路零交集 (G2 自动满足)。复用 exit_replay_backtest.fetch_bars
(缓存 replay_bars/ + AuType.NONE + 1.2s 限流 + 空重试), 顺带补 L1 回放当日缓存。

落盘: data/position_paths/{decision_id_sanitized}.json —
  meta (decision_id/symbol/direction/entry/exit/peak/config_version/exit_reason/vol_bucket)
  + bars (窗口: entry-30min → min(exit+30min, 16:00); 隔夜仓跨日采两日, 声明 §九补丁2)
幂等: 文件存在跳过。失败: 单笔 WARNING 不中断批次。
cron: 35 4 * * 2-6 (CST 周二至六凌晨 = ET 周一至五盘后 16:35, CST×ET 纪律)。
用法: PYTHONPATH=. ./venv/bin/python scripts/position_path_collector.py [--days 7]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from app.utils.timezone import ET, now_et  # noqa: E402

OUT_DIR = REPO / "data" / "position_paths"


def _sanitize(decision_id: str) -> str:
    """decision_id 含 ':' '.' 等 (如 universe_expansion:2026-07-28:US.TSM) → 安全文件名."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", decision_id)[:180]


def load_closed(since: str) -> list[dict]:
    c = sqlite3.connect(f"file:{REPO}/data/ftseek_us.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, decision_id, symbol, direction, trade_date, entry_time, exit_time, "
        "entry_price, exit_price, quantity, realized_pnl, mfe_pct, mae_pct, exit_reason, "
        "config_version, metadata_json FROM trade_records "
        "WHERE trade_date >= ? AND source_type LIKE 'PLAYBOOK%' "
        "AND exit_time IS NOT NULL AND exit_price > 0", (since,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="回看天数 (首跑存量用 7)")
    args = ap.parse_args()
    since = (now_et().date() - timedelta(days=args.days)).isoformat()

    recs = load_closed(since)
    todo = []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for r in recs:
        did = r["decision_id"] or f"trade_{r['id']}"
        fp = OUT_DIR / f"{_sanitize(did)}.json"
        if not fp.exists():
            todo.append((r, did, fp))
    print(f"[path-lake] 平仓笔 {len(recs)} (自 {since}), 待采 {len(todo)} (幂等跳过 {len(recs)-len(todo)})")
    if not todo:
        return 0

    from exit_replay_backtest import fetch_bars  # 复用: 缓存+NONE+限流+重试
    from futu import OpenQuoteContext
    import os
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    ok = fail = empty = 0
    try:
        for r, did, fp in todo:
            try:
                # naive ET 口径 (库内 entry/exit_time 已统一 naive ET)
                et_entry = datetime.fromisoformat(str(r["entry_time"]).replace("+08:00", ""))
                et_exit = datetime.fromisoformat(str(r["exit_time"]))
                if et_entry.tzinfo:
                    et_entry = et_entry.replace(tzinfo=None)
                if et_exit.tzinfo:
                    et_exit = et_exit.replace(tzinfo=None)
                days = sorted({et_entry.date().isoformat(), et_exit.date().isoformat()})
                bars_all = []
                for day in days:  # 隔夜仓跨日采两日 (§九补丁2)
                    b = fetch_bars(ctx, r["symbol"], day)
                    if b:
                        bars_all.extend(b)
                if not bars_all:
                    empty += 1
                    print(f"  [empty] {r['symbol']} {days} 无 bar (休市/退市合法空, G1 分母排除)")
                    continue
                # 窗口: entry-30min → exit+30min (bar 天然止于 16:00, §九补丁: 取早者自动成立)
                w_lo = (et_entry - timedelta(minutes=30))
                w_hi = (et_exit + timedelta(minutes=30))
                win = [b for b in bars_all
                       if w_lo <= b["t"].replace(tzinfo=None) <= w_hi]
                m = json.loads(r["metadata_json"] or "{}")
                doc = {
                    "decision_id": did, "trade_id": r["id"], "symbol": r["symbol"],
                    "direction": r["direction"], "trade_date": r["trade_date"],
                    "entry_time": str(r["entry_time"]), "exit_time": str(r["exit_time"]),
                    "entry_price": r["entry_price"], "exit_price": r["exit_price"],
                    "quantity": r["quantity"], "realized_pnl": r["realized_pnl"],
                    "mfe_pct": r["mfe_pct"], "mae_pct": r["mae_pct"],
                    "exit_reason": r["exit_reason"], "config_version": r["config_version"],
                    "vol_bucket": (m.get("attribution") or {}).get("vol_bucket") or m.get("vol_bucket"),
                    "peak_price": m.get("peak_price"), "trough_price": m.get("trough_price"),
                    "autype": "NONE",  # §九补丁1: 原始价口径显式声明
                    "window": [w_lo.isoformat(), w_hi.isoformat()],
                    "bars": [[b["t"].strftime("%Y-%m-%d %H:%M:%S"), b["o"], b["h"], b["l"], b["c"]]
                             for b in win],
                    "collected_at": now_et().isoformat(),
                }
                fp.write_text(json.dumps(doc, ensure_ascii=False))
                ok += 1
                print(f"  [ok] {r['symbol']} {did[:40]} bars={len(win)}")
            except Exception as e:
                fail += 1
                print(f"  [WARNING] {r['symbol']} 采集失败: {e}")
    finally:
        ctx.close()
    print(f"[path-lake] 完成: ok={ok} empty={empty} fail={fail} | "
          f"G1 覆盖率(排除合法空)={ok}/{ok+fail} ")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
