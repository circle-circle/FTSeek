"""[DEB-20260902 E0 · 2026-09-02 实施] 冻结外部哨兵契约测试.

锁住的语义:
  S1  心跳新鲜 (≤420s) → ok (不打扰)
  S2  心跳缺失 → skip (防重启循环; 非冻结形态)
  S3  停滞但服务未运行 → skip (systemd 管辖)
  S4  停滞但非 TRADING → skip (休市心跳停滞不是事故)
  S5  冷却期内 → skip
  S6  停滞+服务活+TRADING+无冷却 → enforce (420s 裁决阈值: 内部 heal 360s+60s)
  S7  时间窗兜底: 北京 周一22:00/周二03:00 命中; 周三12:00/周六12:00 不命中
  S8  相位 resolver 异常 → 落时间窗兜底

存活层 (2026-09-05 审计 G-01/R-5(ii) 伴生 — 与相位/心跳正交):
  S9  ActiveState=active/reloading → ok
  S10 activating/deactivating → skip (过渡态, 给 systemd 时间)
  S11 inactive + Result=success → skip (干净退出 ≈ 运维主动 stop, 不抢方向盘)
  S12 failed / 异常退出 → revive (reset-failed + restart + critical)
  S13 revive 冷却期内 → skip; systemctl 不可用 (unknown) → skip 不拦截

快速冻结层 (DEB-20261023):
  S14 心跳 ≤90s → ok
  S15 >90s + active/running + TRADING + 过启动宽限 + 无冷却 → enforce
  S16 非 active/running → skip (含 stop-sigterm 过渡态)
  S17 启动宽限内 / 启动时间不可知 → skip
  S18 非 TRADING → skip
  S19 冷却期内 → skip
  S20 快速层禁用 (FTSEEK_SENTINEL_FAST_S=0) → skip
  S21 心跳缺失 → skip
  S22 硬停路径: 双栈取证先于 SIGKILL, 且禁用 systemctl restart
"""
import importlib.util
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("freeze_sentinel", REPO / "scripts" / "freeze_sentinel.py")
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)

NOW = 1_000_000.0


def test_fresh_heartbeat_ok():
    assert fs.evaluate(NOW, NOW - 10, True, True, None)[0] == "ok"
    assert fs.evaluate(NOW, NOW - fs.STALE_S, True, True, None)[0] == "ok"  # 边界含等号


def test_missing_heartbeat_skip():
    assert fs.evaluate(NOW, None, True, True, None) == ("skip", "heartbeat_missing")


def test_stale_service_inactive_skip():
    a, r = fs.evaluate(NOW, NOW - 500, False, True, None)
    assert a == "skip" and "inactive" in r


def test_stale_not_trading_skip():
    a, r = fs.evaluate(NOW, NOW - 500, True, False, None)
    assert a == "skip" and "not_trading" in r


def test_cooldown_skip():
    a, r = fs.evaluate(NOW, NOW - 500, True, True, NOW - 60)
    assert a == "skip" and "cooldown" in r


def test_stale_trading_enforce():
    a, r = fs.evaluate(NOW, NOW - 421, True, True, None)
    assert a == "enforce" and "421" in r


def test_threshold_is_adjudicated_420():
    assert fs.STALE_S == 420, "E0 阈值为裁决值 420s (内部 heal 360s + 60s 余量), 改动须新裁决"


def test_clock_window():
    assert fs.clock_window_allows(datetime(2026, 9, 7, 22, 0))   # 周一 22:00 (盘中)
    assert fs.clock_window_allows(datetime(2026, 9, 8, 3, 0))    # 周二 03:00 (跨午夜段)
    assert not fs.clock_window_allows(datetime(2026, 9, 9, 12, 0))  # 周三中午
    assert not fs.clock_window_allows(datetime(2026, 9, 12, 12, 0))  # 周六中午
    assert not fs.clock_window_allows(datetime(2026, 9, 7, 20, 0))   # 周一 20:00 (早于窗口)


def test_phase_resolver_exception_falls_back_to_clock():
    def boom():
        raise TimeoutError("openD dead")
    ok, basis = fs.market_phase_allows(datetime(2026, 9, 7, 22, 0), resolver=boom)
    assert ok is True and "clock" in basis
    ok, basis = fs.market_phase_allows(datetime(2026, 9, 9, 12, 0), resolver=boom)
    assert ok is False and "clock" in basis


def test_phase_resolver_direct():
    ok, basis = fs.market_phase_allows(datetime(2026, 9, 7, 22, 0), resolver=lambda: True)
    assert ok is True and basis == "phase"
    ok, _ = fs.market_phase_allows(datetime(2026, 9, 7, 22, 0), resolver=lambda: False)
    assert ok is False


# ── 存活层 (S9–S13) ───────────────────────────────────────────────────────

def test_liveness_active_ok():
    assert fs.evaluate_liveness("active", "success", NOW, None) == ("ok", "active")
    assert fs.evaluate_liveness("reloading", "success", NOW, None)[0] == "ok"


def test_liveness_transitioning_skip():
    for state in ("activating", "deactivating"):
        a, r = fs.evaluate_liveness(state, "success", NOW, None)
        assert a == "skip" and state in r


def test_liveness_clean_stop_not_fought():
    """运维 systemctl stop 后 Restart=always 不拉起 → Result=success 的 inactive。
    哨兵若在此重启就是与操作者抢方向盘 (比停机更糟)。"""
    a, r = fs.evaluate_liveness("inactive", "success", NOW, None)
    assert a == "skip" and "clean_exit" in r


