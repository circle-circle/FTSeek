#!/usr/bin/env python3
"""dd (drawdown_from_peak) 参数反事实扫描 (2026-07-29, 用户裁执行)。

背景: 捕获率 ~49% 跨市况稳定 (周一亏日/周二胜日均 ~50%), dd=0.8 锁利呼吸空间
是 08-07 裁决头号议题; be_shadow 顺风日攒样本慢 → 离线反事实补位。

方法 (一阶近似, 如实标注): 对近 N 日已平仓笔, 假设路径 entry→peak→回撤:
  dd 档 X% 的反事实出场价 = peak × (1∓X/100) (LONG 减/SHORT 加)
  - 若该价优于实际 exit → 反事实多拿 (dd 更宽时 peak 后回撤更深才出)
  - 若 peak 浮盈不足以覆盖 X% 回撤仍保住成本 → 按保本/实际出场近似
  忽略多峰路径与 dd 触发前的其他退出规则竞争 — 方向性扫描, 精确版走 L1 回放。
用法: PYTHONPATH=. ./venv/bin/python scripts/dd_param_scan.py [--days 14]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import now_et  # noqa: E402

DD_GRID = [0.6, 0.8, 1.0, 1.2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    since = (now_et().date() - timedelta(days=args.days)).isoformat()

    c = sqlite3.connect(f"file:{REPO}/data/ftseek_us.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT symbol, direction, entry_price, exit_price, quantity, realized_pnl, "
        "mfe_pct, exit_reason, metadata_json FROM trade_records "
        "WHERE trade_date >= ? AND source_type LIKE 'PLAYBOOK%' AND exit_time IS NOT NULL "
        "AND exit_price > 0", (since,)).fetchall()
    c.close()

    samples = []
    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        pk = m.get("peak_price")
        e = float(r["entry_price"] or 0)
        q = float(r["quantity"] or 0)
        if not pk or e <= 0 or q <= 0:
            continue
        samples.append({
            "short": str(r["direction"]).upper() in ("SHORT", "SELL_SHORT"),
            "entry": e, "exit": float(r["exit_price"]), "qty": q,
            "peak": float(pk), "pnl": float(r["realized_pnl"] or 0),
        })
    if not samples:
        print("无样本")
        return 1

    actual = sum(s["pnl"] for s in samples)
    mfe_usd = sum(abs(s["peak"] - s["entry"]) * s["qty"] for s in samples)
    print(f"样本 {len(samples)} 笔 (近 {args.days} 日) | 实际落袋 ${actual:+.1f} | "
          f"MFE 潜力 ${mfe_usd:.1f} | 实际捕获率 {actual/mfe_usd*100:.0f}%")
    print(f"\n{'dd档':<6} {'反事实落袋':<12} {'vs实际':<10} {'捕获率':<8} 假设")
    for dd in DD_GRID:
        cf_total = 0.0
        for s in samples:
            sign = -1 if s["short"] else 1
            # 反事实出场价: peak 回撤 dd%
            cf_exit = s["peak"] * (1 - dd / 100 * sign)
            cf_pnl = (cf_exit - s["entry"]) * s["qty"] * sign
            # 语义约束: 若 peak 浮盈本身 < dd (从未触发锁利), 该笔退化为实际结局
            peak_gain_pct = abs(s["peak"] - s["entry"]) / s["entry"] * 100
            if peak_gain_pct < dd:
                cf_pnl = s["pnl"]
            cf_total += cf_pnl
        print(f"{dd:<6} ${cf_total:+10.1f}  {cf_total-actual:+8.1f}  "
              f"{cf_total/mfe_usd*100:5.0f}%   {'(当前档)' if dd == 0.8 else ''}")
    print("\n注: 一阶近似 (entry→peak→回撤单峰路径), 忽略 dd 与其他退出规则竞争;"
          "\n    peak 浮盈<dd 档的笔按实际结局计。精确版待 L1 分钟级回放 (周五)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
