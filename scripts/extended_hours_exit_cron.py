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

    结构约束 (2026-10-02 INCIDENT-002 复查发现, 根因修):
      守卫 (orphan / closekey_health) 必须**早于** session 门控执行 —— 阻塞事件
      (RTH 09:3x) 与死锁/孤儿单 (ETH 05:50 创建) 都可能落在本 cron 窗外, 窗外返回
      "outside_extended_hours" 就永远扫不到。
      但「早于门控执行」不等于「结果被看见」: 首版只在窗外分支回填守卫字段, 窗内
      三个 return 全丢 ⇒ 探针自身故障 (ok:false) 在盘前/盘后窗内**无人可见**,
      与本文件 `_report_failure` docstring 记载的 INCIDENT-001 同型 (信息在 JSON 块里
      但行首无时间戳 ⇒ error_alert 扫不到 ⇒ 静默)。
      ⇒ 收敛为**单一返回出口** (`result` 逐层补字段), 守卫字段在所有路径都在场。
    """
    # ── 守卫: 全部早于 session 门控 ──────────────────────────────────────
    # 孤儿单检测: 按 durable 账本触发, 无事件时零券商调用。
    try:
        orphan = scan_orphan_orders(dry=dry)
    except Exception as _exc:      # 显式可见的降级, 不静默 (退出链优先)
        orphan = {"ok": False, "error": f"{type(_exc).__name__}: {_exc}"[:200]}

    # close_key 死锁体检 (INCIDENT-002 P2-5): 死锁不分时段, 且该形态零未决意图 ⇒
    #   blocked_exits / orphan watch / fast loop 全静默, 本探针是唯一哨兵。
    try:
        closekey_health = scan_closekey_health(dry=dry)
    except Exception as _exc:      # 探针故障不阻塞退出链
        closekey_health = {"ok": False, "error": f"{type(_exc).__name__}: {_exc}"[:200]}

    # 单一出口: 守卫字段在此一次性挂上, 后续任何分支都带着它返回。
    result = {"ok": True, "orphan": orphan, "closekey_health": closekey_health}

    session = get_exit_cron_session(now)
    command = collector_command(session)
    result["session"] = session
    if session is None or command is None:
        result["skip"] = "outside_extended_hours"
        return result

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["skip"] = "previous_run_still_active"
            return result
        if dry:
            result.update({"dry": True, "command": command})
            return result
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
            result.update({
                "ok": False,
                "error": f"collector_timeout_after_{timeout_s}s",
            })
            return result
        result.update({
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-1000:],
            "stderr_tail": (completed.stderr or "")[-1000:],
        })
        return result


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


def _report_guard_failure(name: str, guard: dict) -> None:
    """守卫自身故障必须发**契约格式 ERROR 行** (与 `_report_failure` 同一纪律).

    为什么不能只塞 JSON 块: error_alert 的 `_parse_line` 要求行首
    `^YYYY-MM-DD HH:MM` ⇒ JSON 块整行无时间戳 ⇒ 被视作 unparsed 跳过。
    守卫 (orphan / closekey_health) 是**唯一**能看见各自故障形态的哨兵,
    它自己哑了而无人知晓 = INCIDENT-002「静默零保护」的同型复发。
    """
    err = (guard.get("error") or "").strip() or "unknown_error"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [extended_hours_exit_cron] ERROR: 守卫 {name} 自身故障 {_et_hhmm()} "
          f"| {err[:180]} — 本守卫覆盖的形态当前**无人监视**, 请人工核查")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--timeout", type=int, default=240,
                        help="等待 shadow collector 的上限(秒); 必须 > shadow 的 _executor_timeout()")
    args = parser.parse_args()
    result = run_once(args.dry, args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    # 守卫故障单独发契约行 (不并入 rc=1: 守卫降级不该让退出链判失败 —
    # 退出链本身可能是好的, 把它一起标红会淹没真故障)
    for _name in ("orphan", "closekey_health"):
        _guard = result.get(_name) or {}
        if isinstance(_guard, dict) and _guard.get("ok") is False:
            _report_guard_failure(_name, _guard)
    if not result.get("ok"):
        _report_failure(result)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
