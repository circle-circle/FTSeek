"""[2026-09-05 审计 G-02] 部署断层守卫契约测试.

锁住的语义:
  F1  drift ≤ 120s (GRACE_S) → ok, 不打扰
  F2  drift > 120s + 安全窗内 + 无冷却 → restart
  F3  drift > 120s + 窗外 → skip 但 reason 必须带 stale_drift (main 据此告警 — 纸面风控必须可见)
  F4  冷却期内 → skip
  F5  服务非 active → skip (交 freeze_sentinel 存活层, 不与之抢方向盘)
  F6  取不到进程启动时刻/代码 mtime → skip (不盲目重启)
  F7  安全窗 = 北京 09:00–12:00: 盘中会话 (21:30 CST 起) 与盘后分析 cron (05:00–07:30) 均在窗外
  F8  窗外漂移告警按 newest_code_ts 去重 (同一批部署只吼一次)
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "deploy_freshness_check", REPO / "scripts" / "deploy_freshness_check.py")
dfc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dfc)

NOW = 1_000_000.0
START = NOW - 3600.0          # 进程起于 1h 前
FRESH_CODE = START + 10.0     # 代码早于/接近启动 → 无断层
STALE_CODE = START + 1800.0   # 代码比进程新 30min → 断层
IN_WIN = 10 * 60              # 北京 10:00 (窗内)
OUT_WIN = 22 * 60             # 北京 22:00 (盘中会话, 窗外)


def test_fresh_ok():
    assert dfc.evaluate(NOW, START, FRESH_CODE, IN_WIN, "active", None)[0] == "ok"
    a, r = dfc.evaluate(NOW, START, START + dfc.GRACE_S, IN_WIN, "active", None)
    assert a == "ok", "drift == GRACE_S 边界含等号"


def test_stale_in_window_restarts():
    a, r = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, "active", None)
    assert a == "restart" and "stale_drift=1800s" in r


def test_stale_outside_window_skips_but_is_loud():
    a, r = dfc.evaluate(NOW, START, STALE_CODE, OUT_WIN, "active", None)
    assert a == "skip"
    assert r.startswith("stale_drift"), "reason 必须以 stale_drift 开头 — main 据此触发告警"


def test_cooldown_skip():
    a, r = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, "active",
                        NOW - dfc.RESTART_COOLDOWN_S + 60)
    assert a == "skip" and "cooldown" in r
    a2, _ = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, "active",
                         NOW - dfc.RESTART_COOLDOWN_S - 1)
    assert a2 == "restart", "冷却期满即恢复自愈能力"


def test_cooldown_state_from_text_file():
    """冷却态读自 /run 文本文件 → str。守卫不得因类型崩掉 (崩掉的守卫 == 没有守卫)。"""
    a, r = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, "active",
                        str(NOW - dfc.RESTART_COOLDOWN_S + 60))
    assert a == "skip" and "cooldown" in r
    a2, _ = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, "active",
                         str(NOW - dfc.RESTART_COOLDOWN_S - 1))
    assert a2 == "restart"
    a3, _ = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, "active", "garbage")
    assert a3 == "restart", "脏态文件按'无冷却记录'处理 → 仍自愈"


def test_service_not_active_defers_to_sentinel():
    for state in ("failed", "inactive", "activating", "unknown"):
        a, r = dfc.evaluate(NOW, START, STALE_CODE, IN_WIN, state, None)
        assert a == "skip" and state in r


def test_undetermined_inputs_never_restart():
    assert dfc.evaluate(NOW, None, STALE_CODE, IN_WIN, "active", None)[0] == "skip"
    assert dfc.evaluate(NOW, START, None, IN_WIN, "active", None)[0] == "skip"


def test_safe_window_excludes_sessions():
    # 盘中会话 (北京 21:30 开盘 → 次日 04:00 收盘) 与盘后分析 cron (05:00–07:30) 必须窗外
    for minutes in (0, 3 * 60, 4 * 60, 5 * 60, 7 * 60 + 30, 8 * 60 + 59,
                    12 * 60 + 1, 18 * 60, 20 * 60 + 45, 21 * 60 + 30, 23 * 60):
        assert not dfc.in_safe_window(minutes), f"{minutes // 60:02d}:{minutes % 60:02d} 应在窗外"
    for minutes in (9 * 60, 10 * 60 + 30, 12 * 60):
        assert dfc.in_safe_window(minutes), f"{minutes // 60:02d}:{minutes % 60:02d} 应在窗内"


def test_stale_notify_dedup_by_deploy_batch():
    assert dfc.should_notify_stale(None, STALE_CODE) is True
    assert dfc.should_notify_stale(str(STALE_CODE), STALE_CODE) is False
    assert dfc.should_notify_stale(str(STALE_CODE), STALE_CODE + 600) is True, (
        "新一批部署落盘 → 必须重新告警")
    assert dfc.should_notify_stale("garbage", STALE_CODE) is True


def test_scan_scope_excludes_config_and_scripts():
    """扫描面 = 被进程 import 的代码 (app/ + main.py)。config/*.yaml 热加载, scripts/ 由
    cron 独立进程运行 — 两者变更都不需要重启, 纳入会造成无谓重启 (H-12 状态损失)。"""
    assert dfc.SCAN_DIRS == ("app",)
    assert dfc.SCAN_FILES == ("main.py",)


def test_newest_code_ts_is_readable_and_recent():
    got = dfc.newest_code_ts()
    assert got is not None
    mtime, rel = got
    assert rel.endswith(".py") and mtime > 0
    assert not rel.startswith("scripts/"), "scripts/ 不应进入扫描面"


def test_guard_announces_its_own_blindness():
    """台账 §6 规则5: 探测器必须自带"我自己坏了"的输出路径。
    undetermined (取不到进程启动时刻/代码 mtime) 比漏报更糟 — 部署断层转为不可观测。"""
    src = (REPO / "scripts" / "deploy_freshness_check.py").read_text(encoding="utf-8")
    body = src[src.index("def main()"):]
    assert 'reason.startswith("undetermined")' in body
    idx = body.index('reason.startswith("undetermined")')
    assert "alert(" in body[idx:idx + 900], "失明必须外推 critical, 不能只 print 到 cron 日志"


def test_guard_has_no_silent_swallow():
    """本守卫自己不得含 except:pass/continue (棘轮纪律 + §6 规则5 自适用)。"""
    import re
    src = (REPO / "scripts" / "deploy_freshness_check.py").read_text(encoding="utf-8")
    hits = re.findall(r"except[^:\n]*:\s*\n\s*(pass|continue)\s*(?:#.*)?$", src, re.MULTILINE)
    assert not hits, f"守卫内出现静默吞错 {len(hits)} 处"


def test_proc_start_ts_matches_systemd():
    """进程龄必须与 systemd ActiveEnterTimestamp 同源 (台账 §6 规则6②: 严禁 stat /proc/PID)。
    容差 5s 覆盖两次读时钟的间隔。"""
    import subprocess, time
    pid = dfc.main_pid()
    if not pid:
        return
    got = dfc.proc_start_ts(pid)
    assert got is not None
    r = subprocess.run(["systemctl", "show", dfc.SERVICE,
                        "-p", "ActiveEnterTimestampMonotonic", "--value"],
                       capture_output=True, text=True, timeout=10)
    ref = time.time() - (time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6
                         - float(r.stdout.strip())) / 1e6
    assert abs(got - ref) < 5.0, f"进程龄口径漂移 {got - ref:+.1f}s (守卫会误判 fresh/stale)"
    src = (REPO / "scripts" / "deploy_freshness_check.py").read_text(encoding="utf-8")
    assert "stat -c %Y" not in src.split("严禁")[0], "不得用 /proc/PID 目录 mtime 当进程龄"
