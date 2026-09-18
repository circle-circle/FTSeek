#!/usr/bin/env python3
"""盘前兑现 shadow (2026-09-16; **2026-09-17 用户裁「方案1」后已非 record-only** — 见下).

问题: 隔夜仓盘前 spike (NOK 9/16 06:10 ET pre_high 10.35 = +3.2% vs entry)
到 09:02 单日单点 premarket_reprice_shadow 已回落至 -1.9% — 盘前盈利在
开盘前蒸发, 现有影子捕不到 spike, 也从不回答"该不该趁盘前直接平仓锁盈".

与既有 shadow 的分工 (互补非重复):
  - premarket_reprice_shadow: 09:02 单点问"该不该重定价 be/SL" (持有中保护)
  - 本 shadow: 04:00-09:30 高频轮询问"该不该盘前直接平仓兑现" (退出决策)

数据源 (LV3 实测 2026-09-16 06:25 ET):
  get_market_snapshot.last_price = 盘前实时可成交价 (非昨收, update_time=当前分钟)
  pre_volume = 盘前累计成交量 (薄量 spike 过滤用)
  prev_close_price = 昨收 (分母, 盘前窗稳定)
  无需订阅推送 — 盘前逐笔太稀疏, 轮询快照即得准实时价.

决策规则 (2026-09-18 用户勘误):
  复用盘中 Playbook 原规则 — TAKE_PROFIT / SL_BREAKEVEN / SL_TRAIL_LOCK,
  但决策价改为方向感知 executable bid/ask 并扣真实往返费; SL_INITIAL 只落
  hold_no_stop, 亏损止损仍由盘中处理. peak/breakeven 状态持久化到
  trade_records.metadata_json, 跨盘前/盘中/盘后连续.

盘前收口强平 (2026-09-17 回放裁决, 详见 DEB §16.5):
  09:20-09:25 ET 对**今日盘前开的仓**无条件平仓 (action=pre_close_flatten, 覆盖其它信号),
  因为回放里"什么都不做等到收口"净 +0.78%/+0.26%, 而现役规则 -0.06%/-0.19%、-1.5%止损最差。
  回滚: EXT_PREMARKET_FLATTEN=false

盘后进场仓 (2026-09-17 用户令「不强止损，直接持有到次日开盘」):
  盘后窗内进的仓在**开盘前一律不动作** (既不锁盈也不止损), 09:30-09:45 ET 开盘兑现
  (调 exit executor --session next_open = RTH 单)。回滚: EXT_AFTERHOURS_HOLD_TO_OPEN=false

执行 (2026-09-17 用户裁「方案1」; 2026-09-18 勘误为原利润保护规则):
  TAKE_PROFIT / SL_BREAKEVEN / SL_TRAIL_LOCK 三信号且窗内 → 调
  extended_hours_exit_executor.py SIM 真平仓 (session=ETH 限价单) + 飞书推结果.
  亏损侧信号仍只落 jsonl. 回滚: 删调用块 (退回 record-only).

结算: 次轮运行对 trade_date < 今日 ET 的行, 用该日完整日 K + trade_records
      结局 join, 算"若按利润保护线兑现 vs 实际结局"的反事实 delta.

用法: PYTHONPATH=. venv/bin/python scripts/premarket_exit_shadow.py [--dry]
挂载: crontab 北京 16:00-21:25 每 5min (= ET 04:00-09:25 夏令时), 交易日 gate.
      ⚠️ 11 月冬令时 ET 窗右移 1h → 北京 cron 需改为 17:00-22:25 (列入 11 月重评)
回滚: 删 crontab 行 + 删本脚本 (jsonl 保留供分析).
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
OUTPUT = ROOT / "data" / "premarket_exit_shadow.jsonl"

# 决策参数单一真源 = app/playbook/exit_engine.py + config/playbook_horizon.yaml.
# 2026-09-18 用户勘误: 不再使用临时 +0.3% breakeven_lock / +2% lock_in 阈值.

# ─ 盘前收口强平 (2026-09-17 回放裁决, 用户令「按建议执行」) ──
#   回放 (60 交易日, 两套闸门口径, n=46 / n=161):
#     pre_close(开仓后什么都不做, 09:25 收口强平) 净 **+0.780% / +0.258%**  ← 唯一稳定跑赢费用线(~0.085%)
#     sl_only(-1.5% 止损→收口)                     净   -1.176% / -1.017%  (WR 17.4%/24.2%) ← 最差
#     rth_close(持有到收盘)                        净   -1.170% / -0.826%
#   ⇒ 盘前跌破 -1.5% 的仓多数在收口前**回归** ⇒ 盘前止损是在收割噪声, 不是控损;
#     且把盘前仓带进盘中是负期望 (与既有"盘前 spike 开盘蒸发"同向)。
#   实现范围 (只覆盖被回放验证过的 cohort): 仅 **entry_time ET 日期 == 今日** 的当日在盘前开的仓;
#     隔夜/legacy 仓回放未覆盖 → 仍走原 decide() 规则。
#   回滚: EXT_PREMARKET_FLATTEN=false (单 env, 不用改码)。
FLATTEN_ENABLED = os.getenv("EXT_PREMARKET_FLATTEN", "true").lower() == "true"
FLATTEN_FROM_HHMM = int(os.getenv("EXT_PREMARKET_FLATTEN_FROM", "920"))   # 09:20 ET 起每次轮询都强平

# ─ 盘后进场仓: 不强止损, 持有到次日开盘兑现 (2026-09-17 用户令「不强止损，直接持有到次日开盘」) ──
#   依据 (DEB §16.6 盘后回放 60 交易日): 现役盘后规则净 -0.64%(负); 盘后收口平 +0.94%;
#   **持有到次日开盘 +3.47%/中位+4.84%/WR 76%/合计+86.7%** = 最强臂; 持有到次日收盘 -1.14%。
#   盘后入场多为信息驱动跳空 (MFE 3.85-6.36%) ⇒ 止损/锁小利是在砍趋势, 与盘前噪声型 spike 相反。
#   尾部如实: 该臂最差 -19.39% (25 笔中 6 笔亏) —— 真隔夜跳空风险, 非无风险套利。
#   范围: 仅"entry ET 时间在盘后窗 (16:00-20:00) 且 ET 日期 != 今日"的 carry 仓; 其余仓走原规则。
#   回滚: EXT_AFTERHOURS_HOLD_TO_OPEN=false
# ─ 延长时段"只做止盈, 不做止损" (2026-09-17 用户令: 「只做止盈，不做止损(止损放在盘中)」) ──
#   依据 = 我们自建的盘前影子台账 (20 个**真实盘中持仓**的盘前路径):
#     现役组合(回本锁+0.3%/止盈+2% + 盘前止损-1.5%)  均值 -0.11%
#     去掉盘前止损 (回本锁+0.3%, 无盘前止损)          均值 -0.07%
#     只止盈 +1.5% / +2.0% / +3.0%                   +0.42% / +0.39% / +0.38%
#     什么都不做(持到盘前收口)                        +0.21%
#     ⇒ **盘前止损是负贡献** (与 DEB §16.5/§16.8 同向, 机制: 盘前回撤多会在收口前回归);
#       止损职责回收到盘中 (playbook 规则引擎读 initial_sl), 延长时段只负责"高价兑现"。
AFTERHOURS_HOLD_TO_OPEN = os.getenv("EXT_AFTERHOURS_HOLD_TO_OPEN", "true").lower() == "true"
NEXT_OPEN_EXIT_FROM = int(os.getenv("EXT_NEXT_OPEN_EXIT_FROM", "930"))
NEXT_OPEN_EXIT_TO = int(os.getenv("EXT_NEXT_OPEN_EXIT_TO", "945"))
AFTERHOURS_WINDOW_FROM, AFTERHOURS_WINDOW_TO = 1600, 2000


def _today_et() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().strftime("%Y-%m-%d")
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            from datetime import timezone, timedelta
            return datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")


def _now_et_hhmm() -> int:
    try:
        from app.utils.timezone import now_et
        n = now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        n = datetime.now(ZoneInfo("America/New_York"))
    return n.hour * 100 + n.minute


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


def _entry_et_dt(entry_time):
    """entry_time (open_positions, **CST**) → ET datetime; 失败回 None."""
    try:
        from app.utils.timezone import to_et
        dt = entry_time if isinstance(entry_time, datetime) else datetime.fromisoformat(str(entry_time))
        return to_et(dt)
    except Exception:
        return None


def is_afterhours_carry(entry_time, today_et: str, enabled: bool = None) -> bool:
    """该仓是否属"盘后进场 → 次日开盘兑现" cohort (纯函数, 可测).

    判据: entry 的 ET 时间落在盘后窗 (16:00-20:00) 且 ET 日期 != 今日
    (今日的盘后仓归盘后影子管, 不归盘前影子)。
    """
    if not (AFTERHOURS_HOLD_TO_OPEN if enabled is None else enabled):
        return False
    et = _entry_et_dt(entry_time)
    if et is None:
        return False
    hhmm_et = et.hour * 100 + et.minute
    return (et.strftime("%Y-%m-%d") != today_et
            and AFTERHOURS_WINDOW_FROM <= hhmm_et <= AFTERHOURS_WINDOW_TO)


def _entry_et_date(entry_time) -> str:
    """open_positions.entry_time 是 **CST** (见 memory/时区三格式) → 转 ET 日期再比今日.

    盘前仓 ET 04:00-09:25 == CST 16:00-21:25 (同一日历日), 但隔夜/legacy 仓跨日,
    必须真转换而不是取字符串前 10 位 (那会把 CST 昨夜 22:00 的仓算成"今日")。
    """
    try:
        from app.utils.timezone import to_et
        dt = entry_time if isinstance(entry_time, datetime) else datetime.fromisoformat(str(entry_time))
        et = to_et(dt)
        return et.strftime("%Y-%m-%d") if et else str(entry_time)[:10]
    except Exception:
        return str(entry_time)[:10]


def flatten_now(hhmm: int, entry_time, today_et: str, in_window: bool,
                enabled: bool = None) -> bool:
    """盘前收口强平是否适用 (纯函数, 可测).

    三个条件都必须满足: 开关开 + 在盘前窗内且 hhmm ≥ 09:20 + 该仓是**今日盘前开的**。
    """
    if not (FLATTEN_ENABLED if enabled is None else enabled):
        return False
    if not in_window or hhmm < FLATTEN_FROM_HHMM:
        return False
    return _entry_et_date(entry_time) == today_et


def _load_open_positions():
    from scripts.extended_hours_position_scope import load_non_manual_positions
    return load_non_manual_positions(DB)


def collect(dry: bool) -> int:
    today_et = _today_et()
    hhmm = _now_et_hhmm()
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
    in_window = session == "premarket"

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
        # 盘前真实价 = pre_price (盘前窗内权威); last_price 在盘前滞后停留在昨收
        # (2026-09-16 实锤: NOK 盘前 last 全程卡9.84不动, pre_high却从10.35涨到10.47 — 真价在pre_price)
        # 窗外回退 last_price (非盘前窗运行时兜底).
        last_raw = float(s.get("last_price", 0) or 0) if s is not None else 0.0
        pre_px = float(s.get("pre_price", 0) or 0) if s is not None else 0.0
        last = pre_px if (in_window and pre_px > 0) else last_raw
        pre_vol = float(s.get("pre_volume", 0) or 0) if s is not None else 0.0
        prev_close = float(s.get("prev_close_price", 0) or 0) if s is not None else 0.0
        pre_high = float(s.get("pre_high_price", 0) or 0) if s is not None else 0.0
        upd = str(s.get("update_time", "")) if s is not None else ""
        sign = _dir_sign(direction)
        metrics = quote_metrics(s, direction)
        volume_delta = session_volume_delta(
            OUTPUT, symbol, today_et, "premarket", pre_vol)
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
        _ah_carry = is_afterhours_carry(entry_time, today_et)
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
            _flat = flatten_now(hhmm, entry_time, today_et, in_window)
            fallback = None
            if _ah_carry:
                # 盘后进场仓: 仅无有效止盈时等待开盘; 错过窗口由盘中引擎接管.
                if hhmm < NEXT_OPEN_EXIT_FROM:
                    fallback = "hold_to_next_open"
                elif hhmm <= NEXT_OPEN_EXIT_TO:
                    fallback = "next_open_exit"
                else:
                    fallback = "hold_intraday_takeover"
            elif _flat:
                # 收口强平只兜底, 不覆盖 TAKE_PROFIT/BE/TRAIL_LOCK.
                fallback = "pre_close_flatten"
            action = prioritize_profit_exit(action, fallback)
            # 2026-09-18 用户勘误: 只执行原 Playbook 利润保护/止盈信号.
            _exec_ok = in_window or action == "next_open_exit"   # 开盘兑现发生在盘前窗之外
            if (action in ("TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK",
                           "pre_close_flatten", "next_open_exit")
                    and _exec_ok and not dry):
                if action in ("SL_BREAKEVEN", "SL_TRAIL_LOCK"):
                    executor_price = float(metadata.get("_be_net_line_price") or executable)
                try:
                    import subprocess as _sp
                    # 2026-09-17 修复(实盘发现): 原为 capture_output=True + except: pass ⇒ 子进程异常/超时
                    #   **完全不可见** (04:10/04:20 CTNT/GIPR 平仓单在券商侧已成交, 但本地无审计行、无 DB 回写,
                    #   根因因输出被丢弃而无法定位)。现把 rc/stdout/stderr 尾部打进日志 (cron 重定向到本 log)。
                    _proc = _sp.run(
                        [sys.executable, str(ROOT / "scripts" / "extended_hours_exit_executor.py"),
                         "--symbol", symbol, "--direction", direction, "--qty", str(qty),
                         "--price", str(executor_price), "--reason", action,
                         "--session", ("next_open" if action == "next_open_exit" else "premarket")],
                        capture_output=True, text=True, timeout=90)
                    _tail = ((_proc.stdout or "")[-300:] + " | " + (_proc.stderr or "")[-300:]).strip()
                    if _proc.returncode != 0:
                        print(f"[shadow_exit] {symbol} {action} exit_executor rc={_proc.returncode} :: {_tail}")
                    else:
                        print(f"[shadow_exit] {symbol} {action} exit_executor ok :: {_tail[-200:]}")
                except Exception as _exc:
                    print(f"[shadow_exit] {symbol} {action} exit_executor 异常: {type(_exc).__name__}: {_exc}")
        else:
            pnl_pct, pnl_usd, action = None, None, "no_executable_quote"
        # 距 pre_high 回撤幅度 (spike 可持续性指标)
        pullback = round((pre_high - last) / pre_high * 100, 3) if pre_high > 0 and last > 0 else None
        row = {
            "session": "premarket",
            "line": "exit_lockin",
            "trade_date": today_et,
            "hhmm_et": hhmm,
            "in_premarket_window": in_window,
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry_price": entry,
            "current_sl": sl,
            "signal_id": signal_id,
            "entry_time": entry_time,
            "prev_close": prev_close,
            "last_price": last,          # LV3 盘前实时价 (非昨收)
            "pre_high": pre_high,
            "pre_volume": pre_vol,
            "session_volume": pre_vol,
            "session_volume_delta": volume_delta,
            "bid_price": metrics["bid_price"],
            "ask_price": metrics["ask_price"],
            "mid_price": metrics["mid_price"],
            "spread": metrics["spread"],
            "spread_pct": metrics["spread_pct"],
            "executable_price": executable,
            "executor_limit_price": executor_price if executable is not None else None,
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
            "pnl_pct_vs_entry": pnl_pct,  # 相对入场价 (用户口径)
            "pnl_usd": pnl_usd,
            "pullback_from_pre_high_pct": pullback,
            "action": action,
            "is_today_entry": _entry_et_date(entry_time) == today_et,
            "is_afterhours_carry": _ah_carry,
            "flatten_enabled": FLATTEN_ENABLED,
            "settled": False,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        }
        if dry:
            print(f"[dry] {symbol} last={last} pnl={pnl_pct}% ${pnl_usd} vol={pre_vol:.0f} → {action}")
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
