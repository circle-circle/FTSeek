#!/usr/bin/env python3
"""盘后兑现 shadow (2026-09-16; **2026-09-17 用户裁「方案1」后已非 record-only** — 见下).

问题: 盘前已有 exit_shadow/reprice_shadow 两条线, 盘后 16:00-20:00 ET
持仓盈利管理完全空白 — 财报/异动盘后 spike 无监控, 由亏转盈机会流失.

与盘前 exit_shadow 对称 (复用骨架, 差异三处):
  1. 时段窗: 盘后 16:00-20:00 ET (北京夏令时 04:00-08:00, 冬令时 05:00-09:00)
  2. 分母:   盘前用 prev_close (昨日正式收盘), 盘后用当日正式收盘
             (盘后时段 snapshot.last_price=盘后市场最新价, prev_close_price
              在盘后仍=当日收盘, 与盘前语义一致 — 实测 prev_close 在盘后窗稳定)
  3. 数据源: 盘后 LV3 同样有实时快照 (与盘前同通道, 9/16 已实测)

决策规则 (2026-09-18 用户勘误):
  复用盘中 Playbook 原规则 — TAKE_PROFIT / SL_BREAKEVEN / SL_TRAIL_LOCK,
  决策价 = LONG bid / SHORT ask, 并扣真实往返费; 亏损侧只 hold_no_stop.
  peak/breakeven 状态持久化到 trade_records.metadata_json.
记录: 与盘前 exit 同构 jsonl, 增 session="afterhours" 标签, 供统一结算器
      聚合时段收益台账 (盘前/盘后利润保护命中+反事实delta).

执行 (2026-09-17 用户裁「方案1」; 2026-09-18 勘误为原利润保护规则):
  TAKE_PROFIT / SL_BREAKEVEN / SL_TRAIL_LOCK 三信号且窗内 → 调
  extended_hours_exit_executor.py SIM 真平仓 (session=ETH 限价单) + 飞书推结果.
  亏损侧信号仍只落 jsonl. 回滚: 删调用块 (退回 record-only).

用法: PYTHONPATH=. venv/bin/python scripts/afterhours_exit_shadow.py [--dry]
挂载: crontab 北京 04:00-07:55 每 5min (= ET 16:00-19:55 夏令时), 交易日 gate.
      ⚠️ 11月冬令时 ET窗右移1h → 北京 cron 改 05:00-08:55 (列入 11 月重评)
回滚: 删 crontab 行 + 删本脚本 (jsonl 保留).
"""
import argparse
import json
import os
import sqlite3
import subprocess
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

DB = ROOT / "data" / "ftseek_us.db"
OUTPUT = ROOT / "data" / "afterhours_exit_shadow.jsonl"

# 决策参数单一真源 = app/playbook/exit_engine.py + config/playbook_horizon.yaml.
# 2026-09-18 用户勘误: 不再使用临时 +0.3% breakeven_lock / +2% lock_in 阈值.

# 2026-09-17 用户令「不强止损，直接持有到次日开盘」: 当日在盘后进的仓**停用**上述三信号,
# 改持到次日开盘兑现 (DEB §16.6: 现役盘后规则净 -0.64%, 持到次日开盘 +3.47%)。
# 回滚: EXT_AFTERHOURS_HOLD_TO_OPEN=false
# ─ 延长时段"只做止盈, 不做止损" (2026-09-17 用户令: 「只做止盈，不做止损(止损放在盘中)」) ──
#   依据 = 我们自建的盘前影子台账 (20 个**真实盘中持仓**的盘前路径):
#     现役组合(回本锁+0.3%/止盈+2% + 盘前止损-1.5%)  均值 -0.11%
#     去掉盘前止损 (回本锁+0.3%, 无盘前止损)          均值 -0.07%
#     只止盈 +1.5% / +2.0% / +3.0%                   +0.42% / +0.39% / +0.38%
#     什么都不做(持到盘前收口)                        +0.21%
#     ⇒ **盘前止损是负贡献** (与 DEB §16.5/§16.8 同向, 机制: 盘前回撤多会在收口前回归);
#       止损职责回收到盘中 (playbook 规则引擎读 initial_sl), 延长时段只负责"高价兑现"。
HOLD_TO_NEXT_OPEN = os.getenv("EXT_AFTERHOURS_HOLD_TO_OPEN", "true").lower() == "true"


def _now_et():
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


def _executor_timeout() -> float:
    """盘前/盘后 shadow 等待 exit_executor 子进程的上限 (秒).

    2026-09-18 修正 (Hermes 复核 §23 R2): 原 `max(90.0, t + 40.0)` 在 t=20 时
    恒等于 90.0 = 改动前的硬编码值 ⇒ **空操作**; 而实测该子进程确实 >90s 才被杀
    (logs/premarket_exit_shadow.log-20260918, US.CRM 09:48 ET 那轮), 失败模式原样保留。
    真实链条: 子进程内部限价等待 20s (env EXT_EXIT_LIMIT_TIMEOUT_S) + 连接/撤单/终态确认,
    故父进程上限取 max(180, t + 90)。⚠️ 与 extended_hours_exit_cron.py 的 collector
    上限是同一条链 (cron 240 > shadow 180 > 子限价 20), 改这里必须同步改那里。
    """
    limit_timeout = float(os.getenv("EXT_EXIT_LIMIT_TIMEOUT_S", "20"))
    return max(180.0, limit_timeout + 90.0)


