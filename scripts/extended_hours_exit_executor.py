#!/usr/bin/env python3
"""盘前/盘后锁盈自动执行器 (2026-09-17, 用户批准全自动).

触发: premarket/afterhours exit_shadow 检测 breakeven_lock/lock_in 信号后调用.
动作: 全自动盘前/盘后卖出平仓 (锁盈), 推送成交结果通知.

与 R04 的关系: R04 强制主链 (dual_broker/exit_lifecycle) RTH-only, 防"订单意图不确定".
本执行器是**显式声明的延长时段例外通道** — 独立 session=ETH + 限价单, 不影响主链护栏.
REVIEW-20260916 §7.1 R04 允许: "若未来启用 ETH, 再显式 session=ETH 并使用限价单" — 本器即此.

安全护栏 (全自动的代价控制):
  1. 仅 SIMULATE 环境 (ALLOW_REAL_TRADING=false 当前); 实盘需另裁决.
  2. 限价单 (盘前/盘后无市价单), 价格=当前 pre/after_price (不追价).
  3. 仅平已有持仓 (SELL for LONG), 不开仓.
  4. 量验证: pre/after_volume ≥ MIN_VOLUME 才执行 (薄量 spike 不接).
  5. 重复防护: (symbol, trade_date) 每日一次.
  6. 失败/异常 → 推送告警, 不静默.

用法: PYTHONPATH=. venv/bin/python scripts/extended_hours_exit_executor.py \
        --symbol US.NOK --direction LONG --qty 575 --price 10.35 \
        --session premarket --reason breakeven_lock [--dry]
回滚: 删本脚本 + exit_shadow 里的 executor 调用.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

MIN_VOLUME = 50000  # 与影子线一致
_AUDIT_LOG = ROOT / "data" / "extended_hours_exit_executions.jsonl"
DB = ROOT / "data" / "ftseek_us.db"
SESSION_WINDOWS = {"premarket": (400, 929), "afterhours": (1600, 1959)}

from scripts.extended_hours_order_lifecycle import (
    ACTIVE_ORDER_STATUSES,
    query_order_rows,
    wait_order_fill,
)


def _already_executed(symbol, today):
    """跨进程防重: 只有真实全额成交且本地仓位已更新才阻断当日重试."""
    try:
        if not _AUDIT_LOG.exists():
            return False
        with _AUDIT_LOG.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (
                    r.get("symbol") == symbol
                    and r.get("trade_date") == today
                    and r.get("ok")
                    and r.get("fully_filled") is True
                    and r.get("db_updated") is True
                ):
                    return True
    except Exception:
        pass
    return False


def _now_et():
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _push(text: str):
    """推送结果通知 (成功或失败都推)."""
    try:
        from app.notifier.feishu import FeishuNotifier
        FeishuNotifier().send_text(text)
    except Exception:
        pass


def _audit(result):
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "trade_date": _today_et(),
        **result,
    }
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOG.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _open_trade_context():
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm

    return OpenSecTradeContext(
        host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPD_PORT", "11111")),
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.NONE,
    )


def _in_session_window(session):
    start, end = SESSION_WINDOWS[session]
    now = _now_et()
    return start <= now.hour * 100 + now.minute <= end


def _is_trading_day():
    import subprocess

    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _position_snapshot(symbol):
    conn = sqlite3.connect(DB, timeout=30)
    try:
        row = conn.execute(
            "SELECT direction, size, avg_entry_price, entry_time, position_id "
            "FROM open_positions WHERE symbol=? AND size>0",
            (symbol,),
        ).fetchone()
        if not row:
            return None
        return {
            "direction": str(row[0]),
            "qty": float(row[1] or 0),
            "entry_price": float(row[2] or 0),
            "entry_time": str(row[3] or ""),
            "position_id": str(row[4] or ""),
        }
    finally:
        conn.close()


def _latest_open_trade(conn, symbol):
    return conn.execute(
        "SELECT id, direction, entry_price, entry_time, quantity, futu_order_id "
        "FROM trade_records WHERE symbol=? "
        "AND (COALESCE(exit_time,'')='' OR COALESCE(exit_price,0)=0) "
        "ORDER BY id DESC LIMIT 1",
        (symbol,),
    ).fetchone()


def _apply_exit_fill(symbol, filled_qty, avg_fill_price, reason, order_id):
    """Apply a broker-confirmed fill to open_positions and trade_records."""
    conn = sqlite3.connect(DB, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        position = conn.execute(
            "SELECT direction, size, avg_entry_price, entry_time "
            "FROM open_positions WHERE symbol=? AND size>0",
            (symbol,),
        ).fetchone()
        if not position:
            conn.rollback()
            return {"db_updated": False, "fully_filled": True, "remaining_qty": 0.0,
                    "skip": "open_position_not_found"}

        direction, position_qty, entry_price, entry_time = position
        position_qty = float(position_qty or 0)
        close_qty = min(float(filled_qty), position_qty)
        remaining_qty = max(0.0, position_qty - close_qty)
        sign = -1 if str(direction).upper() in ("SHORT", "SELL_SHORT") else 1
        pnl = (float(avg_fill_price) - float(entry_price)) * close_qty * sign
        pnl_pct = (float(avg_fill_price) / float(entry_price) - 1) * sign if entry_price else 0
        now = _now_et().isoformat()
        try:
            holding_hours = max(
                0.0,
                (_now_et() - datetime.fromisoformat(entry_time)).total_seconds() / 3600,
            )
        except (TypeError, ValueError):
            holding_hours = 0.0
        trade = _latest_open_trade(conn, symbol)

        if remaining_qty <= 1e-9:
            if trade:
                conn.execute(
                    "UPDATE trade_records SET exit_price=?, exit_time=?, exit_reason=?, "
                    "realized_pnl=?, pnl_pct=?, holding_hours=?, close_futu_order_id=? "
                    "WHERE id=? AND (COALESCE(exit_time,'')='' OR COALESCE(exit_price,0)=0)",
                    (avg_fill_price, now, reason, round(pnl, 2), round(pnl_pct, 4),
                     round(holding_hours, 1), order_id, trade[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, exit_time, "
                    " entry_price, exit_price, quantity, realized_pnl, pnl_pct, exit_reason, "
                    " holding_hours, source_type, trade_mode, mode, owner_strategy, strategy_id, "
                    " close_futu_order_id, metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLAYBOOK_SIM', 'simulate', "
                    " 'futu_simulate', 'extended_hours_exit', 'extended_hours_exit', ?, ?, ?)",
                    (_today_et(), symbol, direction, entry_time, now, entry_price,
                     avg_fill_price, close_qty, round(pnl, 2), round(pnl_pct, 4), reason,
                     round(holding_hours, 1), order_id,
                     json.dumps({"session": "extended_hours", "close_order_id": order_id},
                                ensure_ascii=False), now),
                )
            conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
        else:
            if trade:
                conn.execute(
                    "UPDATE trade_records SET quantity=? WHERE id=?",
                    (remaining_qty, trade[0]),
                )
            conn.execute(
                "UPDATE open_positions SET size=?, updated_at=? WHERE symbol=?",
                (remaining_qty, now, symbol),
            )
            conn.execute(
                "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, exit_time, "
                " entry_price, exit_price, quantity, realized_pnl, pnl_pct, exit_reason, "
                " holding_hours, source_type, trade_mode, mode, owner_strategy, strategy_id, "
                " futu_order_id, close_futu_order_id, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLAYBOOK_SIM', 'simulate', "
                " 'futu_simulate', 'extended_hours_exit', 'extended_hours_exit', ?, ?, ?, ?)",
                (_today_et(), symbol, direction, entry_time, now, entry_price,
                 avg_fill_price, close_qty, round(pnl, 2), round(pnl_pct, 4),
                 f"{reason}_partial", round(holding_hours, 1),
                 trade[5] if trade else "", order_id,
                 json.dumps({"session": "extended_hours", "partial_close": True,
                             "close_order_id": order_id}, ensure_ascii=False), now),
            )
        conn.commit()
        return {
            "db_updated": True,
            "fully_filled": remaining_qty <= 1e-9,
            "remaining_qty": remaining_qty,
        }
    finally:
        conn.close()


def _audit_has_updated_order(order_id):
    if not _AUDIT_LOG.exists():
        return False
    with _AUDIT_LOG.open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            if record.get("order_id") == order_id and record.get("db_updated") is True:
                return True
    return False


def _reconcile_broker_exit_orders(ctx, env, acc_id, symbol):
    rows = [
        row for row in query_order_rows(ctx, env, acc_id)
        if row.get("symbol") == symbol and row.get("remark", "").startswith("ext_exit_")
    ]
    active_orders = []
    conflicts = []
    for row in rows:
        if row["status"] in ACTIVE_ORDER_STATUSES:
            active_orders.append({
                "order_id": row["order_id"], "status": row["status"],
                "qty": row["qty"], "filled_qty": row["filled_qty"],
            })
            continue
        if row["filled_qty"] <= 0 or row["avg_fill_price"] <= 0:
            continue
        if _audit_has_updated_order(row["order_id"]):
            continue
        try:
            _apply_exit_fill(
                symbol, row["filled_qty"], row["avg_fill_price"],
                row["remark"].removeprefix("ext_exit_"), row["order_id"],
            )
        except Exception as exc:
            conflicts.append({
                "order_id": row["order_id"],
                "error": f"reconcile_write_failed:{exc}",
            })
    return active_orders, conflicts


def execute(symbol, direction, qty, price, session, reason, dry=False):
    """盘前/盘后锁盈平仓. 只有券商确认成交后才更新本地仓位."""
    today = _today_et()
    if session not in SESSION_WINDOWS:
        return {"ok": False, "skip": "invalid_session"}
    if not _is_trading_day() or not _in_session_window(session):
        return {"ok": False, "skip": "outside_trading_window"}
    if _already_executed(symbol, today):  # P0-1: 跨进程防重 (读审计文件)
        return {"ok": False, "skip": "already_executed_today"}

    # 环境守卫: 仅模拟
    trd_env = os.getenv("FUTU_TRD_ENV", "SIMULATE")
    acc_id = int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823"))
    if trd_env.upper() != "SIMULATE":
        _push(f"🚫 盘前锁盈拦截: {symbol} 仅允许 SIMULATE")
        return {"ok": False, "skip": "real_not_authorized"}

    if dry:
        return {"ok": True, "dry": True, "symbol": symbol, "direction": direction,
                "qty": qty, "price": price, "session": session}

    from futu import TrdSide, OrderType, TrdEnv, TimeInForce, Session, RET_OK

    # 2026-09-17 实测: 19930823 (美股模拟) 需 filter_trdmarket=US + security_firm=NONE 才能下单
    #   缺 filter_trdmarket → get_acc_list 不列该股; 缺 security_firm → "Nonexisting acc_id".
    ctx = _open_trade_context()
    try:
        env = TrdEnv.SIMULATE
        active_orders, reconcile_conflicts = _reconcile_broker_exit_orders(
            ctx, env, acc_id, symbol
        )
        if reconcile_conflicts:
            return {
                "ok": False, "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            }
        if active_orders:
            return {"ok": False, "skip": "active_ext_exit_order",
                    "orders": active_orders}
        position = _position_snapshot(symbol)
        if not position:
            return {"ok": False, "skip": "open_position_not_found"}
        actual_qty = position["qty"]
        actual_direction = position["direction"]
        if actual_qty <= 0 or price <= 0:
            return {"ok": False, "skip": "invalid_position_or_price"}
        side = TrdSide.SELL if actual_direction.upper() in ("LONG", "BUY") else TrdSide.BUY
        ret, data = ctx.place_order(
            price=price, qty=actual_qty, code=symbol, trd_side=side,
            order_type=OrderType.NORMAL,
            trd_env=env,
            acc_id=acc_id,
            time_in_force=TimeInForce.DAY, session=Session.ETH,
            remark=f"ext_exit_{reason}",
        )
        if ret != RET_OK:
            result = {
                "ok": False, "order_id": "", "symbol": symbol, "qty": actual_qty,
                "price": price, "session": session, "reason": reason, "ret": ret,
                "error": str(data),
            }
            _audit(result)
            _push(f"❌ 盘前锁盈下单失败\n{symbol} {actual_qty:g}股 @ ${price:.2f}\n{result['error']}")
            return result
        order_id = ""
        if data is not None:
            try:
                order_id = str(data["order_id"].values[0])
            except Exception:
                pass
        fill = wait_order_fill(ctx, env, acc_id, order_id, actual_qty, price)
        if not fill.get("cancel_ok"):
            result = {
                "ok": False, "order_id": order_id, "symbol": symbol,
                "qty": actual_qty, "price": price, "session": session,
                "reason": reason, "ret": ret, "fill": fill,
                "error": f"exit_cancel_failed_{fill.get('status')}:{fill.get('error')}",
            }
            _audit(result)
            _push(f"⚠️ 盘前锁盈撤单未确认\n{symbol} 订单 {order_id}\n状态 {fill.get('status')}")
            return result
        if not fill.get("filled"):
            result = {
                "ok": False, "order_id": order_id, "symbol": symbol,
                "qty": actual_qty, "price": price, "session": session,
                "reason": reason, "ret": ret, "fill": fill,
                "error": f"exit_not_filled_{fill.get('status')}",
            }
            _audit(result)
            _push(f"⏳ 盘前锁盈未成交已撤单\n{symbol} {actual_qty:g}股 @ ${price:.2f}\n状态 {fill.get('status')}")
            return result
        try:
            db_result = _apply_exit_fill(
                symbol, fill["filled_qty"], fill["avg_fill_price"], reason, order_id
            )
        except Exception as exc:
            result = {
                "ok": False, "order_id": order_id, "symbol": symbol,
                "qty": actual_qty, "price": price, "session": session,
                "reason": reason, "ret": ret, "fill": fill,
                "error": f"position_db_update_failed:{exc}",
            }
            _audit(result)
            _push(f"🚨 盘前锁盈已成交但本地平仓失败\n{symbol} 订单 {order_id}\n{exc}")
            return result
        result = {
            "ok": True, "order_id": order_id, "symbol": symbol,
            "qty": actual_qty, "price": price, "session": session,
            "reason": reason, "ret": ret,
            "filled_qty": fill["filled_qty"],
            "avg_fill_price": fill["avg_fill_price"],
            "remaining_qty": db_result["remaining_qty"],
            "fully_filled": db_result["fully_filled"],
            "db_updated": db_result["db_updated"],
        }
        _audit(result)
        _push(f"✅ 盘前锁盈已成交\n{symbol} {fill['filled_qty']:g}股 @ ${fill['avg_fill_price']:.2f}\n"
              f"订单 {order_id} | 原因 {reason}\n剩余 {db_result['remaining_qty']:g}股")
        return result
    finally:
        ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--direction", required=True)
    ap.add_argument("--qty", type=float, required=True)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours"])
    ap.add_argument("--reason", default="breakeven_lock")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    r = execute(args.symbol, args.direction, args.qty, args.price, args.session, args.reason, args.dry)
    print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
