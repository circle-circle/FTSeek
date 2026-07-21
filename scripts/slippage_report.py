#!/usr/bin/env python3
"""
滑点分布报告 (Slippage Report)

从 trade_records.metadata_json.execution 读取真实滑点数据 (P0 落库后产生),
按标的 / 时段 / 方向分层统计滑点 bps 分布 (均值 / p50 / p90 / p99)。

用途: 切实盘前用真实数据校准 cost_model / simulate_slippage_bps。
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_path():
    try:
        from app.core.paths import get_trade_records_db
        return str(get_trade_records_db())
    except Exception:
        return os.path.join(os.path.dirname(__file__), "..", "data", "ftseek_us.db")


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _summ(name, vals):
    if not vals:
        print(f"  {name:20s} 无数据")
        return
    avg = sum(vals) / len(vals)
    print(
        f"  {name:20s} n={len(vals):4d}  "
        f"avg={avg:+6.1f}  p50={_pct(vals,50):+6.1f}  "
        f"p90={_pct(vals,90):+6.1f}  p99={_pct(vals,99):+6.1f} bps"
    )


def main():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, direction, entry_session, trade_mode, metadata_json "
        "FROM trade_records WHERE metadata_json LIKE '%\"execution\"%'"
    ).fetchall()
    conn.close()

    all_bps = []
    by_symbol = defaultdict(list)
    by_session = defaultdict(list)
    by_direction = defaultdict(list)
    by_mode = defaultdict(list)

    for r in rows:
        try:
            meta = json.loads(r["metadata_json"])
        except Exception:
            continue
        execu = meta.get("execution") or {}
        bps = execu.get("slippage_bps")
        if bps is None:
            continue
        bps = float(bps)
        all_bps.append(bps)
        by_symbol[r["symbol"]].append(bps)
        by_session[r["entry_session"] or "?"].append(bps)
        by_direction[r["direction"] or "?"].append(bps)
        by_mode[r["trade_mode"] or "?"].append(bps)

    print("=" * 72)
    print(f"滑点分布报告 — 共 {len(all_bps)} 笔含滑点数据 (正=不利)")
    print("=" * 72)
    if not all_bps:
        print("\n暂无滑点数据。P0 落库后, 新交易会开始记录 arrival→fill 滑点。")
        return

    print("\n[总体]")
    _summ("ALL", all_bps)

    print("\n[按交易模式]")
    for k in sorted(by_mode):
        _summ(k, by_mode[k])

    print("\n[按方向]")
    for k in sorted(by_direction):
        _summ(k, by_direction[k])

    print("\n[按时段]")
    for k in sorted(by_session):
        _summ(k, by_session[k])

    print("\n[按标的 Top15 (笔数)]")
    for sym in sorted(by_symbol, key=lambda s: -len(by_symbol[s]))[:15]:
        _summ(sym, by_symbol[sym])


if __name__ == "__main__":
    main()
