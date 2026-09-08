#!/usr/bin/env python3
"""实盘券商台账回补 (RFC-078 批B cron 入口, 港股 + 美股).

把券商侧**逐笔成交流水**与**当日持仓快照**落进 live 库两张表
(`live_deal_records` / `live_position_snapshots`), 供实盘周报复盘消费。
这层账本**不依赖下单代码是否记得写** —— 本地 `trade_records` 曾因委派签名漂移
断链 2026-06-19~09-08 (券商已平仓、账本零记录), 券商真值层是唯一能回补的载体。

纯幂等回补: 成交按 `deal_id` upsert, 快照按 `(snapshot_date, market, symbol)` upsert
⇒ 反复跑、漏跑补跑、两市场腿返回重叠集合都安全。

退出码契约 (给 cron / 守卫脚本用, 不靠 stdout 猜):
  0 = 两市场两腿均 OK / NO_DEALS / NO_POSITIONS (业务空窗属正常)
  3 = 至少一腿 UNAVAILABLE (券商不可达/查询失败) —— **不是**"零成交", 必须告警
  2 = 脚本自身异常

用法:
  PYTHONPATH=. venv/bin/python scripts/live_ledger_sync.py [--days 30] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="实盘券商台账回补 (港股+美股)")
    ap.add_argument("--days", type=int, default=30, help="成交回补窗口天数 (默认 30)")
    ap.add_argument("--quiet", action="store_true", help="只在非 OK 时输出")
    args = ap.parse_args()

    try:
        from app.web_dashboard import live_ledger
    except Exception as exc:  # noqa: BLE001
        print(f"[live_ledger_sync] 模块导入失败: {exc}", file=sys.stderr)
        return 2

    try:
        result = live_ledger.sync_all(days=args.days)
    except Exception as exc:  # noqa: BLE001
        print(f"[live_ledger_sync] 同步异常: {exc}", file=sys.stderr)
        return 2

    overall = result.get("overall")
    if not args.quiet or overall != "OK":
        slim = {
            "overall": overall,
            "deals": {k: v for k, v in result["deals"].items() if k != "legs"},
            "deals_legs": result["deals"].get("legs"),
            "positions": {k: v for k, v in result["positions"].items()},
            "coverage": live_ledger.ledger_coverage(),
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2, default=str))

    if overall == "UNAVAILABLE":
        print("[live_ledger_sync] 🚨 全腿 UNAVAILABLE — 券商不可达, 台账本次未更新 "
              "(这**不是**零成交/空仓)", file=sys.stderr)
        return 3
    if overall == "PARTIAL":
        print(f"[live_ledger_sync] ⚠ 部分腿不可达: deals={result['deals'].get('unavailable_legs')} "
              f"positions={result['positions'].get('unavailable_legs')}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
