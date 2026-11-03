#!/usr/bin/env python3
"""[DEB-20260902 E0 · 2026-09-02 用户裁"按照建议执行落地" · 实施] 冻结外部哨兵 — 检测+取证+恢复一次开火.

设计锚点: docs/debates/DEB-20260902-process-freeze-root-cause.md §五 E0 (阈值 420s 裁决值).
心跳 touch 点: app/core/scheduler_core.py 退出快循环, 与内部 watchdog ping 同点.
挂载: crontab 每分钟 (回滚 = 删 cron 行 + 删本脚本 + 删 scheduler_core touch 块).

决策链:
  ── 存活层 (与相位/心跳正交, 每分钟必判) ──
  ActiveState=active/reloading              → 健康, 落入下方冻结层
  ActiveState=activating/deactivating        → 不处置 (过渡态, 给 systemd 时间)
  ActiveState=inactive 且 Result=success     → 不处置 (干净退出 ≈ 运维 systemctl stop, 不抢方向盘)
  其余 (failed / start-limit-hit / 异常退出)  → reset-failed + restart + critical 告警 (冷却 900s)
  ── 冻结层 (原逻辑不变) ──
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

存活层理由 (2026-09-05 审计 G-01/R-5(ii) 伴生): 冻结层被相位门 (仅 TRADING) 与
"服务未运行→不处置" 双重挡在门外 —— 恰恰把 G-01 的真实空洞 (进程死亡/休市期死亡)
留给了 systemd。而 systemd 一旦 start-limit 击穿即永久 failed (无告警), 存量仓在下一
交易日开盘时无人看护 (2026-09-05 存量 UBER/RIVN/HOOD 即此形态)。本层只补"死了没人知道":
不判行情、不判冻结, 只在 systemd 已放弃时拉一次并 critical 告警。
回滚: 删 main() 内存活层分支 + evaluate_liveness/service_state/do_revive 三函数。

快速冻结层 (DEB-20261023, 2026-10-23 用户批准):
  10/22 实证: PlaybookExitFastLoop 卡在无超时的 request_history_kline (py-spy 栈
  freeze_20261022_214002_pid3056646.txt), 心跳正是该循环更新 → 420s 后才处置;
  且 systemctl restart 的 SIGTERM 因进程僵死被忽略, 120s 后 SIGKILL, 端到端 ~9.5min。
  10/23 同类: 人工在 4min 重启, 仍 SIGKILL (无栈)。
  本层: 心跳停滞 >90s + ActiveState=active/SubState=running + TRADING + 启动宽限
  → py-spy + SIGUSR1 (faulthandler) 双栈取证 → SIGKILL 硬停 → systemd
  Restart=always 拉起 (~30s) → critical 告警。420s 旧层保留为兜底 (快速层禁用或
  启动时间不可知时生效)。
  回滚: FTSEEK_SENTINEL_FAST_S=0 (只关快速层, 不动旧层) + 删 evaluate_fast/hard_restart.
"""
from __future__ import annotations

import os
import sys
import signal
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
REVIVE_STATE = Path("/run/ftseek-freeze-sentinel.revive.state")
RUN_LOCK = Path("/run/ftseek-freeze-sentinel.lock")
_NO_LOCK = object()  # 锁文件不可写时照常运行 (锁是防重叠, 不是安全闸)
STARTUP_GRACE_S = 180
HARD_KILL_WAIT_S = 15
RESTART_VERIFY_S = 45


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# 90s ≈ 出场快循环 10s 节拍的 9 倍; 正常一轮 K 线/报价调用远小于此.
# 0 = 只保留 420s 旧层 (快速层回滚阀).
FAST_STALE_S = _env_int("FTSEEK_SENTINEL_FAST_S", 90)

