#!/usr/bin/env python3
"""Shadow Mirror EOD Replay (2026-05-19)

每日 04:30 ET (= 16:30 北京) 跑一次, 把所有未关闭的 non-baseline shadow 按各 variant 规则
用真实价格回放, 写入 closed=1 + realized_pnl.

第一性: 没这个 batch, 7/8 variant 永远 closed=0, A/B 对比毫无意义.

支持的 8 个 variant 规则:
- baseline:            已被 mirror_exit 实时关闭, 不需 EOD
- tighter_stop:        SL 收紧 50%
- no_chandelier:       仅 ATR 止损, 不 trail
- quick_exit:          30 min 强制平仓
- tp_sl_5to1:          TP=2.5% SL=0.5% (P3)
- min_hold_6min:       入场后强制 ≥ 6min 才止损
- short_only:          mirror_entry 已过滤 LONG, 此处复用 baseline 逻辑
- early_chandelier:    Chandelier 0.5 ATR 激活 (P7)

用法:
  python scripts/shadow_eod_replay.py                # 默认: 关闭今日未平仓
  python scripts/shadow_eod_replay.py --days 7      # 关闭过去 7 天
  python scripts/shadow_eod_replay.py --dry-run    # 仅显示, 不写 DB
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("ftseek.shadow_eod")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _query_unclosed_shadows(days_back: int) -> List[Dict]:
    db = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    cur.execute("""
        SELECT trade_id, symbol, strategy, direction, signal_detail,
               entry_time, entry_price, quantity
        FROM shadow_trades
        WHERE closed = 0
          AND entry_time >= ?
          AND strategy != 'baseline'
        ORDER BY entry_time
    """, (cutoff,))
    rows = cur.fetchall()
    conn.close()
    cols = ["trade_id", "symbol", "strategy", "direction", "signal_detail",
            "entry_time", "entry_price", "quantity"]
    return [dict(zip(cols, r)) for r in rows]


def _get_real_trade_outcome(real_trace_id: str) -> Optional[Dict]:
    """查 real trade_records 拿真实退出价格 + 时间 (作为 EOD replay 的参考).

    KISS: 用 real 的 exit_price 作为终点 (而非拉 intraday 价格序列).
    """
    db = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("""
        SELECT exit_time, exit_price, holding_hours, exit_reason
        FROM trade_records
        WHERE trace_id = ? AND exit_price IS NOT NULL
        LIMIT 1
    """, (real_trace_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "exit_time": row[0],
        "exit_price": float(row[1] or 0),
        "holding_hours": float(row[2] or 0),
        "exit_reason": row[3] or "",
    }


def _apply_variant_logic(shadow: Dict, real_outcome: Optional[Dict]) -> Optional[Dict]:
    """按 variant 规则计算 shadow 的 exit_price + reason."""
    if not real_outcome:
        return None

    variant = shadow["strategy"]
    entry_price = float(shadow["entry_price"])
    quantity = int(shadow["quantity"])
    direction = shadow["direction"]
    real_exit = real_outcome["exit_price"]
    real_hold_hrs = real_outcome["holding_hours"]

    # 默认: 用 real 的 exit_price (baseline 路径)
    exit_price = real_exit
    exit_reason = f"variant_{variant}"

    if variant == "tighter_stop":
        # SL 收紧 50% (cost × 0.95 LONG / × 1.05 SHORT)
        tight_sl = entry_price * 0.95 if direction == "LONG" else entry_price * 1.05
        if direction == "LONG":
            exit_price = max(real_exit, tight_sl) if real_exit < entry_price else real_exit
        else:
            exit_price = min(real_exit, tight_sl) if real_exit > entry_price else real_exit
        exit_reason = "tighter_sl_replay"

    elif variant == "no_chandelier":
        # 不用 chandelier trail, 复用 real 退出 (近似)
        exit_price = real_exit
        exit_reason = "no_chandelier_replay"

    elif variant == "quick_exit":
        # 30 min 强制平仓: 如果 real_hold > 0.5h, 假设 30min 时价格是中点
        if real_hold_hrs > 0.5:
            mid_factor = 0.5 / max(real_hold_hrs, 0.5)
            price_drift = (real_exit - entry_price) * mid_factor
            exit_price = entry_price + price_drift
        exit_reason = "quick_exit_30min"

    elif variant == "tp_sl_5to1":
        # TP 2.5% / SL 0.5% — 按 real 价格曲线判断
        if direction == "LONG":
            tp_price = entry_price * 1.025
            sl_price = entry_price * 0.995
            if real_exit >= tp_price:
                exit_price, exit_reason = tp_price, "tp_5to1_hit"
            elif real_exit <= sl_price:
                exit_price, exit_reason = sl_price, "sl_5to1_hit"
            else:
                exit_price, exit_reason = real_exit, "tp_sl_no_hit"
        else:
            tp_price = entry_price * 0.975
            sl_price = entry_price * 1.005
            if real_exit <= tp_price:
                exit_price, exit_reason = tp_price, "tp_5to1_hit"
            elif real_exit >= sl_price:
                exit_price, exit_reason = sl_price, "sl_5to1_hit"
            else:
                exit_price, exit_reason = real_exit, "tp_sl_no_hit"

    elif variant == "min_hold_6min":
        # 强制 ≥ 6min: 如果 real_hold < 6min, 假设线性外推
        if real_hold_hrs < 0.1:
            price_drift = (real_exit - entry_price) * 1.5
            exit_price = entry_price + price_drift
            exit_reason = "min_hold_extended"
        else:
            exit_price = real_exit
            exit_reason = "min_hold_normal"

    elif variant == "short_only":
        # mirror_entry 已过滤 LONG, 此处仅 SHORT 笔, 复用 baseline 逻辑
        exit_price = real_exit
        exit_reason = "short_only_baseline"

    elif variant == "early_chandelier":
        # Chandelier 0.5 ATR 激活: 提前锁利保留 80%
        if direction == "LONG" and real_exit > entry_price:
            exit_price = entry_price + (real_exit - entry_price) * 0.8
        elif direction == "SHORT" and real_exit < entry_price:
            exit_price = entry_price - (entry_price - real_exit) * 0.8
        else:
            exit_price = real_exit
        exit_reason = "early_chandelier_replay"

    # 计算 PnL
    if direction == "LONG":
        realized_pnl = (exit_price - entry_price) * quantity
    else:
        realized_pnl = (entry_price - exit_price) * quantity
    pnl_pct = realized_pnl / (entry_price * quantity) if entry_price * quantity > 0 else 0
    holding_minutes = real_hold_hrs * 60.0

    return {
        "exit_price": exit_price,
        "exit_time": real_outcome["exit_time"],
        "exit_reason": exit_reason,
        "realized_pnl": realized_pnl,
        "pnl_pct": pnl_pct,
        "holding_minutes": holding_minutes,
    }


def replay_shadows(days_back: int = 1, dry_run: bool = False) -> Dict:
    """主入口: 回放未平仓 shadow trades."""
    logger.info(f"[ShadowEOD] 开始回放, days_back={days_back}, dry_run={dry_run}")

    unclosed = _query_unclosed_shadows(days_back)
    logger.info(f"[ShadowEOD] 找到 {len(unclosed)} 笔未平仓 shadow")

    if not unclosed:
        return {"total": 0, "closed": 0, "skipped": 0, "by_variant": {}}

    by_variant: Dict[str, Dict] = {}

    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = None
    cur = None
    if not dry_run:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

    closed_count = 0
    skipped_count = 0

    for shadow in unclosed:
        variant = shadow["strategy"]
        by_variant.setdefault(variant, {"n_total": 0, "n_closed": 0, "pnl_sum": 0})
        by_variant[variant]["n_total"] += 1

        try:
            real_trace_id = json.loads(shadow["signal_detail"] or "{}").get("real_trace_id")
        except Exception:
            real_trace_id = None
        if not real_trace_id:
            skipped_count += 1
            continue
        real = _get_real_trade_outcome(real_trace_id)
        if not real:
            skipped_count += 1
            continue

        outcome = _apply_variant_logic(shadow, real)
        if not outcome:
            skipped_count += 1
            continue

        if dry_run:
            print(f"  [DRY] {variant} {shadow['symbol']} {shadow['direction']} "
                  f"entry=${shadow['entry_price']} exit=${outcome['exit_price']:.2f} "
                  f"pnl=${outcome['realized_pnl']:.0f} reason={outcome['exit_reason']}")
        else:
            cur.execute("""
                UPDATE shadow_trades SET
                  exit_time = ?, exit_price = ?, realized_pnl = ?, pnl_pct = ?,
                  exit_reason = ?, closed = 1, holding_minutes = ?
                WHERE trade_id = ?
            """, (
                outcome["exit_time"], outcome["exit_price"],
                outcome["realized_pnl"], outcome["pnl_pct"],
                outcome["exit_reason"], outcome["holding_minutes"],
                shadow["trade_id"],
            ))

        by_variant[variant]["n_closed"] += 1
        by_variant[variant]["pnl_sum"] += outcome["realized_pnl"]
        closed_count += 1

    if not dry_run:
        conn.commit()
        conn.close()

    logger.info(f"[ShadowEOD] ✅ 完成: closed={closed_count} skipped={skipped_count}")
    print("\n=== Variant 回放结果 ===")
    for variant, stats in sorted(by_variant.items()):
        print(f"  {variant:25s}: {stats['n_closed']}/{stats['n_total']} closed, "
              f"pnl=${stats['pnl_sum']:.0f}")

    return {
        "total": len(unclosed),
        "closed": closed_count,
        "skipped": skipped_count,
        "by_variant": by_variant,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shadow Mirror EOD Replay")
    parser.add_argument("--days", type=int, default=1, help="回放过去 N 天 (默认 1)")
    parser.add_argument("--dry-run", action="store_true", help="不写 DB")
    args = parser.parse_args()
    replay_shadows(days_back=args.days, dry_run=args.dry_run)
