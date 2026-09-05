#!/usr/bin/env python3
"""[2026-09-05 审计 G-02 · 部署断层守卫] 代码比运行进程新 → 安全窗内自动重启 + 告警.

病灶 (REPORT.md G-02, 严重度 5): YAML 配置热加载, 但 **Python 代码只在进程启动时 import 一次**。
2026-09-05 实测形态: chase_cap enforce (87ebc785, 07:19 提交) 与经验泛化批 (46e31ccf) 均已落盘,
而运行进程 PID 266035 起于 9/4 14:45 → 闸门在内存里根本不存在, 日志/配置/测试全绿而**风控是纸面的**。
本脚本把"是否纸面"变成一个每分钟自答的问题:

  drift = max(app/**/*.py, main.py 的 mtime) - 进程启动时刻
  drift ≤ 120s          → ok  (同批部署的落盘/import 时钟余量)
  drift > 120s 且在安全窗 → systemctl restart + critical 告警 (冷却 6h)
  drift > 120s 但窗外     → 不重启, 但**首次发现即 critical 告警** (按 newest_code_ts 去重)
  服务非 active           → 不处置 (交 scripts/freeze_sentinel.py 存活层)

安全窗 = 北京 09:00–12:00: 盘后分析 cron (05:00–07:30 CST) 已结束, 盘中会话
(21:30 CST → 次日 04:00 CST) 尚有 ≥9h 缓冲 → 重启不会打断任何在途订单/退出监控。
窗外只告警不重启: 部署断层必须**可见**, 但"何时动刀"由会话边界决定。

已知代价 (诚实声明, 与 REPORT H-12 同源): 重启会丢内存态 — StartupReconciler 把
trail_state.highest_price 重置为 cost (Chandelier 保护清零), ExitMonitor._exit_cooldowns
清零。故本守卫**只在代码真的变了时**重启 (不是每日无条件重启), 把状态损失限定在部署事件本身
(与人工部署等价)。若要根治, 需 H-12 的 trail_state 持久化恢复, 独立议题。

挂载: crontab 每 10 分钟 (回滚 = 删 cron 行 + 删本脚本; 脚本只读代码树, 无副作用残留)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVICE = "ftseek-us"
STATE = Path("/run/ftseek-deploy-freshness.state")        # 上次 restart 的 epoch
NOTIFY_STATE = Path("/run/ftseek-deploy-freshness.notified")  # 已告警过的 newest_code_ts

GRACE_S = 120.0
RESTART_COOLDOWN_S = 6 * 3600.0
SAFE_WINDOW_MIN = (9 * 60, 12 * 60)   # 北京时间分钟数, 闭区间
SCAN_DIRS = ("app",)
SCAN_FILES = ("main.py",)


# ── 纯决策 (单元测试锚点) ─────────────────────────────────────────────────

def in_safe_window(now_minutes: int) -> bool:
    return SAFE_WINDOW_MIN[0] <= now_minutes <= SAFE_WINDOW_MIN[1]


def evaluate(now_ts, proc_start_ts, newest_code_ts, now_minutes,
             active_state: str, last_restart_ts) -> tuple:
    """纯决策。返回 (action, reason); action ∈ ok|skip|restart."""
    if active_state != "active":
        return "skip", f"service_{active_state}(存活层管辖)"
    if proc_start_ts is None or newest_code_ts is None:
        return "skip", "undetermined(取不到进程启动时刻或代码 mtime)"
    drift = newest_code_ts - proc_start_ts
    if drift <= GRACE_S:
        return "ok", f"fresh(drift={drift:.0f}s)"
    if not in_safe_window(now_minutes):
        return "skip", f"stale_drift={drift:.0f}s_outside_safe_window"
    # 冷却态来自文本文件 → 可能是 str; 不可解析按"无冷却记录"处理 (宁可多做一次自愈,
    # 也不能因脏态文件让守卫自己崩掉 —— 崩掉的守卫与没有守卫不可区分)。
    try:
        _last = float(last_restart_ts) if last_restart_ts is not None else None
    except (TypeError, ValueError):
        _last = None
    if _last is not None and now_ts - _last < RESTART_COOLDOWN_S:
        return "skip", f"stale_drift={drift:.0f}s_cooldown({int(now_ts - _last)}s)"
    return "restart", f"stale_drift={drift:.0f}s"


def should_notify_stale(notified_key, newest_code_ts) -> bool:
    """窗外漂移的告警去重: 同一批部署 (newest_code_ts 不变) 只吼一次."""
    try:
        return abs(float(notified_key) - float(newest_code_ts)) > 1.0
    except (TypeError, ValueError):
        return True


# ── 副作用层 ──────────────────────────────────────────────────────────────

def service_active_state() -> str:
    try:
        r = subprocess.run(["systemctl", "show", SERVICE, "-p", "ActiveState", "--value"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main_pid():
    try:
        r = subprocess.run(["systemctl", "show", SERVICE, "-p", "MainPID", "--value"],
                           capture_output=True, text=True, timeout=10)
        pid = int(r.stdout.strip() or 0)
        return pid if pid > 0 else None
    except Exception:
        return None


def proc_start_ts(pid: int):
    """进程启动 epoch。首选 /proc/<pid>/stat 的 starttime (field 22, 无时区解析),
    回落 systemd ActiveEnterTimestampMonotonic。两者都取不到 → None (决策层 skip + 告警)。

    纪律 (台账 §6 规则6②): 严禁用 `stat -c %Y /proc/PID` 取进程龄 — 那是该目录的**实时**
    mtime, 拿它比代码 mtime 恒真 (永远 fresh) ⇒ 守卫静默失明。本实现读 field 22 的
    clock ticks since boot + /proc/uptime 反推, 实测与 systemd ActiveEnterTimestamp 对齐
    (2026-09-05: 两者同为 14:11:01 CST)。"""
    try:
        hertz = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        return time.time() - (uptime - ticks / hertz)
    except Exception as e:
        # 台账 §6 规则5: 探测器必须自带"我自己坏了"的输出路径, 禁 except:pass
        print(f"[deploy-freshness] /proc/{pid}/stat 取启动时刻失败 "
              f"({type(e).__name__}: {e}) → 回落 systemd ActiveEnterTimestampMonotonic")
    try:
        r = subprocess.run(["systemctl", "show", SERVICE,
                            "-p", "ActiveEnterTimestampMonotonic", "--value"],
                           capture_output=True, text=True, timeout=10)
        mono_us = float(r.stdout.strip())
        now_mono_us = time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6
        return time.time() - (now_mono_us - mono_us) / 1e6
    except Exception:
        return None


def newest_code_ts():
    """被进程 import 的代码树里最新的 mtime (config/*.yaml 不计 — 它热加载)."""
    newest = None
    unreadable = 0
    paths = [REPO / f for f in SCAN_FILES]
    for d in SCAN_DIRS:
        paths.extend((REPO / d).rglob("*.py"))
    for p in paths:
        try:
            if "__pycache__" in p.parts or not p.is_file():
                continue
            m = p.stat().st_mtime
            if newest is None or m > newest[0]:
                newest = (m, str(p.relative_to(REPO)))
        except Exception:
            unreadable += 1
            continue
    if unreadable:
        # 台账 §6 规则5: 探测器不得静默少算 — 不可 stat 的文件数必须可见
        print(f"[deploy-freshness] ⚠️ {unreadable} 个代码文件不可 stat "
              f"(mtime 面按少算, 可能漏判部署断层)")
    return newest


def do_restart() -> bool:
    try:
        subprocess.run(["systemctl", "restart", SERVICE], timeout=200)
        return service_active_state() == "active"
    except Exception:
        return False


def _read(path: Path):
    try:
        return path.read_text().strip()
    except Exception:
        return None


def alert(msg: str) -> None:
    try:
        sys.path.insert(0, str(REPO))
        from app.utils.critical_alerts import send_critical_sync
        send_critical_sync(msg)
    except Exception as e:
        print(f"[deploy-freshness] alert failed (不阻断): {e}")


def main() -> int:
    now_ts = time.time()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    now_minutes = datetime.now().hour * 60 + datetime.now().minute
    pid = main_pid()
    start = proc_start_ts(pid) if pid else None
    newest = newest_code_ts()
    newest_ts, newest_file = newest if newest else (None, None)
    action, reason = evaluate(now_ts, start, newest_ts, now_minutes,
                              service_active_state(), _read(STATE))

    if action == "ok":
        return 0
    if action == "skip":
        print(f"[{ts}] skip: {reason} | newest={newest_file}")
        # 窗外漂移: 首次发现即吼 (闸门在内存里不存在 = 风控纸面, 必须可见)
        if reason.startswith("stale_drift") and should_notify_stale(
                _read(NOTIFY_STATE), newest_ts):
            try:
                NOTIFY_STATE.write_text(str(newest_ts))
            except Exception as e:
                print(f"[deploy-freshness] 去重态写失败 (不阻断): {e}")
            drift = int(newest_ts - start) if (newest_ts and start) else -1
            alert(f"🚨 [deploy-freshness] 部署断层: 代码比运行进程新 {drift}s "
                  f"(最新改动 {newest_file}) — 进程内加载的是旧代码, 新落盘的风控闸/热修"
                  f"**当前不生效**。安全窗 (北京 09:00–12:00) 内将自动重启; "
                  f"如需立即生效请人工 systemctl restart ftseek-us | G-02")
        elif reason.startswith("undetermined") and _read(NOTIFY_STATE) != "undetermined":
            # 守卫自身失明 (取不到进程启动时刻或代码 mtime) — 比漏报更糟, 必须吼一次
            try:
                NOTIFY_STATE.write_text("undetermined")
            except Exception as e:
                print(f"[deploy-freshness] 去重态写失败 (不阻断): {e}")
            alert(f"🚨 [deploy-freshness] 守卫自身失明: {reason} — 无法判断进程内代码是否"
                  f"最新 (部署断层转为不可观测)。请人工核查 systemctl show ftseek-us "
                  f"-p MainPID 与 app/ mtime | G-02/台账§6规则5")
        return 0

    drift = int(newest_ts - start)
    ok = do_restart()
    try:
        STATE.write_text(str(time.time()))
    except Exception as e:
        print(f"[deploy-freshness] 冷却态写失败 (不阻断): {e}")
    try:
        NOTIFY_STATE.write_text(str(newest_ts))
    except Exception as e:
        print(f"[deploy-freshness] 去重态写失败 (不阻断, 下次可能重复告警): {e}")
    msg = (f"🚨 [deploy-freshness] 部署断层自愈: 代码比进程新 {drift}s "
           f"(最新 {newest_file}) → systemctl restart "
           f"{'成功, 新代码已入进程' if ok else '失败, 需人工介入'} | "
           f"⚠️ 重启代价: trail_state/exit_cooldowns 内存态清零 (H-12) | G-02")
    print(f"[{ts}] {msg}")
    alert(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
