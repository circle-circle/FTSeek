"""审计日志/DB 写入/WebSocket 事件/手动持仓标记.

公开函数 (供 ManualOrderService facade 委派):
    - ensure_audit_table(service)
    - write_audit(service, order)
    - get_audit_history(service, limit) -> List[Dict]
    - record_manual_close(service, ...)
    - mark_position_manual(service, symbol, direction)
    - ensure_sl_column(service)
    - ensure_lifecycle_columns(service)
    - push_attribution_to_feishu(service, attr) -> bool
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:  # pragma: no cover
    from app.web_dashboard.manual_order_service import ManualOrder, ManualOrderService

logger = logging.getLogger("ftseek_us.manual_order_service.audit")


# ─────────────────────────────────────────────────────────────
# Schema 迁移 (幂等)
# ─────────────────────────────────────────────────────────────

def ensure_sl_column(service: "ManualOrderService") -> None:
    """运行时为 open_positions 添加 SL/TP broker 订单追踪列 (幂等).
    
    添加的列:
    - sl_broker_order_id: 止损单在 Futu 的订单 ID (实盘使用)
    - tp_broker_order_id: 止盈单在 Futu 的订单 ID (实盘使用)
    - current_sl: 当前止损价格 (模拟盘使用)
    - initial_tp: 初始止盈价格 (模拟盘使用)
    """
    columns_to_add = [
        ("sl_broker_order_id", "TEXT"),
        ("tp_broker_order_id", "TEXT"),
        ("current_sl", "REAL"),
        ("initial_tp", "REAL"),
    ]
    
    try:
        conn = service._db()
        for col_name, col_type in columns_to_add:
            try:
                conn.execute(f"ALTER TABLE open_positions ADD COLUMN {col_name} {col_type}")
                logger.info(f"[止损/止盈] open_positions 表已添加 {col_name} 列")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[止损/止盈] 添加追踪列失败 (不影响业务): {e}")


def ensure_lifecycle_columns(service: "ManualOrderService") -> None:
    """幂等确保 open_positions 表的 lifecycle/protection 列存在."""
    columns = [
        ("thesis", "TEXT"),
        ("invalidation_price", "REAL"),
        ("expected_hold_days", "INTEGER"),
        ("entry_context_json", "TEXT"),
        ("stop_loss_price", "REAL"),
        ("stop_loss_triggered", "INTEGER DEFAULT 0"),
        ("trailing_stop_price", "REAL"),
        ("trailing_stop_triggered", "INTEGER DEFAULT 0"),
    ]
    try:
        conn = service._db()
        for col_name, col_type in columns:
            try:
                conn.execute(f"ALTER TABLE open_positions ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[schema] lifecycle 列确保失败 (不影响业务): {e}")


def ensure_audit_table(service: "ManualOrderService") -> None:
    """确保审计表存在."""
    try:
        conn = service._db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_order_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT,
                action TEXT,
                quantity INTEGER,
                order_type TEXT,
                price REAL,
                reason TEXT,
                mode TEXT,
                order_id TEXT,
                status TEXT,
                error_msg TEXT,
                time_in_force TEXT DEFAULT 'DAY',
                fill_outside_rth INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_audit_time ON manual_order_audit(timestamp)"
        )
        # 兼容旧表: 尝试添加新列
        for col_sql in [
            "ALTER TABLE manual_order_audit ADD COLUMN time_in_force TEXT DEFAULT 'DAY'",
            "ALTER TABLE manual_order_audit ADD COLUMN fill_outside_rth INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_audit_symbol ON manual_order_audit(symbol)"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[审计] 创建审计表失败: {e}")


# ─────────────────────────────────────────────────────────────
# 审计写入 / 查询
# ─────────────────────────────────────────────────────────────

def write_audit(service: "ManualOrderService", order: "ManualOrder") -> None:
    """写审计日志到 DB."""
    try:
        ensure_audit_table(service)
        conn = service._db()
        conn.execute(
            """INSERT INTO manual_order_audit
               (timestamp, symbol, direction, action, quantity, order_type,
                price, reason, mode, order_id, status, error_msg,
                time_in_force, fill_outside_rth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.timestamp or datetime.now().isoformat(),
                order.symbol, order.direction, order.action,
                order.quantity, order.order_type, order.price,
                order.reason, order.mode, order.order_id,
                order.status, order.error_msg,
                order.time_in_force, 1 if order.fill_outside_rth else 0,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[审计] 写入失败: {e}")


def get_audit_history(service: "ManualOrderService", limit: int = 50) -> List[Dict]:
    """获取审计历史."""
    try:
        ensure_audit_table(service)
        conn = service._db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM manual_order_audit ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[审计] 查询失败: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# trade_records 闭环 / 飞书归因推送
# ─────────────────────────────────────────────────────────────

def push_attribution_to_feishu(service: "ManualOrderService", attr) -> bool:
    """推送归因报告到飞书."""
    try:
        import os
        from app.observability.feishu_bot import FeishuBotAlerter

        webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US")
        if not webhook_url:
            return False

        alerter = FeishuBotAlerter(webhook_url)

        # 构建消息
        pnl_emoji = "🟢" if attr["pnl"] >= 0 else "🔴"
        title = f"{pnl_emoji} FTSeek 平仓归因: {attr['symbol']}"

        # 主体内容
        content_lines = [
            f"{attr['direction']} | 盈亏: ${attr['pnl']:.2f} ({attr['pnl_pct']:.1f}%)",
            f"入场: ${attr['entry_price']:.2f} → 出场: ${attr['exit_price']:.2f}",
            f"持仓: {attr['holding_hours']:.1f}小时",
            "",
            "📊 归因分析:",
        ]

        for dim in attr["dimensions"]:
            icon = {"✅": "✅", "⚠️": "⚠️", "❌": "❌"}.get(dim["verdict"], "•")
            content_lines.append(f"{icon} {dim['name']}: {dim['detail']}")

        if attr.get("improvement"):
            content_lines.append("")
            content_lines.append(f"💡 改进: {attr['improvement']}")

        content = "\n".join(content_lines)

        return alerter.send_info(title=title, content=content)

    except Exception as e:
        print(f"飞书归因推送失败: {e}")
        return False


def record_manual_open(service: "ManualOrderService", order: "ManualOrder") -> None:
    """手动开仓后写入 trade_records (exit_price=0, 待平仓时 UPDATE 闭环).

    目的: 让 plan_effectiveness 等下游消费者在开仓时就能看到手动交易,
    实现 "通过 Dashboard 执行作战计划 = 计划命中" 的追踪闭环.

    设计:
      - exit_price=0 表示未平仓 (与 backfill 假数据一致, 已有 exit_price>0 过滤惯例)
      - futu_order_id 存储开仓 broker order_id, 平仓时 UPDATE 用此字段关联
      - 非阻塞, 失败静默
    """
    try:
        from datetime import timezone, timedelta
        et_now = datetime.now(timezone(timedelta(hours=-4)))
        trade_date = et_now.strftime("%Y-%m-%d")

        # 判断是否执行了盘前计划 (strategy_id 标记)
        strategy_id = "manual"
        try:
            plan_path = Path(__file__).parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"
            if plan_path.exists():
                import json as _json
                _plan = _json.loads(plan_path.read_text())
                _plan_targets = {t.get("symbol", ""): t for t in _plan.get("targets", [])}
                if order.symbol in _plan_targets:
                    _pt = _plan_targets[order.symbol]
                    _plan_dir = str(_pt.get("direction", "")).upper()
                    if _plan_dir in ("", "ANY") or _plan_dir == str(order.direction).upper():
                        strategy_id = "battle_plan"
        except Exception:
            pass

        _signal_id = f"manual:{order.order_id}" if order.order_id else "manual"

        conn = service._db()
        conn.execute(
            """
            INSERT INTO trade_records (
                trade_date, symbol, direction, entry_time, exit_time,
                entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                exit_reason, strategy_id, source_type, mode, trade_mode,
                data_quality, created_at, futu_order_id,
                signal_strength, signal_id, signal_confidence,
                owner_strategy
            ) VALUES (?, ?, ?, ?, NULL,
                      ?, 0, ?, 0, 0,
                      NULL, ?, 'MANUAL', ?, ?,
                      'manual', ?, ?,
                      0, ?, 0.0,
                      'manual')
            """,
            (
                trade_date, order.symbol, order.direction,
                order.timestamp or datetime.now().isoformat(),
                order.price or 0, order.quantity,
                strategy_id,
                service._get_mode_label(),
                "live" if service._is_live() else "simulate",
                datetime.now().isoformat(),
                order.order_id or "",
                _signal_id,
            ),
        )
        conn.commit()
        conn.close()
        logger.info(
            f"[手动交易闭环] 开仓写入: {order.symbol} {order.direction} "
            f"{order.quantity}股 strategy={strategy_id} → trade_records"
        )
    except Exception as e:
        logger.warning(f"[手动交易闭环] 开仓写入失败 (不影响下单): {e}")


def record_manual_close(service: "ManualOrderService", symbol: str, direction: str,
                        entry_price: float, exit_price: float, quantity: int,
                        reason: str, order_id: str, entry_reason: str = "") -> None:
    """手动平仓后更新 trade_records, 形成交易闭环 (P&L 追踪).

    优先 UPDATE 已有的开仓记录 (exit_price=0 的行), 找不到再 INSERT 新行.
    这样 plan_effectiveness 等消费者看到的是一笔完整的开→平交易.
    """
    try:
        # 计算 P&L (exit_price=0 时 realized_pnl=0, 防止假亏损)
        if exit_price > 0 and entry_price > 0 and quantity > 0:
            if direction == "LONG":
                pnl = (exit_price - entry_price) * quantity
            else:
                pnl = (entry_price - exit_price) * quantity
            pnl_pct = pnl / (entry_price * quantity)
        else:
            pnl = 0
            pnl_pct = 0

        conn = service._db()
        now = datetime.now()
        # trade_date 用美东时间 (EDT=UTC-4), 避免服务器时区导致交易日偏移
        from datetime import timezone, timedelta
        et_now = datetime.now(timezone(timedelta(hours=-4)))
        trade_date = et_now.strftime("%Y-%m-%d")

        # Check if this trade matches today's battle plan targets
        strategy_id = "manual"
        try:
            plan_path = Path(__file__).parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"
            if plan_path.exists():
                import json as _json
                _plan = _json.loads(plan_path.read_text())
                _plan_targets = {t.get("symbol", ""): t for t in _plan.get("targets", [])}
                if symbol in _plan_targets:
                    _pt = _plan_targets[symbol]
                    if _pt.get("direction") == direction:
                        strategy_id = "battle_plan"
        except Exception:
            pass

        # ── 优先 UPDATE 已有开仓记录 ──
        _signal_id_manual = f"manual:{order_id}" if order_id else "manual"
        _updated = False
        try:
            # 找同 symbol + exit_price=0 的未平仓记录
            cursor = conn.execute(
                """SELECT id FROM trade_records
                   WHERE symbol=? AND exit_price=0 AND source_type='MANUAL'
                   ORDER BY id DESC LIMIT 1""",
                (symbol,),
            )
            row = cursor.fetchone()
            if row:
                conn.execute(
                    """UPDATE trade_records SET
                        exit_time=?, exit_price=?, exit_reason=?,
                        realized_pnl=?, pnl_pct=?, close_futu_order_id=?,
                        trade_date=?, strategy_id=?,
                        signal_id=COALESCE(signal_id, ?),
                        owner_strategy='manual',
                        mode=?, trade_mode=?
                       WHERE id=?""",
                    (
                        now.isoformat(), exit_price, reason,
                        round(pnl, 2), round(pnl_pct, 4), order_id,
                        trade_date, strategy_id,
                        _signal_id_manual,
                        service._get_mode_label(),
                        "live" if service._is_live() else "simulate",
                        row[0],
                    ),
                )
                _updated = True
                logger.info(
                    f"[手动交易闭环] UPDATE 开仓记录 id={row[0]}: "
                    f"{symbol} 平仓 P&L=${pnl:.2f}"
                )
        except Exception as _upd_err:
            logger.warning(f"[手动交易闭环] UPDATE 失败, 退回 INSERT: {_upd_err}")

        if not _updated:
            # 查开仓时间 (从 open_positions 或 manual_order_audit)
            entry_time = None
            try:
                cursor = conn.execute(
                    "SELECT MIN(updated_at) FROM open_positions WHERE symbol=? AND owner='manual'",
                    (symbol,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    entry_time = row[0]
            except Exception:
                pass
            if not entry_time:
                try:
                    cursor = conn.execute(
                        "SELECT MIN(timestamp) FROM manual_order_audit WHERE symbol=? AND action='BUY'",
                        (symbol,),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        entry_time = row[0]
                except Exception:
                    pass
            if not entry_time:
                entry_time = now.isoformat()

            conn.execute(
                """
                INSERT INTO trade_records (
                    trade_date, symbol, direction, entry_time, exit_time,
                    entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                    exit_reason, entry_reason, strategy_id, source_type, mode, trade_mode,
                    data_quality, created_at, close_futu_order_id,
                    signal_strength, signal_id, signal_confidence,
                    owner_strategy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_date, symbol, direction, entry_time, now.isoformat(),
                    entry_price, exit_price, quantity, round(pnl, 2), round(pnl_pct, 4),
                    reason, entry_reason, strategy_id, "MANUAL",
                    service._get_mode_label(),
                    "live" if service._is_live() else "simulate",
                    "manual", now.isoformat(), order_id,
                    0, _signal_id_manual, 0.0,
                    "manual",
                ),
            )
        conn.commit()
        conn.close()
        logger.info(
            f"[手动交易闭环] {symbol} {direction} {quantity}股 "
            f"P&L=${pnl:.2f} ({pnl_pct*100:.2f}%) → trade_records"
        )
    except Exception as e:
        logger.warning(f"[手动交易闭环] 写入失败: {e}")


# ─────────────────────────────────────────────────────────────
# 手动持仓标记 (DB owner=manual + in-memory metadata.source=manual)
# ─────────────────────────────────────────────────────────────

def mark_position_manual(service: "ManualOrderService", symbol: str,
                         direction: str = "LONG") -> None:
    """标记持仓为手动管理，防止 ExitMonitor 误平.

    Race condition fix (2026-06-11): web 下单时 open_positions 可能尚未创建
    (position_sync 30s 后才写入)。纯 UPDATE 会 0 rows affected → 标记丢失。
    改为 UPSERT: UPDATE + INSERT IF NOT EXISTS。

    Task #10 (2026-06-15): 同步写入 in-memory PositionStore 的
    metadata['source'] = 'manual', 让 ManualHoldRegistry / 启动恢复 /
    PositionSource.from_str 能正确识别为 MANUAL.
    """
    try:
        conn = service._db()
        cursor = conn.execute(
            "UPDATE open_positions SET owner='manual' WHERE symbol=?",
            (symbol,),
        )
        if cursor.rowcount == 0:
            # Row doesn't exist yet — position_sync hasn't run.
            # Insert stub row; position_sync will merge/update on next cycle.
            conn.execute(
                """INSERT INTO open_positions (symbol, direction, size, avg_entry_price, owner, sync_status, updated_at)
                   VALUES (?, ?, 0, 0, 'manual', 'pending_sync', ?)""",
                (symbol, direction, datetime.now().isoformat()),
            )
            logger.info(
                f"[手动下单] {symbol} 新建 stub 行 owner=manual dir={direction} (等 position_sync 补全)"
            )
        else:
            logger.info(f"[手动下单] {symbol} 已标记为 owner=manual (跳过系统退出监控)")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[手动下单] 标记 owner=manual 失败 ({symbol}): {e}")

    # Task #10 (2026-06-15): 同步写入 in-memory PositionStore.metadata['source']='manual'
    # 防止 sync_from_positions / PositionSource.from_str 错判为 auto_main
    try:
        import importlib as _il
        _sc_mod = _il.import_module("app.core.scheduler_core")
        _sched = getattr(_sc_mod, "_scheduler_instance", None)
        if _sched and hasattr(_sched, 'position_store') and _sched.position_store.has(symbol):
            _pos = _sched.position_store.get(symbol)
            _meta = getattr(_pos, 'metadata', None) or {}
            if not isinstance(_meta, dict):
                _meta = {}
            _meta['source'] = 'manual'
            _meta['protection'] = 'protected'
            try:
                _pos.metadata = _meta
            except Exception:
                pass
            # 同步 owner_strategy, 让 PositionSource.get_position_source 识别为 MANUAL
            try:
                _pos.owner_strategy = 'manual'
            except Exception:
                pass
            logger.info(f"[手动下单] {symbol} in-memory metadata.source='manual' 已同步")
    except Exception as _mem_err:
        logger.debug(f"[手动下单] 同步 in-memory metadata.source 跳过 ({symbol}): {_mem_err}")
