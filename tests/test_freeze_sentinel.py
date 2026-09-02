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