def _is_trading_day() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return True  # fail-open


def _dir_sign(direction: str) -> int:
    return -1 if str(direction).upper() in ("SHORT", "SELL_SHORT") else 1


def _load_open_positions():
    from scripts.extended_hours_position_scope import load_non_manual_positions
    return load_non_manual_positions(DB)


def _is_afterhours_entry_today(entry_time, today_et: str) -> bool:
    """当日在盘后窗内进的仓 (纯函数). entry_time 是 CST → 转 ET 后判窗与日期."""
    try:
        from scripts.premarket_exit_shadow import _entry_et_dt
    except Exception:                                   # 回退: 只比日期前缀 (不会误伤当日仓)
        return str(entry_time)[:10] == today_et
    et = _entry_et_dt(entry_time)
    if et is None:
        return False
    hhmm_et = et.hour * 100 + et.minute
    return et.strftime("%Y-%m-%d") == today_et and 1600 <= hhmm_et <= 2000


def collect(dry: bool) -> int:
    today_et = _today_et()
    n_et = _now_et()
    hhmm = n_et.hour * 100 + n_et.minute
    from scripts.extended_hours_market_session import (
        atr_metrics,
        extended_hours_session_for,
        net_executable_pnl_pct,
        quote_metrics,
        risk_r_pct,
        round_trip_fee_metrics,
        session_volume_delta,
    )
    from scripts.extended_hours_profit_exit_rules import (
        decide_original_profit_exit,
        parse_trade_metadata,
        prioritize_profit_exit,
        persist_profit_exit_state,
    )
    session = extended_hours_session_for(today_et, hhmm)
    in_window = session == "afterhours"

    positions = _load_open_positions()
    if not positions:
        print(f"[collect] {today_et} {hhmm:04d}ET 无持仓")
        return 0

    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    snap_map = {}
    try:
        ret, snap = ctx.get_market_snapshot([p[0] for p in positions])
        if ret == RET_OK:
            for _, r in snap.iterrows():
                snap_map[r["code"]] = r
    finally:
        ctx.close()

    n = 0
    decision = {}
    for position in positions:
        symbol, direction, qty, entry, sl, entry_time, signal_id, trade_metadata = position[:8]
        trade_id = position[8] if len(position) > 8 else None
        source_type = position[9] if len(position) > 9 else None
        metadata = parse_trade_metadata(trade_metadata)
        s = snap_map.get(symbol)
        # 盘后真实价 = after_price (盘后窗内权威); last_price 在盘后滞后停留在盘中收盘
        # (2026-09-17 实测: NOK 盘后 after_price=10.29 vs last=10.14 差1.5%, after_vol在动)
        # 窗外回退 last_price (非盘后窗运行时兜底).
        last_raw = float(s.get("last_price", 0) or 0) if s is not None else 0.0
        after_px = float(s.get("after_price", 0) or 0) if s is not None else 0.0
        last = after_px if (in_window and after_px > 0) else last_raw
        # 盘后分母: prev_close_price 在盘后窗 = 当日正式收盘 (盘后涨跌基准)
        prev_close = float(s.get("prev_close_price", 0) or 0) if s is not None else 0.0
        # 盘后量字段: after_volume (实测字段名, 9/16 verify)
        sess_vol = float(s.get("after_volume", 0) or 0) if s is not None else 0.0
        upd = str(s.get("update_time", "")) if s is not None else ""
        sign = _dir_sign(direction)
        metrics = quote_metrics(s, direction)
        volume_delta = session_volume_delta(
            OUTPUT, symbol, today_et, "afterhours", sess_vol)
        risk_r = risk_r_pct(entry, sl, direction)
        atr = atr_metrics(entry, metadata)
        executable = metrics["executable_price"]
        executor_price = executable
        fee_metrics = round_trip_fee_metrics(
            qty, entry, executable, direction, today_et)
        fee_pct = fee_metrics["round_trip_fee_pct"] if fee_metrics else None
        gross_executable = round(
            (executable - entry) / entry * 100 * sign, 4
        ) if executable is not None and entry > 0 else None
        net_executable = net_executable_pnl_pct(
            entry, executable, direction, fee_pct)
        if executable is not None and entry > 0:
            decision = decide_original_profit_exit(
                direction, entry, executable, sl,
                metadata.get("peak_price", entry), metadata, fee_pct,
                source_type,
            )
            metadata = decision["metadata"]
            pnl_pct = decision.get("profit_pct")
            pnl_usd = round((executable - entry) * qty * sign, 2)
            action = decision["action"]
            if not dry:
                persist_profit_exit_state(DB, trade_id, decision, direction)
            fallback = (
                "hold_to_next_open"
                if HOLD_TO_NEXT_OPEN and _is_afterhours_entry_today(entry_time, today_et)
                else None
            )
            action = prioritize_profit_exit(action, fallback)
            # 2026-09-18 用户勘误: 只执行原 Playbook 利润保护/止盈信号.
            if (action in ("TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK", "SL_CERT_LOCK")
                    and in_window and not dry):
                if action in ("SL_BREAKEVEN", "SL_TRAIL_LOCK"):
                    executor_price = float(metadata.get("_be_net_line_price") or executable)
                try:
                    import subprocess as _sp
                    # 2026-09-17 修复(与盘前同批): 原 capture_output=True + except: pass ⇒ 子进程异常/超时
                    #   完全不可见 (券商侧已成交但本地无审计/无 DB 回写时无法定位)。现打 rc/stdout/stderr 尾部。
                    _proc = _sp.run(
                        [sys.executable, str(ROOT / "scripts" / "extended_hours_exit_executor.py"),
                         "--symbol", symbol, "--direction", direction, "--qty", str(qty),
                         "--price", str(executor_price), "--session", "afterhours", "--reason", action],
                        capture_output=True, text=True, timeout=_executor_timeout())
                    _tail = ((_proc.stdout or "")[-300:] + " | " + (_proc.stderr or "")[-300:]).strip()
                    if _proc.returncode != 0:
                        print(f"[shadow_exit] {symbol} {action} exit_executor rc={_proc.returncode} :: {_tail}")
                    else:
                        print(f"[shadow_exit] {symbol} {action} exit_executor ok :: {_tail[-200:]}")
                except Exception as _exc:
                    # 2026-09-18 (R2 修正, 与盘前同批): 超时时子进程被 kill, 其部分
                    #   stdout/stderr 是"它走到哪一步"的唯一证据; 原实现只打异常字符串。
                    _partial = ""
                    if isinstance(_exc, _sp.TimeoutExpired):
                        _partial = (" | 子进程残留输出: " + (
                            (_exc.stdout or "")[-300:] + " " + (_exc.stderr or "")[-300:]).strip())
                    print(f"[shadow_exit] {symbol} {action} exit_executor 异常: "
                          f"{type(_exc).__name__}: {_exc}{_partial}")
        else:
            pnl_pct, pnl_usd, action = None, None, "no_executable_quote"
        row = {
            "session": "afterhours",
            "line": "exit_lockin",
            "trade_date": today_et,
            "hhmm_et": hhmm,
            "in_afterhours_window": in_window,
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry_price": entry,
            "current_sl": sl,
            "signal_id": signal_id,
            "entry_time": entry_time,
            "prev_close": prev_close,        # 盘后窗 = 当日收盘 (盘后涨跌分母)
            "last_price": last,              # 盘后窗内 = after_price (真实盘后价)
            "after_price": after_px,         # 原始盘后价 (审计留痕)
            "session_volume": sess_vol,
            "session_volume_delta": volume_delta,
            "bid_price": metrics["bid_price"],
            "ask_price": metrics["ask_price"],
            "mid_price": metrics["mid_price"],
            "spread": metrics["spread"],
            "spread_pct": metrics["spread_pct"],
            "executable_price": executable,
            "executor_limit_price": executor_price,
            "quote_age_seconds": metrics["quote_age_seconds"],
            "risk_r_pct": risk_r,
            "atr_abs": atr["atr_abs"],
            "atr_pct": atr["atr_pct"],
            "atr_source": atr["atr_source"],
            "round_trip_fee_usd": fee_metrics["round_trip_fee_usd"] if fee_metrics else None,
            "round_trip_fee_pct": fee_pct,
            "gross_executable_pnl_pct": gross_executable,
            "net_executable_pnl_pct": net_executable,
            "decision_detail": decision.get("detail", "") if executable is not None else "",
            "cert_lock_pct": decision.get("cert_lock_pct") if executable is not None else None,
            "effective_sl": decision.get("effective_sl") if executable is not None else None,
            "adaptive_trailing_drawdown_pct": decision.get(
                "adaptive_trailing_drawdown_pct") if executable is not None else None,
            "adaptive_trailing_snapshot": decision.get(
                "adaptive_trailing_snapshot") if executable is not None else None,
            "peak_price": decision.get("peak_price") if executable is not None else metadata.get("peak_price"),
            "peak_profit_pct": decision.get("peak_profit_pct") if executable is not None else None,
            "breakeven_triggered": decision.get("breakeven_triggered") if executable is not None else bool(metadata.get("breakeven_triggered")),
            "take_profit_rr": decision.get("take_profit_rr") if executable is not None else None,
            "update_time": upd,
            "pnl_pct_vs_entry": pnl_pct,
            "pnl_usd": pnl_usd,
            "action": action,
            "settled": False,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        }
        if dry:
            print(f"[dry] {symbol} last={last} close={prev_close} pnl={pnl_pct}% ${pnl_usd} vol={sess_vol:.0f} → {action}")
        else:
            with OUTPUT.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if not _is_trading_day():
        print("[collect] 非交易日, 跳过")
        return
    n = collect(args.dry)
    print(f"[collect] 落 {n} 行 → {OUTPUT}")


if __name__ == "__main__":
    main()
