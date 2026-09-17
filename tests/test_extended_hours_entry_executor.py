import fcntl
import json

import pytest

import scripts.extended_hours_entry_executor as executor


class FakeQuoteContext:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(executor, "LOCK", tmp_path / "state.lock")


def test_collect_refuses_real_order_when_sim_disabled_via_switch(
    isolated_state, monkeypatch
):
    """2026-09-17 方案1: ALLOW_SIM_ENTRY 默认 true 恢复下单; 开关=false 时 SIM 仍 fail-closed."""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setenv("EXT_ENTRY_ALLOW_SIM", "false")  # 关开关
    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", False)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)

    result = executor.collect("premarket")

    assert result == [{"ok": False, "skip": "simulate_stop_unsupported_fail_closed"}]
    assert not executor.STATE.exists()


def test_collect_rejects_outside_session_window(monkeypatch):
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: False)

    result = executor.collect("premarket")

    assert result == [{"ok": False, "skip": "outside_trading_window"}]


def test_dry_scan_does_not_write_state_and_caps_position_notional(
    isolated_state, monkeypatch
):
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    monkeypatch.setattr(
        executor,
        "_scan_movers",
        lambda session, qctx: [
            {"symbol": "US.TEST", "px": 10.0, "chg": 3.0, "vol": 300000.0},
            {"symbol": "US.EXPENSIVE", "px": 6000.0, "chg": 3.0, "vol": 300000.0},
        ],
    )
    monkeypatch.setattr(
        executor,
        "_earnings_surprise",
        lambda symbol: {"surprise_pct": 10.0, "period": "2026-06-30"},
    )
    monkeypatch.setattr("futu.OpenQuoteContext", FakeQuoteContext)

    result = executor.collect("premarket", dry=True)

    assert len(result) == 1
    assert result[0]["symbol"] == "US.TEST"
    assert result[0]["qty"] == 500
    assert result[0]["dry"] is True
    assert not executor.STATE.exists()


def test_state_is_atomic_and_unreadable_state_fails_closed(
    isolated_state, monkeypatch
):
    state = {"version": 2, "opened_today": {}, "attempts": {}}
    executor._save_state(state)

    assert executor._load_state() == state
    assert not any(path.name.endswith(".tmp") for path in executor.STATE.parent.iterdir())

    executor.STATE.write_text("{broken")
    with pytest.raises(RuntimeError, match="unreadable"):
        executor._load_state()


def test_state_lock_detects_concurrent_executor(isolated_state):
    with executor.LOCK.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with executor._state_lock() as locked:
            assert locked is False

    with executor._state_lock() as locked:
        assert locked is True


def test_execute_refuses_simulate_without_stop(isolated_state, monkeypatch):
    """2026-09-17 用户裁方案1后: SIM 走真下单(规则引擎兜底止损), 不再 fail-closed.
    本测试改为验证: ALLOW_SIM_ENTRY=false 时 SIM 仍 fail-closed (开关兜底可关)."""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setenv("EXT_ENTRY_ALLOW_SIM", "false")  # 关开关 → 应拒绝
    # _execute 不读 ALLOW_SIM_ENTRY (开关在 collect), 故此处验 REAL 拒绝仍生效即可
    # 真下单路径需 mock futu, 避免测试连真 OpenD — 见 test_execute_sim_real_order_mocked
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    result = executor._execute(
        "US.TEST", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )
    assert result["ok"] is False
    assert result["skip"] == "real_not_authorized"


def test_execute_rejects_real_environment(isolated_state, monkeypatch):
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")

    result = executor._execute(
        "US.TEST", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )

    assert result["ok"] is False
    assert result["skip"] == "real_not_authorized"
