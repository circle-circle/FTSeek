"""Per-arm circuit-breaker contracts: LIVE never consumes SIM loss counters."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import app.risk.absolute_circuit_breaker as acb_module
import app.risk.account_circuit_breaker as okr_module
from app.risk.absolute_circuit_breaker import AbsoluteCircuitBreaker
from app.risk.account_circuit_breaker import AccountCircuitBreaker


TODAY = "2026-10-02"
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def _make_ledger(path, source_type, pnl):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, exit_time TEXT, exit_price REAL, "
        "realized_pnl REAL, pnl_net REAL, source_type TEXT)"
    )
    connection.execute(
        "INSERT INTO trade_records "
        "(exit_time, exit_price, realized_pnl, pnl_net, source_type) "
        "VALUES (?, 10.0, ?, ?, ?)",
        (f"{TODAY}T09:30:00-04:00", pnl, pnl, source_type),
    )
    connection.commit()
    connection.close()


def test_account_circuit_breaker_reads_arm_ledger(monkeypatch, tmp_path):
    sim_db = tmp_path / "sim.db"
    live_db = tmp_path / "live.db"
    _make_ledger(sim_db, "PLAYBOOK_SIM", -100.0)
    _make_ledger(live_db, "PLAYBOOK_LIVE", -200.0)

    monkeypatch.setattr(
        okr_module,
        "get_trade_records_db_for_arm",
        lambda target_env: live_db if target_env == "real" else sim_db,
    )

    breaker = AccountCircuitBreaker()
    assert breaker._get_daily_pnl(TODAY, "sim") == -100.0
    assert breaker._get_daily_pnl(TODAY, "real") == -200.0


def test_account_circuit_breaker_sim_stays_pinned_when_legacy_env_flips(
    monkeypatch, tmp_path
):
    sim_db = tmp_path / "sim.db"
    live_db = tmp_path / "live.db"
    _make_ledger(sim_db, "PLAYBOOK_SIM", -100.0)
    _make_ledger(live_db, "PLAYBOOK_LIVE", -200.0)

    monkeypatch.setenv("TRADE_ENV", "0")
    monkeypatch.setattr(
        okr_module,
        "get_trade_records_db_for_arm",
        lambda target_env: live_db if target_env == "real" else sim_db,
    )

    breaker = AccountCircuitBreaker()
    assert breaker._get_daily_pnl(TODAY, "sim") == -100.0
    assert breaker._get_daily_pnl(TODAY, "real") == -200.0


def test_absolute_circuit_breaker_instances_are_arm_isolated(monkeypatch, tmp_path):
    sim_db = tmp_path / "sim.db"
    live_db = tmp_path / "live.db"
    _make_ledger(sim_db, "PLAYBOOK_SIM", -300.0)
    _make_ledger(live_db, "PLAYBOOK_LIVE", -400.0)

    monkeypatch.setattr(AbsoluteCircuitBreaker, "_instance", None)
    monkeypatch.setattr(AbsoluteCircuitBreaker, "_real_instance", None)
    monkeypatch.setattr(acb_module, "now_et", lambda: NOW)
    monkeypatch.setattr(acb_module, "_et_trade_date", lambda _dt: TODAY)
    monkeypatch.setattr("app.core.paths.get_trade_records_db", lambda: sim_db)
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm",
        lambda target_env: live_db if target_env == "real" else sim_db,
    )

    sim_breaker = AbsoluteCircuitBreaker("sim")
    live_breaker = AbsoluteCircuitBreaker("real")

    assert sim_breaker is not live_breaker
    assert sim_breaker.target_env == "sim"
    assert live_breaker.target_env == "real"
    assert sim_breaker._daily_pnl == -300.0
    assert live_breaker._daily_pnl == -400.0


def test_absolute_circuit_breaker_sim_stays_pinned_when_legacy_env_flips(
    monkeypatch, tmp_path
):
    sim_db = tmp_path / "sim.db"
    live_db = tmp_path / "live.db"
    _make_ledger(sim_db, "PLAYBOOK_SIM", -300.0)
    _make_ledger(live_db, "PLAYBOOK_LIVE", -400.0)

    monkeypatch.setenv("TRADE_ENV", "0")
    monkeypatch.setattr(AbsoluteCircuitBreaker, "_instance", None)
    monkeypatch.setattr(AbsoluteCircuitBreaker, "_real_instance", None)
    monkeypatch.setattr(acb_module, "now_et", lambda: NOW)
    monkeypatch.setattr(acb_module, "_et_trade_date", lambda _dt: TODAY)
    monkeypatch.setattr("app.core.paths.get_trade_records_db", lambda: live_db)
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm",
        lambda target_env: live_db if target_env == "real" else sim_db,
    )

    sim_breaker = AbsoluteCircuitBreaker("sim")
    live_breaker = AbsoluteCircuitBreaker("real")

    assert sim_breaker._daily_pnl == -300.0
    assert live_breaker._daily_pnl == -400.0


def test_capital_safety_sim_arm_is_pinned_when_legacy_env_flips(monkeypatch, tmp_path):
    import app.risk.capital_safety as capital_safety_module

    sim_db = tmp_path / "sim.db"
    live_db = tmp_path / "live.db"

    monkeypatch.setenv("TRADE_ENV", "0")
    monkeypatch.setattr(capital_safety_module, "_capital_instance", None)
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm",
        lambda target_env: live_db if target_env == "real" else sim_db,
    )

    capital = capital_safety_module.get_capital_safety_system_for_arm("sim")

    assert Path(capital._db_path) == sim_db
    assert capital.trade_env == 1
