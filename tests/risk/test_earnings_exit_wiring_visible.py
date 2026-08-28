"""财报强制退出链"失效可见"回归测试 (2026-08-28, 全链路复查 P1-4).

背景: position_exit_monitor._check_earnings_exit (Task #24, 2026-05-27) 从
app.risk.pre_trade_check 导入 check_earnings_exit_required — 该函数**不存在**
(生产 venv 实证: ImportError: cannot import name ...; pre_trade_check 内无任何
earnings 符号)。而 ImportError 落在 `except Exception: logger.debug(...)` 里 →
日志形态"正常", 实际 P0 财报强平规则自 2026-05-27 起一行判断都没执行过。

本轮只修"可见性", 不擅自改接 EarningsRiskGuard: 强制平仓是 P0 资金动作, 真实
接线 (含数据源与平仓授权) 待用户裁决。建单侧 EarningsRiskGuard 财报 blackout 门
仍在生效 (生产实证 109 只/未来7天)。

覆盖点:
  1. 一次性探测: 未接线 → 返回 False 且打 ERROR (含"未接线"), 每进程只打一次
  2. 未接线时 _check_earnings_exit 返回 None 且不抛 (行为与修复前一致)
  3. 接线后 (注入同名函数) P0 在窗口内 → 退出动作 dict (skip_scorer/P0)
  4. 接线后窗口外 → None (时段守卫不变)
  5. 运行时异常从 debug 升为 WARNING, 且 300s 节流 (不再刷屏也不再隐形)

回滚: 删本文件即可, 零生产代码依赖。
"""
import sys
import types
from datetime import datetime as _real_datetime, timedelta as _real_timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import app.risk.position_exit_monitor as pem
import app.risk.pre_trade_check as ptc
from app.risk.position_exit_monitor import ExitMonitor


@pytest.fixture(autouse=True)
def _reset_probe(monkeypatch):
    """模块级一次性状态复位, 用例间不串."""
    monkeypatch.setattr(pem, "_EARNINGS_EXIT_WIRED", None, raising=False)
    monkeypatch.setattr(pem, "_EARNINGS_EXIT_LAST_ERR_TS", 0.0, raising=False)
    yield


@pytest.fixture
def monitor():
    return ExitMonitor.__new__(ExitMonitor)   # 绕开重量级 __init__


class _FrozenDatetime(_real_datetime):
    """只冻结 now(); 其余沿用真实 datetime."""
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen if tz is None else cls._frozen.astimezone(tz)


def _freeze_et(y, mo, d, h, mi):
    import pytz
    et = pytz.timezone("US/Eastern")
    _FrozenDatetime._frozen = et.localize(_real_datetime(y, mo, d, h, mi))
    return _FrozenDatetime


# ── 1/2. 未接线: 一次性 ERROR + 行为不变 ──
def test_unwired_chain_logs_one_shot_error(monitor, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        r1 = monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU"))
        r2 = monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU"))

    assert r1 is None and r2 is None
    assert pem._EARNINGS_EXIT_WIRED is False
    hits = [rec for rec in caplog.records if "未接线" in rec.getMessage()]
    assert len(hits) == 1, "每进程只打一次, 但必须打"
    assert hits[0].levelno == logging.ERROR
    # 旧行为留痕: 缺陷曾只落 debug → 生产日志不可见
    debug_hits = [rec for rec in caplog.records
                  if "检查异常" in rec.getMessage() and rec.levelno <= logging.DEBUG]
    assert debug_hits == []


def test_probe_reports_unwired_in_production_tree():
    """当前代码库事实: pre_trade_check 没有 check_earnings_exit_required."""
    assert hasattr(ptc, "check_earnings_exit_required") is False
    assert pem._probe_earnings_exit_api() is False


# ── 3. 接线后 P0 动作 ──
def test_wired_p0_returns_forced_exit(monitor, monkeypatch):
    def _fake_required(symbol):
        return {"priority": "P0", "reason": "财报前24h", "deadline": "10:00"}

    monkeypatch.setattr(ptc, "check_earnings_exit_required", _fake_required, raising=False)
    monkeypatch.setattr(pem, "_EARNINGS_EXIT_WIRED", True)
    monkeypatch.setattr(pem, "datetime", _freeze_et(2026, 8, 28, 9, 35))

    r = monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU"))
    assert r is not None
    assert r["reason"] == "earnings_exit_p0"
    assert r["priority"] == "P0" and r["skip_scorer"] is True
    assert r["close_ratio"] == 1.0


def test_wired_p1_only_warns(monitor, monkeypatch, caplog):
    monkeypatch.setattr(
        ptc, "check_earnings_exit_required",
        lambda symbol: {"priority": "P1", "reason": "财报临近"},
        raising=False,
    )
    monkeypatch.setattr(pem, "_EARNINGS_EXIT_WIRED", True)
    monkeypatch.setattr(pem, "datetime", _freeze_et(2026, 8, 28, 9, 35))

    import logging
    with caplog.at_level(logging.WARNING):
        assert monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU")) is None
    assert "P1 建议退出" in caplog.text


# ── 4. 窗口守卫不变 ──
def test_wired_outside_opening_window_returns_none(monitor, monkeypatch):
    monkeypatch.setattr(
        ptc, "check_earnings_exit_required",
        lambda symbol: pytest.fail("窗口外不应查询财报 API"),
        raising=False,
    )
    monkeypatch.setattr(pem, "_EARNINGS_EXIT_WIRED", True)
    monkeypatch.setattr(pem, "datetime", _freeze_et(2026, 8, 28, 11, 0))

    assert monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU")) is None


# ── 5. 运行时异常升为 WARNING + 节流 ──
def test_runtime_failure_is_visible_and_throttled(monitor, monkeypatch, caplog):
    calls = {"n": 0}

    def _boom(symbol):
        calls["n"] += 1
        raise RuntimeError("财报 API 超时")

    monkeypatch.setattr(ptc, "check_earnings_exit_required", _boom, raising=False)
    monkeypatch.setattr(pem, "_EARNINGS_EXIT_WIRED", True)
    monkeypatch.setattr(pem, "datetime", _freeze_et(2026, 8, 28, 9, 35))

    import logging
    with caplog.at_level(logging.DEBUG):
        monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU"))
        monitor._check_earnings_exit(types.SimpleNamespace(symbol="US.MU"))

    warns = [rec for rec in caplog.records if "检查异常" in rec.getMessage()]
    assert len(warns) == 1, "同一异常 300s 内只打一次"
    assert warns[0].levelno == logging.WARNING
    assert calls["n"] == 2   # 节流只限日志, 不限检查本身
