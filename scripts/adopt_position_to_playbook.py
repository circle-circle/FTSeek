#!/usr/bin/env python3
"""收编 broker 现有持仓到 Playbook 统一管理 (一次性数据修复).

背景 (2026-07-20):
  某些 broker 持仓 (如 EOG) 因 playbook 下单未落 trade_record (SUBMITTED 未成交/ghost),
  被 position_sync 误标 owner_strategy=auto_main → 主 ExitMonitor(ATR 移动止损) 接管,
  而非 playbook 监控 (%-drawdown-from-peak). 造成双重管理 + 刚开仓 ATR=0 止损过紧.

本脚本 (幂等, 仅补登记, 不下单/不平仓):
  1. 为目标 symbol 在 trade_records 插入一条 source_type=PLAYBOOK_SIM 的未平开仓记录
     (peak/trough 初始化=入场价, atr 从 SL 距离反推, stop_loss=当前 SL),
     使 is_playbook_open_position(symbol) 立即返回 True.
  2. 把 open_positions 该行的 owner_strategy/strategy_id/source 改为 playbook_sim,
     令 position_sync/主 ExitMonitor 下一轮自动跳过, playbook 监控接管.

用法:
  venv/bin/python scripts/adopt_position_to_playbook.py US.EOG --dry-run
  venv/bin/python scripts/adopt_position_to_playbook.py US.EOG
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

# 允许从 scripts/ 直接运行时导入 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="标的代码, 如 US.EOG")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的操作, 不写库")
    args = ap.parse_args()
    symbol = args.symbol.strip()

    from app.core.paths import get_trade_records_db
    import sqlite3

    db_path = str(get_trade_records_db())
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    # ── 读 open_positions 目标行 ──
    row = conn.execute(
        "SELECT * FROM open_positions WHERE symbol=?", (symbol,)
    ).fetchone()
    if row is None:
        print(f"[ERR] open_positions 无 {symbol} 记录, 无法收编 (broker 是否确有持仓?)")
        return 2
    pos = dict(row)

    # ── 幂等检查: 已有未平 PLAYBOOK_SIM 记录则跳过插入 ──
    existing = conn.execute(
        "SELECT id FROM trade_records WHERE symbol=? "
        "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
        "AND (exit_price=0 OR exit_price IS NULL OR exit_time='' OR exit_time IS NULL) "
        "LIMIT 1",
        (symbol,),
    ).fetchone()

    entry = float(pos.get("avg_entry_price") or 0)
    qty = int(float(pos.get("size") or 0))
    direction = str(pos.get("direction") or "LONG")
    cur_sl = float(pos.get("current_sl") or pos.get("initial_sl") or 0)
    init_sl = float(pos.get("initial_sl") or cur_sl or 0)
    # ATR 从入场 SL 距离反推 (playbook 退出用 %-drawdown, atr 仅诊断用途)
    atr_guess = abs(entry - init_sl) / 2.0 if (entry and init_sl) else 0.0
    now_iso = datetime.now().astimezone().isoformat()
    trace_id = f"adopt-{symbol}-{int(datetime.now().timestamp())}"

    print(f"=== 收编 {symbol} 到 Playbook ===")
    print(f"  open_positions: dir={direction} qty={qty} entry={entry} "
          f"initial_sl={init_sl} current_sl={cur_sl} "
          f"owner_strategy={pos.get('owner_strategy')} source={pos.get('source')}")

    if existing:
        print(f"  [幂等] 已存在未平 PLAYBOOK_SIM trade_record (id={existing['id']}), 跳过插入.")
    else:
        record = {
            "trace_id": trace_id,
            "trade_date": now_iso[:10],
            "symbol": symbol,
            "direction": direction,
            "entry_time": pos.get("entry_time") or now_iso,
            "exit_time": "",
            "entry_price": entry,
            "exit_price": 0,
            "quantity": qty,
            "realized_pnl": 0,
            "pnl_pct": 0,
            "exit_reason": "",
            "holding_hours": 0,
            "signal_strength": 50,
            "signal_confidence": 0.5,
            "futu_order_id": pos.get("sl_broker_order_id") or "",
            "source_type": "PLAYBOOK_SIM",
            "entry_reason": "adopt_to_playbook",
            "owner_strategy": "playbook_sim",
            "strategy_id": "playbook_sim",
            "market_regime": pos.get("market_regime") or "RANGE",
            "mode": "normal",
            "trade_mode": "sim",
            "signal_id": trace_id,
            "metadata_json": json.dumps({
                "stop_loss": cur_sl,
                "take_profit": float(pos.get("initial_tp") or 0),
                "atr": atr_guess,
                "peak_price": entry,
                "trough_price": entry,
                "entry_price_source": "adopt_from_open_positions",
                "adopted": True,
                "adopt_origin": pos.get("source") or "position_sync",
            }),
        }
        print(f"  将插入 trade_record: source_type=PLAYBOOK_SIM owner=playbook_sim "
              f"peak/trough={entry} atr≈{atr_guess:.4f} stop_loss={cur_sl}")
        if not args.dry_run:
            from app.storage.trade_store import TradeStore
            rid = TradeStore(db_path).save_trade(record)
            print(f"  [OK] 已插入 trade_record id={rid}")

    # ── 改 open_positions 归属为 playbook_sim ──
    print(f"  将更新 open_positions: owner_strategy/strategy_id/source → playbook_sim")
    if not args.dry_run:
        conn.execute(
            "UPDATE open_positions SET owner_strategy='playbook_sim', "
            "strategy_id='playbook_sim', source='playbook_sim', updated_at=? "
            "WHERE symbol=?",
            (now_iso, symbol),
        )
        conn.commit()
        print(f"  [OK] open_positions 已改归 playbook_sim")

    conn.close()

    if args.dry_run:
        print("\n[DRY-RUN] 未写任何数据. 去掉 --dry-run 实际执行.")
    else:
        print(f"\n[DONE] {symbol} 已收编到 Playbook. 下一轮同步/重启后:")
        print("  - is_playbook_open_position() → True")
        print("  - 主 ExitMonitor 自动跳过, playbook 监控接管")
        print("  - 不再出现 [ATR底限] 告警")
        print(f"\n[UNDO] 如需回滚: DELETE FROM trade_records WHERE trace_id='{trace_id}';")
        print(f"  并将 open_positions.{symbol} owner_strategy 改回 auto_main.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
