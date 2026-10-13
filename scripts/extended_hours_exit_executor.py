#!/usr/bin/env python3
"""Extended-hours take-profit executor through the durable Playbook exit authority.

触发: premarket/afterhours exit_shadow 检测 TAKE_PROFIT / SL_BREAKEVEN /
      SL_TRAIL_LOCK 信号后调用.
动作: SIM-only ETH 限价平仓，成交后经 Playbook 簿记收口并推送结果.

与 R04 的关系: 入场/常规退出仍默认 RTH；本执行器显式声明 session=ETH，且仅限 SIM+限价.

安全护栏 (全自动的代价控制):
  1. 仅 SIMULATE 环境 (ALLOW_REAL_TRADING=false 当前); 实盘需另裁决.
  2. ETH 限价单, 不使用市价兜底; 部分成交保持 durable pending.
  3. 仅平已有 PLAYBOOK 持仓, 不开仓; next_open 使用 RTH 限价单.
  4. 重复防护: audit 日防重 + durable close_key 防重.
  5. 失败/异常 → 推送告警, 不静默; 不直接写 open_positions/trade_records.

用法: PYTHONPATH=. venv/bin/python scripts/extended_hours_exit_executor.py \
        --symbol US.NOK --direction LONG --qty 575 --price 10.35 \
        --session premarket --reason SL_BREAKEVEN [--dry]
回滚: revert 本脚本、exit_lifecycle/dual_broker 变更 + exit_shadow 里的 executor 调用.
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

_AUDIT_LOG = ROOT / "data" / "extended_hours_exit_executions.jsonl"
DB = ROOT / "data" / "ftseek_us.db"
SESSION_WINDOWS = {"premarket": (400, 929), "afterhours": (1600, 1959),
                   "next_open": (930, 945)}   # 2026-09-17 用户裁: 盘后仓持到次日开盘兑现 (RTH 已开盘)
SESSION_BOUNDARY_MINUTES = {"premarket": 570, "afterhours": 1200}
_PENDING_EVIDENCE_ERRORS = {
    "exit_evidence_pending",
    "exit_legacy_order_evidence_pending",
    "exit_order_evidence_pending",
    "exit_order_pending",
    "exit_position_evidence_pending",
    "exit_fill_price_pending",
    "exit_cancel_pending",
    "exit_reconciliation_pending",
}

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


def _stage(session: str) -> str:
    """阶段中文名 — 委托 app/core/trade_identity.phase_cn (唯一 SSOT)。"""
    try:
        from app.core.trade_identity import phase_cn
        return phase_cn(session) or "延长时段"
    except Exception:
        return "延长时段"


def _push(text: str, level=None):
    """推送结果通知 (成功或失败都推) — 走 PushHub 统一出口。

    2026-09-18 (用户裁「统一推送渠道」): 原直连 `FeishuNotifier().send_text()`,
    与盘中链 (PushHub→钉钉) 分流 ⇒ 同一事件在两个平台各出现一半。
    """
    try:
        from app.push import push_text_safe
        from app.push.message import Level, Category
        return push_text_safe(
            text,
            level=level if level is not None else Level.HIGH,
            category=Category.RISK_ALERT,
        )
    except Exception:
        return False


def _audit(result):
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "trade_date": _today_et(),
        **result,
    }
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOG.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# P1-3 节流参数: 首行必推, 之后每 _PUSH_REMIND_EVERY 次连续同态提醒一次
_PUSH_REMIND_EVERY = int(os.getenv("EXT_EXIT_PUSH_REMIND_EVERY", "30"))
_TAIL_LINES = 400
_TAIL_BYTES = 512 * 1024


def _repeat_run_len(symbol, status, error):
    """审计文件尾部连续相同 (symbol,status,error) 的行数 (含刚写入的本行).

    跨进程节流的依据: 本脚本每分钟被 cron 以新进程调起, 内存去重无效。
    只读尾部 (≤400 行), 不整读大文件。
    """
    try:
        if not _AUDIT_LOG.exists():
            return 1
        with _AUDIT_LOG.open() as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - _TAIL_BYTES)
            handle.seek(start)
            lines = handle.read().splitlines()
            if start and lines:
                lines = lines[1:]
            lines = lines[-_TAIL_LINES:]
        run = 0
        for line in reversed(lines):
            try:
                r = json.loads(line)
            except Exception:
                break
            if (r.get("symbol") == symbol and r.get("status") == status
                    and r.get("error") == error):
                run += 1
            else:
                break
        return max(run, 1)
    except Exception:
        return 1  # fail-open: 读不了审计就按首行处理 (宁可多推一次, 不可静默)


def _should_push(status, run_len):
    """同态连续重放只推首行 + 每 _PUSH_REMIND_EVERY 行提醒一次; ok 行不走此闸。"""
    if run_len <= 1:
        return True
    if status == "awaiting_confirmation":
        return False  # 等券商回报是瞬态, 重复提醒无信息量
    return run_len % _PUSH_REMIND_EVERY == 0


def _last_submit_second(session):
    """ETH 边界前必须留足限价等待与撤单确认余量."""
    boundary = SESSION_BOUNDARY_MINUTES.get(session)
    if boundary is None:
        return None
    try:
        limit_timeout = max(0.0, float(os.getenv("EXT_EXIT_LIMIT_TIMEOUT_S", "20")))
        cancel_margin = max(0.0, float(os.getenv("EXT_EXIT_CANCEL_MARGIN_S", "10")))
    except (TypeError, ValueError):
        return None
    return boundary * 60 - limit_timeout - cancel_margin


def _in_session_window(session, now=None):
    start, end = SESSION_WINDOWS[session]
    now = _now_et() if now is None else now
    if not start <= now.hour * 100 + now.minute <= end:
        return False
    if session not in SESSION_BOUNDARY_MINUTES:
        return True
    last_submit = _last_submit_second(session)
    if last_submit is None:
        return False
    return now.hour * 3600 + now.minute * 60 + now.second <= last_submit


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


def _load_exit_trade(symbol):
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, symbol, direction, entry_time, entry_price, quantity, "
            "source_type, futu_order_id, metadata_json FROM trade_records "
            "WHERE symbol=? AND (COALESCE(exit_time,'')='' OR COALESCE(exit_price,0)=0) "
            "ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    finally:
        conn.close()


def _get_dual_broker():
    from app.execution.dual_broker import get_dual_broker

    return get_dual_broker()


def execute(symbol, direction, qty, price, session, reason, dry=False):
    """盘前/盘后锁盈平仓. 只有券商确认成交后才更新本地仓位."""
    today = _today_et()
    if session not in SESSION_WINDOWS:
        return {"ok": False, "skip": "invalid_session"}
    if not _is_trading_day() or not _in_session_window(session):
        return {"ok": False, "skip": "outside_trading_window"}
    if _already_executed(symbol, today):  # P0-1: 跨进程防重 (读审计文件)
        return {"ok": False, "skip": "already_executed_today"}

    # 交易模式/账户: 单一真相源 (2026-09-20 切实盘开关清单化) —— 与主链路
    # (di_container / AUTO_TRADE_ACCOUNT + ALLOW_REAL_TRADING) 同源; ETH 真钱另需
    # EXT_REAL_AUTHORIZED=true (ETH 链从未实盘跑过)。旧实现在此裸读 FUTU_TRD_ENV
    # + SIMULATE_ACCOUNT_ID, 与主链路不同源 → 切实盘会错配; 且两变量除本守卫外无他用。
    from app.core.trading_mode import TradingMode, ext_channel_real_authorized

    _tm = TradingMode()
    target_env = _tm.target_env()
    if _tm.is_live and not ext_channel_real_authorized():
        _push(f"🚫 {_stage(session)}锁盈拦截: {symbol} ETH 实盘闸未放行 (EXT_REAL_AUTHORIZED≠true)")
        return {"ok": False, "skip": "real_not_authorized"}

    if dry:
        return {"ok": True, "dry": True, "symbol": symbol, "direction": direction,
                "qty": qty, "price": price, "session": session}

    if price <= 0 or qty <= 0 or not reason or len(reason) > 48:
        return {"ok": False, "skip": "invalid_position_or_price"}
    if any(not character.isalnum() and character != "_" for character in reason):
        return {"ok": False, "skip": "invalid_reason"}

    trade = _load_exit_trade(symbol)
    if trade is None:
        return {"ok": False, "skip": "open_trade_not_found"}
    if str(trade["direction"]).upper() != str(direction).upper():
        return {"ok": False, "skip": "direction_mismatch"}
    if abs(float(trade["quantity"] or 0) - float(qty)) > 1e-9:
        return {"ok": False, "skip": "quantity_mismatch"}

    dual = None
    try:
        from app.playbook.pipeline import (
            _book_playbook_exit_result,
            _playbook_exit_decision,
            _playbook_exit_owner,
        )

        try:
            metadata = json.loads(trade["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        metadata["extended_hours_exit"] = {
            "session": session,
            "requested_limit_price": float(price),
            "broker_remark_prefix": f"ext_exit_{reason}",
        }
        # 2026-09-25 P2口径修 (MU 9/25 案, 用户裁「按建议执行」): exit_lifecycle.reserve_leg
        #   只认 decision.metadata 的**顶层** broker_remark_prefix (且 intent session=ETH),
        #   嵌套在 extended_hours_exit 下它读不到 ⇒ ETH 单实际落 pbe_ 前缀, 使
        #   reconcile_extended_hours_exits / orphan_ext_order_watch (按 remark
        #   startswith ext_exit_ 过滤) 对这类单失明。补顶层键对齐契约; 嵌套键保留不动。
        metadata["broker_remark_prefix"] = f"ext_exit_{reason}"
        try:
            holding_hours = max(
                0.0,
                (_now_et() - datetime.fromisoformat(trade["entry_time"])).total_seconds() / 3600,
            )
        except (TypeError, ValueError):
            holding_hours = 0.0

        dual = _get_dual_broker()
        owner = _playbook_exit_owner(DB, trade, metadata)
        decision = _playbook_exit_decision(reason, float(price), metadata, holding_hours)
        timeout = float(os.getenv("EXT_EXIT_LIMIT_TIMEOUT_S", "20"))
        order_session = "RTH" if session == "next_open" else "ETH"
        close_result = dual.close_position(
            symbol, float(qty), str(direction).upper(), target_env=target_env,
            limit_price=float(price), limit_timeout_s=timeout, session=order_session,
            exit_owner=owner, exit_decision=decision,
        )
        dealt_qty = float(close_result.get("dealt_qty") or 0)
        remaining_qty = max(0.0, float(qty) - dealt_qty)
        fully_filled = dealt_qty == float(qty)
        db_updated = False
        if close_result.get("success") and close_result.get("order_filled") and fully_filled:
            db_updated = bool(_book_playbook_exit_result(dual, close_result))
        awaiting_confirmation = bool(
            not db_updated
            and close_result.get("exit_intent_id")
            and close_result.get("exit_state") in ("PENDING", "HELD")
            and close_result.get("error") in _PENDING_EVIDENCE_ERRORS
        )

        result = {
            "ok": bool(close_result.get("success") and close_result.get("order_filled")
                       and fully_filled and db_updated),
            "status": ("awaiting_confirmation" if awaiting_confirmation else
                       "booked" if db_updated else "failed_or_held"),
            "awaiting_confirmation": awaiting_confirmation,
            "order_id": close_result.get("futu_order_id", ""),
            "exit_intent_id": close_result.get("exit_intent_id", ""),
            "symbol": symbol, "qty": float(qty), "price": float(price),
            "session": session, "reason": reason,
            "filled_qty": dealt_qty,
            "avg_fill_price": close_result.get("filled_price", 0),
            "remaining_qty": remaining_qty,
            "fully_filled": fully_filled,
            "db_updated": db_updated,
            "exit_state": close_result.get("exit_state", ""),
            "error": close_result.get("error", ""),
        }
        _audit(result)
        # P1-3 (INCIDENT-002): ①BOOKED 陈旧结果不再冒充「待确认」— 那是账本已终结
        # 而仓位仍在的零保护形态, 文案必须直说并给出释放命令; ②跨进程节流 —
        # 本脚本由 cron 每分钟新进程调起, 内存节流无效, 用审计文件尾部连续
        # 相同 (symbol,status,error) 行数判定: 首行必推, 之后每 30 分钟提醒一次。
        _run_len = _repeat_run_len(result["symbol"], result["status"],
                                   result["error"])
        if result["ok"]:
            _push(f"✅ {_stage(session)}锁盈已成交\n{symbol} {dealt_qty:g}股 @ "
                  f"{result['avg_fill_price']:.2f}\n订单 {result['order_id']} | 原因 {reason}")
        elif _should_push(result["status"], _run_len):
            if (close_result.get("exit_state") == "BOOKED"
                    and not close_result.get("success")):
                _push(f"🚫 {_stage(session)}锁盈被阻: 账本已终结但仓位仍在\n"
                      f"{symbol} {qty:g}股 无可用退出路径 "
                      f"(intent={result['exit_intent_id']} state=BOOKED, "
                      f"close_key 被已终结意图占用 ⇒ ETH 重放与 RTH SL 兜底均发不出单)\n"
                      f"人工核对券商持仓后释放: PYTHONPATH=. venv/bin/python3.11 "
                      f"scripts/release_stuck_exit_intent.py --intent-id "
                      f"{result['exit_intent_id']} --apply")
            elif awaiting_confirmation:
                _push(f"⏳ {_stage(session)}锁盈等待券商回报\n{symbol} {dealt_qty:g}/{qty:g}股\n"
                      f"intent={result['exit_intent_id']} {result['error']}（非执行失败）")
            else:
                _push(f"⚠️ {_stage(session)}锁盈未成交\n{symbol} {dealt_qty:g}/{qty:g}股\n"
                      f"intent={result['exit_intent_id']} {result['error']}")
        return result
    except Exception as exc:
        result = {"ok": False, "symbol": symbol, "qty": qty, "price": price,
                  "session": session, "reason": reason,
                  "status": "failed_or_held", "awaiting_confirmation": False,
                  "error": f"durable_exit_failed:{exc}"}
        _audit(result)
        _push(f"🚨 延长时段 durable exit 异常\n{symbol}\n{exc}")
        return result
    finally:
        if dual is not None and hasattr(dual, "disconnect"):
            dual.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--direction", required=True)
    ap.add_argument("--qty", type=float, required=True)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours", "next_open"])
    ap.add_argument("--reason", default="SL_BREAKEVEN")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    r = execute(args.symbol, args.direction, args.qty, args.price, args.session, args.reason, args.dry)
    print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
