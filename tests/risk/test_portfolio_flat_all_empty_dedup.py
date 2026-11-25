"""FLAT_ALL 空 managed 集告警去重回归 (2026-11-25 诊断项 A)。

背景: 账户级回撤 tripped 长期存续且 legacy managed 集为空时, 旧实现每轮打
2 条 CRITICAL (实测 2026-11-24 共 1231 轮/2462 条), 无任何平仓动作。
修复契约:
  1. 空仓段只告警一次 (WARNING, 非 CRITICAL), 返回值/冷却语义不变;
  2. 回撤解除或持仓恢复后重新计段, 再次触发仍会告警;
  3. 有仓时 CRITICAL + 平仓 + 冷却锁存语义逐字保留;
  4. 空仓 FLAT_ALL 不锁存冷却, 不阻塞随后有仓触发的立即执行。
"""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.playbook.pipeline as playbook_pipeline
import app.risk.position_exit_monitor as exit_monitor_module
from app.risk.position_exit_monitor import ExitMonitor


class FakePosition:
    def __init__(self, symbol):
        self.symbol = symbol
        self.direction = "LONG"
        self.avg_entry_price = 100.0
        self.size = 10
        self.metadata = {"source": "auto_main", "protection": "managed"}


def _monitor(monkeypatch, action="FLAT_ALL"):
    monitor = ExitMonitor()
    monkeypatch.setattr(
        exit_monitor_module,
        "get_shared_kernel",
        lambda: SimpleNamespace(
            get_current_session=lambda: "normal",
            get_session_vol_ratio=lambda session: 1.0,
        ),
    )
    monkeypatch.setattr(
        playbook_pipeline, "load_playbook_open_symbols", lambda: set()
    )
    monitor._check_portfolio_drawdown = lambda positions, get_price_fn: action
    return monitor


def _empty_warnings(records):
    return [
        r for r in records
        if r.levelno == logging.WARNING and "managed 持仓为空" in r.getMessage()
    ]


def test_empty_flat_all_warns_once_per_episode(monkeypatch, caplog):
    monitor = _monitor(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="ftseek.exit_monitor"):
        for _ in range(5):
            closed = monitor.scan_positions(
                [], get_price_fn=lambda s: 100.0,
                execute_close_fn=lambda p, r: True,
            )
            assert closed == []
    assert len(_empty_warnings(caplog.records)) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert monitor._flat_all_last_triggered_ts == 0.0


def test_empty_episode_resets_after_recovery(monkeypatch, caplog):
    monitor = _monitor(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="ftseek.exit_monitor"):
        monitor.scan_positions([], get_price_fn=lambda s: 100.0,
                               execute_close_fn=lambda p, r: True)
        monitor._check_portfolio_drawdown = lambda positions, get_price_fn: None
        monitor.scan_positions([], get_price_fn=lambda s: 100.0,
                               execute_close_fn=lambda p, r: True)
        monitor._check_portfolio_drawdown = lambda positions, get_price_fn: "FLAT_ALL"
        monitor.scan_positions([], get_price_fn=lambda s: 100.0,
                               execute_close_fn=lambda p, r: True)
    assert len(_empty_warnings(caplog.records)) == 2


def test_empty_flat_all_does_not_block_later_nonempty(monkeypatch):
    monitor = _monitor(monkeypatch)
    monitor.scan_positions([], get_price_fn=lambda s: 100.0,
                           execute_close_fn=lambda p, r: True)
    assert monitor._flat_all_last_triggered_ts == 0.0
    calls = []
    closed = monitor.scan_positions(
        [FakePosition("A")], get_price_fn=lambda s: 100.0,
        execute_close_fn=lambda p, r: calls.append(p.symbol) or True,
    )
    assert calls == ["A"]
    assert [c["symbol"] for c in closed] == ["A"]
    assert monitor._flat_all_last_triggered_ts > 0.0


def test_nonempty_flat_all_critical_and_cooldown_preserved(monkeypatch, caplog):
    monitor = _monitor(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="ftseek.exit_monitor"):
        closed = monitor.scan_positions(
            [FakePosition("A"), FakePosition("B")],
            get_price_fn=lambda s: 100.0,
            execute_close_fn=lambda p, r: True,
        )
    assert [c["symbol"] for c in closed] == ["A", "B"]
    assert monitor._flat_all_last_triggered_ts > 0.0
    assert any(
        r.levelno == logging.CRITICAL and "触发 FLAT_ALL" in r.getMessage()
        for r in caplog.records
    )
    assert not _empty_warnings(caplog.records)


def test_nonempty_partial_failure_keeps_retry_semantics(monkeypatch):
    monitor = _monitor(monkeypatch)
    calls = []

    def execute_close(position, reason):
        calls.append(position.symbol)
        return position.symbol == "A"

    closed = monitor.scan_positions(
        [FakePosition("A"), FakePosition("B")],
        get_price_fn=lambda s: 100.0, execute_close_fn=execute_close,
    )
    assert [c["symbol"] for c in closed] == ["A"]
    assert monitor._flat_all_last_triggered_ts == 0.0


def test_check_portfolio_drawdown_empty_suppresses_trigger_log(monkeypatch, caplog):
    import app.risk.capital_safety as capital_safety_module
    monitor = ExitMonitor()
    monkeypatch.setattr(
        capital_safety_module, "get_capital_safety_system",
        lambda: SimpleNamespace(_state=SimpleNamespace(
            current_equity=96000.0, peak_equity=100000.0)),
    )
    with caplog.at_level(logging.DEBUG, logger="ftseek.exit_monitor"):
        action = monitor._check_portfolio_drawdown([], lambda s: 100.0)
    assert action == "FLAT_ALL"
    assert not [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "组合止损·trigger" in r.getMessage()
    ]