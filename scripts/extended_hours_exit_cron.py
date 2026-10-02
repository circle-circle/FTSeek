#!/usr/bin/env python3
"""One-minute ET-calendar entry point for extended-hours exit collectors."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_market_session import get_extended_hours_session
from scripts.orphan_ext_order_watch import scan as scan_orphan_orders
from scripts.exit_closekey_health import scan as scan_closekey_health

LOCK_FILE = ROOT / "logs" / "extended_hours_exit_cron.lock"
NEXT_OPEN_START_MINUTES = 570
NEXT_OPEN_END_MINUTES = 585


def collector_command(session):
    if session == "premarket":
        return [sys.executable, str(ROOT / "scripts" / "premarket_exit_shadow.py")]
    if session == "afterhours":
        return [sys.executable, str(ROOT / "scripts" / "afterhours_exit_shadow.py")]
    if session == "next_open":
        return [sys.executable, str(ROOT / "scripts" / "premarket_exit_shadow.py")]
    return None


def is_next_open_window(now):
    from app.utils.holiday_calendar import get_holiday_calendar

    minutes = now.hour * 60 + now.minute
    return (
        NEXT_OPEN_START_MINUTES <= minutes < NEXT_OPEN_END_MINUTES
        and get_holiday_calendar().is_trading_day("US", now.date())
    )


def get_exit_cron_session(now=None):
    if now is None:
        from app.utils.timezone import now_et
        now = now_et()
    session = get_extended_hours_session(now)
    if session is not None:
        return session
    return "next_open" if is_next_open_window(now) else None


def run_once(dry=False, timeout_s=240, now=None):
    """单轮收集器 (每 1 分钟由 cron 触发, flock 防叠加).

    timeout_s = 等待 shadow collector 子进程的上限。2026-09-18 (R2) 从 120 → 240:
    shadow 内部等 exit_executor 的上限已是 180s (见 premarket/afterhours_exit_shadow.
    _executor_timeout), 若外层仍 120 ⇒ cron 会先杀 shadow, 而 shadow 被杀时它启动的
    孙进程仍在跑 ⇒ 孤儿单 + 本地零证据 (比不改更糟)。链条必须单调:
    cron 240 > shadow 180 > 子进程限价等待 20s。
    240 的构成 (如实): 180 (容忍一个卡住的子进程走完撤单/终态确认) + 60 (同轮其余标的正常返回)。

    另 (2026-09-18 §23.3 方案 A): 会话门控**之前**先跑孤儿单检测 (观察期只告警不撤单)。
    它按 durable 账本触发, 无事件时零券商调用; 失败不阻塞退出链 (只把错误回填到结果里)。
    """
    # 孤儿单检测: 必须**早于** session 门控 —— 阻塞事件(RTH 09:3x)与孤儿单(ETH 05:50 创建)
    #   都可能落在本 cron 的窗外, 窗外返回 "outside_extended_hours" 就永远扫不到。
    try:
        orphan = scan_orphan_orders(dry=dry)
    except Exception as _exc:      # 显式可见的降级, 不静默 (退出链优先)
        orphan = {"ok": False, "error": f"{type(_exc).__name__}: {_exc}"[:200]}

    # close_key 死锁体检 (INCIDENT-002 P2-5): 同样早于 session 门控 — 死锁不分时段,
    #   且该形态零未决意图 ⇒ blocked_exits/orphan watch/fast loop 全静默, 本探针是唯一哨兵。
    try:
        closekey_health = scan_closekey_health(dry=dry)
    except Exception as _exc:      # 探针故障不阻塞退出链
        closekey_health = {"ok": False, "error": f"{type(_exc).__name__}: {_exc}"[:200]}

    session = get_exit_cron_session(now)
    command = collector_command(session)
    if session is None or command is None:
        return {"ok": True, "skip": "outside_extended_hours", "session": session,
                "orphan": orphan, "closekey_health": closekey_health}

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": True, "skip": "previous_run_still_active", "session": session,
                    "orphan": orphan}
        if dry:
            return {"ok": True, "dry": True, "session": session, "command": command,
                    "orphan": orphan}
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "session": session,
                "error": f"collector_timeout_after_{timeout_s}s",
                "orphan": orphan,
            }
        return {
            "ok": completed.returncode == 0,
            "session": session,
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-1000:],
            "stderr_tail": (completed.stderr or "")[-1000:],
            "orphan": orphan,
        }


def _et_hhmm() -> str:
    """ET 时刻标注 (告警行里带上, 便于与 JSON 块/影子行对齐)。取不到就留空, 不阻塞告警。"""
    try:
        from app.utils.timezone import now_et

        return f"{now_et():%H:%M}ET"
    except Exception:
        return ""


def _report_failure(result: dict) -> None:
    """把子进程失败显性化为**契约格式 ERROR 行** (供 error_alert 捕获)。

    2026-09-19 INCIDENT-001 (P1): 原实现只把 rc 塞进 JSON 块, 而 JSON 块行首无时间戳
    ⇒ error_alert 的 `_parse_line` (要求 ^YYYY-MM-DD HH:MM) 视其为 unparsed 跳过;
    叠加 error_alert 的 cron 窗未覆盖延长时段 ⇒ 退出链每分钟崩、连崩 7 轮 = **零告警**。
    JSON 块保留 (机读/审计), 另发一行人读且可被 error_alert 扫到的 ERROR 行。

    判据等价性: 持续失败由 error_alert 的「15min 窗内计数 ≥ max(3, 3×近 7 日同窗中位数)」
    兜住 —— 本轮次即"连续 N 轮 rc≠0"的等价判据, 故不另建计数器 (做减法, 复用既有仪表)。
    """
    stderr = (result.get("stderr_tail") or "").strip()
    reason = (result.get("error") or "").strip()
    if not reason and stderr:
        rows = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
        frames = [ln for ln in rows if ln.startswith('File "')]
        reason = (rows[-1] if rows else "")[:180]
        if frames:
            reason = f"{reason} @ {frames[-1][:110]}"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [extended_hours_exit_cron] ERROR: 退出链子进程失败 "
          f"session={result.get('session')} {_et_hhmm()} rc={result.get('returncode')} "
          f"| {reason} (完整 stderr 见本文件 JSON 块)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--timeout", type=int, default=240,
                        help="等待 shadow collector 的上限(秒); 必须 > shadow 的 _executor_timeout()")
    args = parser.parse_args()
    result = run_once(args.dry, args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        _report_failure(result)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