# ── 长轮次豁免 (2026-11-03): 主线程"进度"心跳 ──────────────────────────────
#   问题: /run/ftseek-heartbeat 的唯一写入者是出场快循环; 主策略轮连续持 GIL >90s
#   (11-02 首轮 122s, GC/GIL 形态) 时它拿不到 CPU ⇒ 心跳停 ⇒ 本层 SIGKILL 一个
#   "只是慢"的进程 (内存态持仓/在途委托全丢)。
#   修法: 主线程在逐条指令/逐标的循环里另写 /run/ftseek-progress-main (语义=有进展)。
#   判定 (三条互斥, 见 evaluate_fast):
#     · 心跳停 + 进度新鲜 + 停滞 ≤ 豁免上限 ⇒ skip(progressing), 不杀 (告警留痕)
#     · 心跳停 + 进度也停                 ⇒ enforce (真冻结, 与旧行为一致)
#     · 心跳停 + 进度新鲜但超豁免上限        ⇒ enforce (出场线程可能独死, 不许被掩盖)
#   回滚: FTSEEK_SENTINEL_PROGRESS_EXEMPT_S=0 (等价于旧行为, 零代码).
PROGRESS = Path(os.environ.get("FTSEEK_PROGRESS_FILE", "/run/ftseek-progress-main"))
PROGRESS_FRESH_S = _env_int("FTSEEK_SENTINEL_PROGRESS_FRESH_S", 45)
PROGRESS_EXEMPT_MAX_S = _env_int("FTSEEK_SENTINEL_PROGRESS_EXEMPT_S", 300)


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


def evaluate_fast(now_ts: float, hb_mtime, active_state: str, sub_state: str,
                  phase_allows: bool, last_enforce_ts, started_ts,
                  fast_stale_s: int = None, progress_mtime=None,
                  progress_fresh_s: int = None,
                  progress_exempt_max_s: int = None) -> tuple:
    """快速冻结层纯决策 (DEB-20261023). 返回 (action, reason); action ∈ ok|skip|enforce.

    与 420s 旧层互补: 旧层等内部 heal 先出手; 快速层针对"内部 heal 也无法执行"的
    整进程/出场循环僵死 — 心跳源就是出场快循环, 它卡住时心跳必然停滞。
    """
    stale_s = FAST_STALE_S if fast_stale_s is None else fast_stale_s
    if stale_s <= 0:
        return "skip", "fast_lane_disabled"
    if hb_mtime is None:
        return "skip", "heartbeat_missing"
    age = now_ts - hb_mtime
    if age <= stale_s:
        return "ok", f"age={age:.0f}s"
    if active_state != "active" or sub_state != "running":
        return "skip", f"service_not_running({active_state}/{sub_state})"
    if started_ts is None:
        # 启动时间不可知时不抢跑 (交回 420s 旧层), 防重启循环.
        return "skip", "start_time_unknown"
    if now_ts - started_ts < STARTUP_GRACE_S:
        return "skip", f"startup_grace({int(now_ts - started_ts)}s<{STARTUP_GRACE_S}s)"
    if not phase_allows:
        return "skip", "stale_but_not_trading"
    # 长轮次豁免 (2026-11-03): 主线程仍在推进 ⇒ 是慢, 不是死。
    _exempt_max = PROGRESS_EXEMPT_MAX_S if progress_exempt_max_s is None else progress_exempt_max_s
    _fresh_s = PROGRESS_FRESH_S if progress_fresh_s is None else progress_fresh_s
    if _exempt_max > 0 and progress_mtime is not None and age <= _exempt_max:
        prog_age = now_ts - progress_mtime
        if prog_age <= _fresh_s:
            return "skip", (f"progressing(hb_age={age:.0f}s<={_exempt_max}s, "
                            f"progress_age={prog_age:.0f}s<={_fresh_s}s)")
    if last_enforce_ts is not None and now_ts - last_enforce_ts < RESTART_COOLDOWN_S:
        return "skip", f"cooldown({int(now_ts - last_enforce_ts)}s<{RESTART_COOLDOWN_S}s)"
    return "enforce", f"age={age:.0f}s>{stale_s}s"


def evaluate_liveness(active_state: str, result: str, now_ts: float,
                      last_revive_ts) -> tuple:
    """纯决策 (存活层)。返回 (action, reason); action ∈ ok|skip|revive.

    与 evaluate (冻结层) 正交: 不看心跳、不看相位 —— 服务不在跑本身就是事故,
    周末/假日死亡同样致命 (下一交易日开盘时存量仓无人看护)。
    """
    if active_state in ("active", "reloading"):
        return "ok", "active"
    if active_state in ("activating", "deactivating"):
        return "skip", f"transitioning({active_state})"
    if active_state == "unknown":
        return "skip", "state_unknown(systemctl 不可用, 不以此闸拦截)"
    if active_state == "inactive" and result == "success":
        # 干净退出 ≈ 运维 systemctl stop (Restart=always 不会自动拉起的情形)。
        # 与操作者抢方向盘比停机更糟 → 只留痕不处置。
        return "skip", "inactive_clean_exit(疑运维主动停机)"
    if last_revive_ts is not None and now_ts - last_revive_ts < RESTART_COOLDOWN_S:
        return "skip", f"revive_cooldown({int(now_ts - last_revive_ts)}s<{RESTART_COOLDOWN_S}s)"
    return "revive", f"state={active_state}/result={result}"


