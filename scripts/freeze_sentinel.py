#!/usr/bin/env python3
"""[DEB-20260902 E0 · 2026-09-02 用户裁"按照建议执行落地" · 实施] 冻结外部哨兵 — 检测+取证+恢复一次开火.

设计锚点: docs/debates/DEB-20260902-process-freeze-root-cause.md §五 E0 (阈值 420s 裁决值).
心跳 touch 点: app/core/scheduler_core.py 退出快循环, 与内部 watchdog ping 同点.
挂载: crontab 每分钟 (回滚 = 删 cron 行 + 删本脚本 + 删 scheduler_core touch 块).

决策链:
  心跳 mtime 龄 ≤ 420s                 → 健康, 静默退出
  心跳文件缺失                          → 不处置 (非冻结形态; 启动冒烟/内部 watchdog 管辖, 防重启循环)
  服务未运行                            → 不处置 (systemd Restart=on-failure 管辖)
  相位 ≠ TRADING (或窗口兜底不命中)      → 不处置 (休市心跳停滞不是事故)
  15min 冷却内已处置过                   → 跳过重启, 仅留痕 (给重启完成的时间)
  否则                                 → ① py-spy dump 全线程栈 → logs/freeze_stacks/
                                          ② systemctl restart ftseek-us
                                          ③ send_critical_sync 告警 (进程外独立通道:
                                             主进程已死, 进程内 PushHub 构造性不可达 —
                                             同 scripts/llm_universe_scan.py _scan_fail_alert 先例)

阈值理由 (裁决值 420s): 内部 heal = 120s×3 = 360s; 外部 > 360s 保证内部 watchdog 活着时
必先出手, 仅"内部同死" (9/1 事故形态) 才外部兜底。300s 原案因抢跑内部 heal 被异议推翻
(见 DEB §五 E0 异议+裁决条)。
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HEARTBEAT = Path("/run/ftseek-heartbeat")
STATE = Path("/run/ftseek-freeze-sentinel.state")
STALE_S = 420
RESTART_COOLDOWN_S = 900
PY_SPY = REPO / "venv" / "bin" / "py-spy"
STACK_DIR = REPO / "logs" / "freeze_stacks"
SERVICE = "ftseek-us"


# ── 纯决策 (单元测试锚点) ─────────────────────────────────────────────────

def clock_window_allows(now_cst: datetime) -> bool:
    """相位检查失败时的时间窗兜底: ET 09:30-16:00 ≈ 北京 21:30→次日 04:00 (±45min 缓冲)."""
    wd = now_cst.weekday()
    hm = now_cst.hour * 60 + now_cst.minute
    evening = wd <= 4 and hm >= 20 * 60 + 45      # 周一~五 20:45 起
    morning = 1 <= wd <= 5 and hm <= 4 * 60 + 45  # 周二~六 04:45 止 (跨午夜段)
    return evening or morning


def market_phase_allows(now_cst: datetime, resolver=None) -> tuple:
    """相位闸门: 优先 get_market_phase() (15s SIGALRM 硬超时 — 防 OpenD 死锁反噬哨兵自身);
    异常 → 时间窗兜底。返回 (是否允许处置, 依据)。"""
    if resolver is None:
        def resolver():
            import signal

            def _alarm(_s, _f):
                raise TimeoutError("get_market_phase >15s (OpenD 疑死)")

            old = signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(15)
            try:
                from app.core.scheduler_triggers import get_market_phase, MarketPhase
                return get_market_phase() == MarketPhase.TRADING
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
    try:
        return bool(resolver()), "phase"
    except Exception as e:
        return clock_window_allows(now_cst), f"phase_unknown({type(e).__name__})->clock"


def evaluate(now_ts: float, hb_mtime, service_active: bool,
             phase_allows: bool, last_enforce_ts) -> tuple:
    """纯决策。返回 (action, reason); action ∈ ok|skip|enforce."""
    if hb_mtime is None:
        return "skip", "heartbeat_missing"
    age = now_ts - hb_mtime
    if age <= STALE_S:
        return "ok", f"age={age:.0f}s"
    if not service_active:
        return "skip", "stale_but_service_inactive"
    if not phase_allows:
        return "skip", "stale_but_not_trading"
    if last_enforce_ts is not None and now_ts - last_enforce_ts < RESTART_COOLDOWN_S:
        return "skip", f"cooldown({int(now_ts - last_enforce_ts)}s<{RESTART_COOLDOWN_S}s)"
    return "enforce", f"age={age:.0f}s"


# ── 副作用层 ──────────────────────────────────────────────────────────────

def service_is_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:
        return True  # systemctl 不可用时不以此闸门拦截


def last_enforce_ts():
    try:
        return float(STATE.read_text().strip())
    except Exception:
        return None


def _cmdline_has_python(pid: int) -> bool:
    try:
        return "python" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except Exception:
        return False


def _pid_from_systemctl():
    try:
        r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", SERVICE],
                           capture_output=True, text=True, timeout=10)
        pid = int(r.stdout.strip() or 0)
        return pid if pid > 0 and _cmdline_has_python(pid) else None
    except Exception:
        return None


def _pid_from_pgrep():
    try:
        r = subprocess.run(["pgrep", "-f", "main.py --start"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.split():
            if line.strip().isdigit():
                return int(line.strip())
    except Exception:
        return None


def resolve_main_pid():
    return _pid_from_systemctl() or _pid_from_pgrep()


def dump_stacks(pid: int, ts: str) -> str:
    STACK_DIR.mkdir(parents=True, exist_ok=True)
    out = STACK_DIR / f"freeze_{ts}_pid{pid}.txt"
    try:
        r = subprocess.run([str(PY_SPY), "dump", "--pid", str(pid)],
                           capture_output=True, text=True, timeout=60)
        out.write_text(f"# py-spy dump pid={pid} @ {ts}\n{r.stdout}\n--- stderr ---\n{r.stderr}",
                       encoding="utf-8")
    except Exception as e:  # 取证失败不阻塞恢复
        out.write_text(f"# py-spy dump failed: {e}", encoding="utf-8")
    return str(out)


def do_restart() -> bool:
    try:
        subprocess.run(["systemctl", "restart", SERVICE], timeout=200)
        return True
    except Exception:
        return False


def alert(msg: str) -> None:
    try:
        sys.path.insert(0, str(REPO))
        from app.utils.critical_alerts import send_critical_sync
        send_critical_sync(msg)
    except Exception as e:
        print(f"[freeze-sentinel] alert failed (不阻断): {e}")


def main() -> int:
    now_ts = time.time()
    now_cst = datetime.now()
    ts = now_cst.strftime("%Y%m%d_%H%M%S")
    try:
        hb_mtime = HEARTBEAT.stat().st_mtime
    except Exception:
        hb_mtime = None
    phase_ok, basis = market_phase_allows(now_cst)
    action, reason = evaluate(now_ts, hb_mtime, service_is_active(),
                              phase_ok, last_enforce_ts())
    if action == "ok":
        return 0  # 健康静默
    if action == "skip":
        print(f"[{ts}] skip: {reason} ({basis})")
        return 0
    pid = resolve_main_pid()
    stack_file = dump_stacks(pid, ts) if pid else "(pid 未解析, 跳过取证)"
    ok = do_restart()
    try:
        STATE.write_text(str(now_ts))
    except Exception as e:
        print(f"[freeze-sentinel] 冷却态写失败 (不阻断): {e}")
    age = int(now_ts - hb_mtime) if hb_mtime is not None else -1
    msg = (f"🚨 [freeze-sentinel] 进程冻结处置: 心跳停滞 {age}s (>420s 裁决阈值) | "
           f"栈: {stack_file} | systemctl restart {'成功' if ok else '失败, 需人工'} | DEB-20260902 E0")
    print(f"[{ts}] {msg}")
    alert(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
