from types import SimpleNamespace

import pytest

from app.core import config_container
from app.risk import account_circuit_breaker as acb, capital_safety


@pytest.fixture
def policy(monkeypatch):
    values = {"risk.account.sim.max_peak_drawdown": 0.05}
    state = SimpleNamespace(peak_equity=100000.0)
    monkeypatch.setattr(config_container, "get_config", lambda: SimpleNamespace(
        get_dotted=lambda key, default=None: values.get(key, default)))
    monkeypatch.setattr(capital_safety, "get_capital_safety_system",
                        lambda: SimpleNamespace(_state=state))
    monkeypatch.setattr(capital_safety, "get_capital_safety_system_for_arm",
                        lambda arm: SimpleNamespace(_state=state))
    breaker = acb.AccountCircuitBreaker()
    monkeypatch.setattr(breaker, "_get_daily_pnl", lambda today, arm: 0.0)
    return breaker, values, state


@pytest.mark.parametrize("arm,equity,action", [
    ("sim", 96894.392, "NORMAL"),
    ("sim", 95000.01, "NORMAL"),
    ("sim", 95000.0, "LIQUIDATE"),
    ("sim", 94999.99, "LIQUIDATE"),
    ("real", 97000.01, "NORMAL"),
    ("real", 97000.0, "LIQUIDATE"),
    ("live", 96894.392, "LIQUIDATE"),
])
def test_peak_budget_is_arm_specific(policy, arm, equity, action):
    breaker, _, state = policy
    result = breaker.check(equity, arm)
    assert result.action == action
    assert result.peak_equity == state.peak_equity == 100000.0
    assert result.current_equity == equity


@pytest.mark.parametrize("arm", ["sim", "real"])
@pytest.mark.parametrize("loss,action", [
    (-999.99, "NORMAL"), (-1000.0, "REDUCE_50"),
    (-1999.99, "REDUCE_50"), (-2000.0, "HALT"),
    (-2999.99, "HALT"), (-3000.0, "LIQUIDATE"),
])
def test_daily_limits_remain_one_two_three_percent(policy, monkeypatch, arm, loss, action):
    breaker, _, state = policy
    monkeypatch.setattr(breaker, "_get_daily_pnl", lambda today, env: loss)
    assert breaker.check(state.peak_equity, arm).action == action
    assert breaker.liquidate_threshold == 0.03


@pytest.mark.parametrize("arm", ["real", "live", " real ", " LIVE "])
def test_live_normalization_uses_live_peak_and_daily_arm(policy, monkeypatch, arm):
    breaker, _, sim_state = policy
    live_state = SimpleNamespace(peak_equity=200000.0)
    peak_arms = []
    daily_arms = []

    def live_capital(env):
        peak_arms.append(env)
        return SimpleNamespace(_state=live_state)

    def daily_pnl(today, env):
        daily_arms.append(env)
        return 0.0

    monkeypatch.setattr(capital_safety, "get_capital_safety_system_for_arm", live_capital)
    monkeypatch.setattr(breaker, "_get_daily_pnl", daily_pnl)
    result = breaker.check(100000.0, arm)
    assert result.action == "LIQUIDATE"
    assert result.peak_equity == live_state.peak_equity
    assert peak_arms == daily_arms == [arm.strip().lower()]
    assert sim_state.peak_equity == 100000.0


def test_hotread_does_not_require_singleton_rebuild(policy, monkeypatch):
    breaker, values, state = policy
    monkeypatch.setattr(acb, "_circuit_breaker_instance", breaker)
    assert acb.check_circuit_breaker(96894.392).action == "NORMAL"
    values["risk.account.sim.max_peak_drawdown"] = 0.03
    assert acb.check_circuit_breaker(96894.392).action == "LIQUIDATE"
    values["risk.account.sim.max_peak_drawdown"] = 0.05
    assert acb.check_circuit_breaker(96894.392).action == "NORMAL"
    assert state.peak_equity == 100000.0


def test_absent_sim_override_retains_original_limit(policy):
    breaker, values, _ = policy
    values.clear()
    assert acb.get_peak_drawdown_threshold("sim") == 0.03
    assert breaker.check(96894.392).action == "LIQUIDATE"


@pytest.mark.parametrize("bad", [None, True, False, "five", float("nan"),
                                 float("inf"), -0.05, 0, 1, 5])
def test_invalid_sim_override_defers_without_affecting_live(policy, bad):
    breaker, values, _ = policy
    values["risk.account.sim.max_peak_drawdown"] = bad
    with pytest.raises((ValueError, TypeError)):
        acb.get_peak_drawdown_threshold("sim")
    assert breaker.check(96894.392).action == "HALT"
    assert breaker.check(100000.0, "real").action == "NORMAL"
    assert acb.get_peak_drawdown_threshold("real") == 0.03


@pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf")])
def test_unavailable_peak_never_means_zero_drawdown(policy, bad):
    breaker, _, state = policy
    state.peak_equity = bad
    assert breaker.check(96894.392).action == "HALT"
    assert state.peak_equity is bad


@pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf")])
def test_invalid_equity_cannot_admit(policy, bad):
    breaker, _, _ = policy
    assert breaker.check(bad).action == "LIQUIDATE"


def test_peak_read_failure_defers(policy, monkeypatch):
    breaker, _, state = policy

    def unavailable():
        raise RuntimeError("capital snapshot unavailable")

    monkeypatch.setattr(capital_safety, "get_capital_safety_system", unavailable)
    assert breaker.check(96894.392).action == "HALT"
    assert state.peak_equity == 100000.0


@pytest.mark.parametrize("daily,action", [
    (float("nan"), "HALT"), (float("inf"), "HALT"),
    (-float("inf"), "LIQUIDATE"),
])
def test_unknown_daily_pnl_cannot_admit(policy, monkeypatch, daily, action):
    breaker, _, _ = policy
    monkeypatch.setattr(breaker, "_get_daily_pnl", lambda today, env: daily)
    assert breaker.check(100000.0).action == action


def test_unknown_arm_cannot_use_sim_override(policy):
    breaker, _, _ = policy
    with pytest.raises(ValueError):
        acb.get_peak_drawdown_threshold("unknown")
    assert breaker.check(100000.0, "unknown").action == "HALT"
