import sys
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.risk.capital_safety as capital_safety_module
from app.risk.capital_safety import CapitalSafetySystem


def test_save_state_appends_history_and_keeps_current_single_row(tmp_path):
    db_path = str(tmp_path / "capital_state.db")
    system = CapitalSafetySystem(db_path=db_path, initial_capital=1000.0, trade_env=1)
    system._state.current_equity = 1100.0
    system._state.peak_equity = 1200.0
    system._state.drawdown_pct = -0.0833
    system._state.last_update = "2026-09-29T04:00:15.000000"
    system._save_state()

    system._state.current_equity = 1150.0
    system._state.last_update = "2026-09-30T04:00:15.000000"
    system._save_state()

    with sqlite3.connect(db_path) as conn:
        current = conn.execute(
            "SELECT initial_capital, current_equity, last_update, trade_env FROM capital_state"
        ).fetchall()
        history = conn.execute(
            "SELECT current_equity, snapshot_time, trade_env "
            "FROM capital_state_history ORDER BY snapshot_time"
        ).fetchall()

    assert current == [(1000.0, 1150.0, "2026-09-30T04:00:15.000000", 1)]
    assert history == [
        (1100.0, "2026-09-29T04:00:15.000000", 1),
        (1150.0, "2026-09-30T04:00:15.000000", 1),
    ]


def test_history_retention_is_per_trade_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "capital_history.db")
    monkeypatch.setattr(capital_safety_module, "CAPITAL_STATE_HISTORY_RETENTION", 2)
    system = CapitalSafetySystem(db_path=db_path, initial_capital=1000.0, trade_env=1)
    snapshots = [
        (1100.0, "2026-09-28T04:00:15.000000"),
        (1150.0, "2026-09-29T04:00:15.000000"),
        (1200.0, "2026-09-30T04:00:15.000000"),
    ]
    for equity, snapshot_time in snapshots:
        system._state.current_equity = equity
        system._state.last_update = snapshot_time
        system._save_state()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT snapshot_time FROM capital_state_history ORDER BY snapshot_time"
        ).fetchall()
        current_count = conn.execute("SELECT COUNT(*) FROM capital_state").fetchone()[0]

    assert rows == [("2026-09-29T04:00:15.000000",), ("2026-09-30T04:00:15.000000",)]
    assert current_count == 1


def test_history_write_failure_rolls_back_current_upsert(tmp_path):
    db_path = str(tmp_path / "capital_transaction.db")
    system = CapitalSafetySystem(db_path=db_path, initial_capital=1000.0, trade_env=1)
    system._state.current_equity = 1100.0
    system._state.last_update = "2026-09-29T04:00:15.000000"
    system._save_state()

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE capital_state_history")
    system._state.current_equity = 900.0
    system._state.last_update = "2026-09-30T04:00:15.000000"
    system._save_state()

    assert system._save_failed is True
    with sqlite3.connect(db_path) as conn:
        current_equity = conn.execute(
            "SELECT current_equity FROM capital_state WHERE trade_env = 1"
        ).fetchone()[0]
    assert current_equity == 1100.0


def test_save_state_without_last_update_skips_history_snapshot(tmp_path):
    """last_update 为空 (reset_capital / 首更前路径): SSOT 照写但不产历史快照 —
    无时间戳的行进 history 只会污染排序与窗口查询; 且不许用裸 now 兜底
    (naive-now 棘轮只降不升)。"""
    db_path = str(tmp_path / "capital_reset.db")
    system = CapitalSafetySystem(db_path=db_path, initial_capital=1000.0, trade_env=1)
    system._state.last_update = ""
    system._save_state()

    assert system._save_failed is False
    with sqlite3.connect(db_path) as conn:
        current = conn.execute("SELECT COUNT(*) FROM capital_state").fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM capital_state_history").fetchone()[0]
    assert current == 1
    assert history == 0


def test_load_state_filters_by_trade_env(tmp_path):
    db_path = str(tmp_path / "capital_state.db")

    sim = CapitalSafetySystem(
        db_path=db_path, initial_capital=1000.0, trade_env=1
    )
    sim._state.current_equity = 1100.0
    sim._state.peak_equity = 1200.0
    sim._state.is_locked = True
    sim._state.lock_reason = "sim_lock"
    sim._save_state()

    live = CapitalSafetySystem(
        db_path=db_path, initial_capital=5000.0, trade_env=0
    )
    live._state.current_equity = 4900.0
    live._state.peak_equity = 5100.0
    live._save_state()

    reloaded_live = CapitalSafetySystem(
        db_path=db_path, initial_capital=1.0, trade_env=0
    )
    assert reloaded_live._state.initial_capital == 5000.0
    assert reloaded_live._state.peak_equity == 5100.0
    assert reloaded_live._peak_initialized is True

    reloaded_sim = CapitalSafetySystem(
        db_path=db_path, initial_capital=1.0, trade_env=1
    )
    assert reloaded_sim._state.initial_capital == 1000.0
    assert reloaded_sim._state.peak_equity == 1200.0
    assert reloaded_sim._state.is_locked is True
    assert reloaded_sim._state.lock_reason == "sim_lock"


def test_reset_capital_none_persists_uninitialized_state(tmp_path):
    system = CapitalSafetySystem(
        db_path=str(tmp_path / "capital_reset.db"),
        initial_capital=1000.0,
        trade_env=1,
    )
    system._state.current_equity = 900.0
    system._state.peak_equity = 1200.0
    system._state.is_locked = True
    system._state.lock_reason = "old_lock"
    system._save_state()

    system.reset_capital(None)

    assert system._state.peak_equity == 0.0
    assert system._state.is_locked is False
    assert system._peak_initialized is False

    reloaded = CapitalSafetySystem(
        db_path=str(tmp_path / "capital_reset.db"),
        initial_capital=1.0,
        trade_env=1,
    )
    assert reloaded._state.peak_equity == 0.0
    assert reloaded._state.is_locked is False
    assert reloaded._peak_initialized is False


def test_singleton_rejects_trade_env_mismatch(monkeypatch):
    existing = SimpleNamespace(trade_env=1)
    monkeypatch.setattr(capital_safety_module, "_capital_instance", existing)

    assert capital_safety_module.get_capital_safety_system(trade_env=1) is existing
    with pytest.raises(RuntimeError, match="trade_env 不一致"):
        capital_safety_module.get_capital_safety_system(trade_env=0)