# ── 副作用层 ──────────────────────────────────────────────────────────────

def _started_ts_from_monotonic(mono_us: str):
    try:
        mono = int(mono_us)
        if mono <= 0:
            return None
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return time.time() - (uptime - mono / 1_000_000.0)
    except Exception:
        return None


def _started_ts_from_text(text: str):
    # systemd: "Fri 2026-10-23 21:50:32 CST"
    try:
        parts = text.split()
        if len(parts) >= 4:
            return datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception as _ts_e:
        # 2026-10-28 except:pass 清剿: 启动时刻读不出 ⇒ 后续"是否已在生产 N 小时"判定失据。
        print(f"[warn] systemd 启动时刻解析失败 (返回 None): {_ts_e} | {text[:80]!r}",
              file=sys.stderr)
    return None


def service_runtime_state() -> tuple:
    """返回 (ActiveState, SubState, Result, started_ts).

    started_ts 供快速层做启动宽限; 不可知时返回 None → 快速层跳过, 交 420s 旧层。
    """
    kv = {}
    try:
        r = subprocess.run(
            ["systemctl", "show", SERVICE,
             "-p", "ActiveState", "-p", "SubState", "-p", "Result",
             "-p", "ActiveEnterTimestampMonotonic", "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    except Exception:
        return "unknown", "unknown", "unknown", None
    started_ts = _started_ts_from_monotonic(kv.get("ActiveEnterTimestampMonotonic", ""))
    if started_ts is None:
        started_ts = _started_ts_from_text(kv.get("ActiveEnterTimestamp", ""))
    return (kv.get("ActiveState", "unknown"), kv.get("SubState", "unknown"),
            kv.get("Result", "unknown"), started_ts)


def service_state() -> tuple:
    """返回 (ActiveState, Result) — 保留旧接口 (存活层/旧冻结层)。"""
    active, _sub, result, _started = service_runtime_state()
    return active, result


def last_revive_ts():
    try:
        return float(REVIVE_STATE.read_text().strip())
    except Exception:
        return None


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


def _signal_caught(pid: int, signum: int) -> bool:
    """目标进程是否捕获了该信号 (读 /proc/<pid>/status SigCgt)。"""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("SigCgt:"):
                return bool(int(line.split()[1], 16) & (1 << (signum - 1)))
    except Exception:
        return False
    return False


def _main_pid_alive(pid: int) -> bool:
    try:
        if b"main.py" not in Path(f"/proc/{pid}/cmdline").read_bytes():
            return False
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.split(")", 1)[1].split()[0]
        return state != "Z"
    except Exception:
        return False


def _has_pending_unit_job() -> bool:
    """systemd 是否已有在途 job (如运维正在 restart/stop) — 有则让路。"""
    try:
        r = subprocess.run(["systemctl", "show", SERVICE, "-p", "Job"],
                           capture_output=True, text=True, timeout=10)
        line = r.stdout.strip()
        return bool(line.split("=", 1)[1].strip()) if "=" in line else False
    except Exception:
        return False


def dump_inprocess_stacks(pid: int, ts: str) -> str:
    """SIGUSR1 → faulthandler 全线程栈写 stderr (systemd 落到 ftseek_us.log)。

    只有目标进程确实捕获 SIGUSR1 才发送 — 未装 faulthandler 的旧进程收到 SIGUSR1
    的默认动作是终止, 会绕过 py-spy/SIGKILL 序列。
    """
    if not pid or not _signal_caught(pid, signal.SIGUSR1):
        return "SIGUSR1 未注册 (跳过进程内栈)"
    try:
        os.kill(pid, signal.SIGUSR1)
        return f"SIGUSR1 已发送 ({ts}, 栈见 logs/ftseek_us.log)"
    except Exception as e:
        return f"SIGUSR1 发送失败: {e}"


def hard_restart(pid: int, ts: str) -> tuple:
    """快速层处置: 双栈取证 → SIGKILL → 等 systemd Restart=always 拉起。

    返回 (status, inproc_note)。不调 systemctl restart — 僵死进程的 SIGTERM 会被
    忽略并拖满 120s (10/22、10/23 实证)。
    """
    inproc = dump_inprocess_stacks(pid, ts) if pid else "pid 未解析"
    if pid:
        time.sleep(3)  # 给 faulthandler 写完 stderr
        # 处置前复核单元状态, 避免与运维 systemctl stop/restart 抢方向盘
        state, sub, _result, _started = service_runtime_state()
        pending_job = _has_pending_unit_job()
        if state != "active" or sub != "running" or pending_job:
            return f"跳过硬停: 单元 {state}/{sub} (在途 job={pending_job})", inproc
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError as _gone_e:
            # 2026-10-28 except:pass 清剿: 目标已自行退出 (预期), 但硬停结论仍要能复盘。
            print(f"[info] SIGKILL 目标已不存在 (自行退出) pid={pid}: {_gone_e}", file=sys.stderr)
        except Exception as e:
            return f"SIGKILL 失败, 需人工: {e}", inproc
        deadline = time.time() + HARD_KILL_WAIT_S
        while time.time() < deadline and _main_pid_alive(pid):
            time.sleep(0.5)
        if _main_pid_alive(pid):
            return "SIGKILL 后进程仍在 (疑 D 状态), 需人工", inproc
    else:
        try:
            subprocess.run(["systemctl", "kill", "-s", "SIGKILL", "--kill-who=main",
                            SERVICE], capture_output=True, text=True, timeout=10)
        except Exception as e:
            return f"无 PID 且 systemctl kill 失败, 需人工: {e}", inproc

    # 等 Restart=always 拉起
    deadline = time.time() + RESTART_VERIFY_S
    while time.time() < deadline:
        state, sub, _result, _started = service_runtime_state()
        if state == "active" and sub == "running":
            return "SIGKILL 硬停成功, systemd 已自动拉起", inproc
        time.sleep(1)
    # 兜底: start-limit 击穿时 reset-failed + start
    try:
        subprocess.run(["systemctl", "reset-failed", SERVICE], timeout=60)
    except Exception as e:
        print(f"[freeze-sentinel] reset-failed 失败 (继续 start): {e}")
    try:
        subprocess.run(["systemctl", "start", SERVICE], timeout=200)
        return "自动拉起超时, reset-failed+start 已发出", inproc
    except Exception as e:
        return f"重启失败, 需人工: {e}", inproc


def acquire_run_lock():
    """防 cron 重叠 (硬停 + 告警可能 >60s); 拿不到锁返回 None, 锁文件不可写则照常运行。"""
    try:
        import fcntl
        f = open(RUN_LOCK, "a+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            return None
        return f
    except Exception:
        return _NO_LOCK


def do_revive() -> bool:
    """start-limit 击穿后 systemd 会拒绝再拉起 → 必须先 reset-failed 清计数."""
    try:
        subprocess.run(["systemctl", "reset-failed", SERVICE], timeout=60)
    except Exception as e:
        print(f"[freeze-sentinel] reset-failed 失败 (继续尝试 restart): {e}")
    return do_restart()


def alert(msg: str) -> None:
    try:
        sys.path.insert(0, str(REPO))
        from app.utils.critical_alerts import send_critical_sync
        send_critical_sync(msg)
    except Exception as e:
        print(f"[freeze-sentinel] alert failed (不阻断): {e}")


def main() -> int:
    run_lock = acquire_run_lock()
    if run_lock is None:
        return 0  # 上一轮仍在处置 (硬停+告警最长 >60s), 防 cron 重叠

    now_ts = time.time()
    now_cst = datetime.now()
    ts = now_cst.strftime("%Y%m%d_%H%M%S")
    try:
        hb_mtime = HEARTBEAT.stat().st_mtime
    except Exception:
        hb_mtime = None
    # 主线程"进度"心跳 (2026-11-03 长轮次豁免): 读不到 = None (不可豁免, 与旧行为一致)
    try:
        progress_mtime = PROGRESS.stat().st_mtime
    except Exception:
        progress_mtime = None
    phase_ok, basis = market_phase_allows(now_cst)
    active_state, sub_state, result, started_ts = service_runtime_state()

    # ── 存活层 (先判): 服务死了 → 冻结层无意义 (心跳必然停滞, 且 phase 门会挡住处置) ──
    laction, lreason = evaluate_liveness(active_state, result, now_ts, last_revive_ts())
    if laction == "skip":
        print(f"[{ts}] liveness skip: {lreason}")
    elif laction == "revive":
        pid = resolve_main_pid()
        stack_file = dump_stacks(pid, ts) if pid else "(进程不在, 无栈可取)"
        ok = do_revive()
        try:
            REVIVE_STATE.write_text(str(now_ts))
        except Exception as e:
            print(f"[freeze-sentinel] revive 冷却态写失败 (不阻断): {e}")
        msg = (f"🚨 [freeze-sentinel·存活层] ftseek-us 不在运行 "
               f"(ActiveState={active_state} Result={result}) — systemd 已放弃自愈 | "
               f"栈: {stack_file} | reset-failed+restart {'成功' if ok else '失败, 需人工'} | "
               f"⚠️ 存量持仓在本进程死亡期间无退出监控 (G-01 空洞)")
        print(f"[{ts}] {msg}")
        alert(msg)
        return 0

    # ── 快速冻结层 (DEB-20261023): 出场快循环是心跳源, 它卡住 = 退出监控停摆 ──
    faction, freason = evaluate_fast(now_ts, hb_mtime, active_state, sub_state,
                                     phase_ok, last_enforce_ts(), started_ts,
                                     progress_mtime=progress_mtime)
    if faction == "enforce":
        pid = resolve_main_pid()
        stack_file = dump_stacks(pid, ts) if pid else "(pid 未解析, 跳过 py-spy)"
        status, inproc = hard_restart(pid, ts)
        try:
            STATE.write_text(str(now_ts))
        except Exception as e:
            print(f"[freeze-sentinel] 冷却态写失败 (不阻断): {e}")
        age = int(now_ts - hb_mtime)
        msg = (f"🚨 [freeze-sentinel·快速层] 出场快循环心跳停滞 {age}s "
               f"(>{FAST_STALE_S}s) — 进程疑冻结 | py-spy: {stack_file} | "
               f"{inproc} | {status} | DEB-20261023")
        print(f"[{ts}] {msg}")
        alert(msg)
        return 0
    if faction == "skip" and hb_mtime is not None and (now_ts - hb_mtime) > FAST_STALE_S:
        print(f"[{ts}] fast skip: {freason} ({basis})")
        # 长轮次豁免必须留痕: 这是"本该杀但没杀"的唯一证据, 事后要能数出豁免了多少次。
        if freason.startswith("progressing("):
            alert(f"⏳ [freeze-sentinel·长轮次豁免] 出场心跳停滞 {int(now_ts - hb_mtime)}s 但主线程仍在推进"
                  f" ({freason}) — 不杀 (慢≠死); 超过 {PROGRESS_EXEMPT_MAX_S}s 仍未恢复则照旧 SIGKILL")

    action, reason = evaluate(now_ts, hb_mtime, active_state == "active",
                              phase_ok, last_enforce_ts())
    if action == "ok":
        return 0  # 健康静默
    if action == "skip":
        print(f"[{ts}] skip: {reason} ({basis}) | liveness={lreason}")
        return 0
    pid = resolve_main_pid()
    stack_file = dump_stacks(pid, ts) if pid else "(pid 未解析, 跳过取证)"
    status, inproc = hard_restart(pid, ts)
    try:
        STATE.write_text(str(now_ts))
    except Exception as e:
        print(f"[freeze-sentinel] 冷却态写失败 (不阻断): {e}")
    age = int(now_ts - hb_mtime) if hb_mtime is not None else -1
    msg = (f"🚨 [freeze-sentinel] 进程冻结处置: 心跳停滞 {age}s (>420s 兜底阈值) | "
           f"py-spy: {stack_file} | {inproc} | {status} | DEB-20261023")
    print(f"[{ts}] {msg}")
    alert(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
