#!/usr/bin/env python3
"""
策略健康度验证脚本 (2026-06-01)
==================================
用途: 每日开盘前/收盘后跑, 检查关键指标是否在合理范围.
触发条件: 任何指标突破阈值 → exit code != 0, 可接 systemd 或 cron 报警.

检查项:
  1. 90 天总胜率 (基线 27.7%, 盈亏平衡需 34%+)
  2. <30min 短命交易占比 (<25% 健康, 当前 18.7%)
  3. HIGH_VOL 市况入场亏损占比 (<200% 健康)
  4. SHORT_PROXY_OPTION 触发 (应该=0)
  5. voting_predictions 回填率 (应该 >50%)
  6. sandbox mode 亏损绝对值 (报警阈值 -$30K)

退出码: 0 = 全部健康, 1 = 有警告, 2 = 有 BLOCKING

用法:
  python3 scripts/validate_strategy_health.py
  python3 scripts/validate_strategy_health.py --json   # 输出 JSON
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

# 阈值定义 (基于 2026-06-01 数据驱动 review)
THRESHOLDS = {
    "win_rate_pct_min": 25.0,           # 90 天胜率 < 25% 警告
    "win_rate_pct_blocking": 20.0,      # 90 天胜率 < 20% blocking
    "short_trade_pct_max": 30.0,        # <30min 持仓占比 > 30% 警告
    "high_vol_loss_max": -50000.0,      # HIGH_VOL PnL < -$50K blocking
    "short_proxy_max_n": 0,             # SHORT_PROXY_OPTION 应为 0 (期权已禁)
    "voting_backfill_pct_min": 50.0,    # 回填率 < 50% 警告
    "sandbox_loss_max": -30000.0,       # sandbox PnL < -$30K blocking
}

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ftseek_us.db"


def query_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"❌ DB 不存在: {DB_PATH}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(DB_PATH))
    metrics: Dict[str, Any] = {}
    warnings: List[str] = []
    blockings: List[str] = []

    # 1. 总胜率 (90d)
    n = query_one(conn, "SELECT COUNT(*) FROM trade_records WHERE entry_time >= datetime('now', '-90 days')") or 0
    if n > 0:
        wr = query_one(conn, """
            SELECT AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100
            FROM trade_records WHERE entry_time >= datetime('now', '-90 days')
        """) or 0
        total_pnl = query_one(conn, """
            SELECT SUM(realized_pnl) FROM trade_records WHERE entry_time >= datetime('now', '-90 days')
        """) or 0
        metrics["win_rate_90d_pct"] = round(wr, 1)
        metrics["total_pnl_90d"] = round(total_pnl, 0)
        metrics["n_trades_90d"] = n
        if wr < THRESHOLDS["win_rate_pct_blocking"]:
            blockings.append(f"胜率 {wr:.1f}% < blocking 阈值 {THRESHOLDS['win_rate_pct_blocking']}%")
        elif wr < THRESHOLDS["win_rate_pct_min"]:
            warnings.append(f"胜率 {wr:.1f}% < 警告阈值 {THRESHOLDS['win_rate_pct_min']}%")

    # 2. <30min 短命交易占比
    n_short = query_one(conn, """
        SELECT COUNT(*) FROM trade_records
        WHERE entry_time >= datetime('now', '-90 days') AND holding_hours < 0.5
    """) or 0
    if n > 0:
        short_pct = (n_short / n) * 100
        metrics["short_lived_pct"] = round(short_pct, 1)
        if short_pct > THRESHOLDS["short_trade_pct_max"]:
            warnings.append(f"<30min 持仓占比 {short_pct:.1f}% > {THRESHOLDS['short_trade_pct_max']}%")

    # 3. HIGH_VOL 市况亏损
    hv_pnl = query_one(conn, """
        SELECT SUM(realized_pnl) FROM trade_records
        WHERE entry_time >= datetime('now', '-90 days') AND market_regime = 'HIGH_VOL'
    """) or 0
    metrics["high_vol_pnl"] = round(hv_pnl, 0)
    if hv_pnl < THRESHOLDS["high_vol_loss_max"]:
        blockings.append(f"HIGH_VOL PnL ${hv_pnl:.0f} < blocking 阈值 ${THRESHOLDS['high_vol_loss_max']:.0f}")

    # 4. SHORT_PROXY_OPTION 应该 0 (近 7 天为实时监控窗口)
    # 历史 5/20-5/22 期权灾难数据保留, 但不再实时报警
    n_proxy_7d = query_one(conn, """
        SELECT COUNT(*) FROM trade_records
        WHERE entry_time >= datetime('now', '-7 days') AND direction LIKE '%PROX%'
    """) or 0
    n_proxy_30d = query_one(conn, """
        SELECT COUNT(*) FROM trade_records
        WHERE entry_time >= datetime('now', '-30 days') AND direction LIKE '%PROX%'
    """) or 0
    metrics["short_proxy_7d_count"] = n_proxy_7d
    metrics["short_proxy_30d_count_historical"] = n_proxy_30d
    if n_proxy_7d > THRESHOLDS["short_proxy_max_n"]:
        blockings.append(f"SHORT_PROXY_OPTION 7d 触发 {n_proxy_7d} 笔 (期权应已禁用)")

    # 5. voting_predictions 回填率
    vp_total = query_one(conn, "SELECT COUNT(*) FROM voting_predictions") or 0
    vp_filled = query_one(conn, "SELECT COUNT(*) FROM voting_predictions WHERE outcome_match IS NOT NULL") or 0
    if vp_total > 0:
        backfill_pct = (vp_filled / vp_total) * 100
        metrics["voting_backfill_pct"] = round(backfill_pct, 1)
        metrics["voting_total"] = vp_total
        metrics["voting_filled"] = vp_filled
        if backfill_pct < THRESHOLDS["voting_backfill_pct_min"]:
            warnings.append(f"voting_predictions 回填率 {backfill_pct:.1f}% < {THRESHOLDS['voting_backfill_pct_min']}%")

    # 6. sandbox 模式亏损 (近 7 天为实时监控, 90 天作历史指标)
    # RFC-059 (2026-06-01) 已关闭 sandbox, 7 天窗口应该 = 0 笔
    sb_pnl_7d = query_one(conn, """
        SELECT SUM(realized_pnl) FROM trade_records
        WHERE entry_time >= datetime('now', '-7 days') AND mode = 'sandbox'
    """) or 0
    sb_pnl_90d = query_one(conn, """
        SELECT SUM(realized_pnl) FROM trade_records
        WHERE entry_time >= datetime('now', '-90 days') AND mode = 'sandbox'
    """) or 0
    metrics["sandbox_pnl_7d"] = round(sb_pnl_7d, 0)
    metrics["sandbox_pnl_90d_historical"] = round(sb_pnl_90d, 0)
    # blocking 阈值缩小到 7 天 -$5K (而非历史 90 天 -$30K)
    if sb_pnl_7d < -5000:
        blockings.append(f"sandbox 7d PnL ${sb_pnl_7d:.0f} < -$5,000 (RFC-059 已关闭, 出现新交易意味着配置回退)")

    # 输出
    if args.json:
        result = {
            "metrics": metrics,
            "warnings": warnings,
            "blockings": blockings,
            "exit_code": 2 if blockings else (1 if warnings else 0),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print("FTSeek-US 策略健康度报告")
        print("=" * 60)
        for k, v in metrics.items():
            print(f"  {k:30s} = {v}")
        print()
        if blockings:
            print("🔴 BLOCKING:")
            for b in blockings:
                print(f"  ❌ {b}")
        if warnings:
            print("🟡 WARNING:")
            for w in warnings:
                print(f"  ⚠️  {w}")
        if not blockings and not warnings:
            print("✅ 所有指标健康")
        print("=" * 60)

    conn.close()
    return 2 if blockings else (1 if warnings else 0)


if __name__ == "__main__":
    sys.exit(main())