def test_liveness_failed_revives():
    """start-limit 击穿 (failed) 与异常退出都必须拉起 — 这是 G-01 的真实空洞:
    systemd 放弃后无告警, 存量仓在下一交易日开盘时无人看护。"""
    for state, result in (("failed", "start-limit-hit"), ("failed", "exit-code"),
                          ("inactive", "signal")):
        a, r = fs.evaluate_liveness(state, result, NOW, None)
        assert a == "revive", f"{state}/{result} 必须 revive"
        assert state in r


def test_liveness_revive_cooldown():
    a, r = fs.evaluate_liveness("failed", "start-limit-hit", NOW, NOW - 60)
    assert a == "skip" and "revive_cooldown" in r
    # 冷却期满即恢复处置能力
    a2, _ = fs.evaluate_liveness("failed", "start-limit-hit", NOW,
                                 NOW - fs.RESTART_COOLDOWN_S - 1)
    assert a2 == "revive"


def test_liveness_unknown_state_does_not_block():
    a, r = fs.evaluate_liveness("unknown", "unknown", NOW, None)
    assert a == "skip" and "state_unknown" in r


def test_liveness_is_phase_independent():
    """存活层签名不含相位参数 — 周末/假日死亡同样致命 (锁住"正交"设计)."""
    import inspect
    params = list(inspect.signature(fs.evaluate_liveness).parameters)
    assert params == ["active_state", "result", "now_ts", "last_revive_ts"]


def test_revive_path_resets_failed():
    """reset-failed 必须在 restart 之前: start-limit 击穿后 systemd 拒绝再拉起。"""
    src = (REPO / "scripts" / "freeze_sentinel.py").read_text(encoding="utf-8")
    body = src[src.index("def do_revive()"):]
    body = body[:body.index("\ndef ")]
    assert body.index("reset-failed") < body.index("do_restart()")


def test_main_runs_liveness_before_freeze_layer():
    src = (REPO / "scripts" / "freeze_sentinel.py").read_text(encoding="utf-8")
    body = src[src.index("def main()"):]
    assert body.index("evaluate_liveness(") < body.index("evaluate(now_ts, hb_mtime")
    assert "alert(msg)" in body, "存活层处置必须 critical 外推 (进程已死, 进程内通道不可达)"


# ── 快速冻结层 (DEB-20261023, S14–S22) ────────────────────────────────────

def _fast(now, age, active="active", sub="running", trading=True,
          cooldown=None, started_ago=3600, stale=None):
    return fs.evaluate_fast(
        now, None if age is None else now - age,
        active, sub, trading, cooldown,
        None if started_ago is None else now - started_ago,
        stale,
    )


def test_fast_threshold_is_90():
    assert fs.FAST_STALE_S == 90, "快速层 90s 为 DEB-20261023 裁决值, 改动须新裁决"


def test_fast_fresh_ok():
    assert _fast(NOW, 10)[0] == "ok"
    assert _fast(NOW, fs.FAST_STALE_S)[0] == "ok"  # 边界含等号


def test_fast_stale_enforce():
    a, r = _fast(NOW, 91)
    assert a == "enforce" and "91" in r


def test_fast_requires_running_unit():
    """10/22 的 stop-sigterm 过渡态必须让路 — 否则会与运维 stop 抢方向盘。"""
    assert _fast(NOW, 91, active="deactivating", sub="stop-sigterm")[0] == "skip"
    assert _fast(NOW, 91, active="inactive", sub="dead")[0] == "skip"
    assert _fast(NOW, 91, active="activating", sub="start-pre")[0] == "skip"


def test_fast_startup_grace_skip():
    a, r = _fast(NOW, 91, started_ago=fs.STARTUP_GRACE_S - 1)
    assert a == "skip" and "startup_grace" in r


def test_fast_start_time_unknown_skip():
    """启动时间不可知时不抢跑, 交回 420s 旧层 (防重启循环)。"""
    a, r = _fast(NOW, 91, started_ago=None)
    assert a == "skip" and "start_time_unknown" in r


def test_fast_not_trading_skip():
    a, r = _fast(NOW, 91, trading=False)
    assert a == "skip" and "not_trading" in r


def test_fast_cooldown_skip():
    a, r = _fast(NOW, 91, cooldown=NOW - 60)
    assert a == "skip" and "cooldown" in r


def test_fast_disabled_rollback_valve():
    a, r = _fast(NOW, 1000, stale=0)
    assert a == "skip" and "disabled" in r


def test_fast_missing_heartbeat_skip():
    a, r = _fast(NOW, None)
    assert a == "skip" and r == "heartbeat_missing"


def test_fast_hard_stop_sequence_dumps_then_kills():
    """取证先于硬停; 且快速层不得回退到 SIGTERM restart (僵死进程会拖满 120s)。"""
    src = (REPO / "scripts" / "freeze_sentinel.py").read_text(encoding="utf-8")
    body = src[src.index("def hard_restart("):]
    body = body[:body.index("\ndef ")]
    assert body.index("dump_inprocess_stacks(") < body.index("signal.SIGKILL")
    assert '"restart"' not in body, "硬停路径不得调用 systemctl restart"


def test_sentinel_run_lock_prevents_overlap():
    src = (REPO / "scripts" / "freeze_sentinel.py").read_text(encoding="utf-8")
    body = src[src.index("def main()"):]
    assert body.index("acquire_run_lock()") < body.index("evaluate_liveness(")
