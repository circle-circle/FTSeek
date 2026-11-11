import pytest

from app.playbook.pilot_policy import (
    MOM_ETF_V1,
    MOM_ETF_V1_ENTRIES_FLAG,
    check_mom_exit,
    compute_mom_candidates,
    max_hold_deadline,
    mom_budget_reject_reason,
    mom_etf_v1_decision_id,
    mom_final_order_reject_reason,
    mom_reprice_params,
    policy_for,
)


def test_default_and_existing_prefixes_keep_da_path():
    assert policy_for(None) is None
    assert policy_for("") is None
    assert policy_for("DA-20261112-US.AAPL") is None
    assert policy_for("EXT-20261112-US.QQQ-premarket") is None
    assert policy_for("universe_expansion:20261112:US.AAPL") is None


def test_mom_etf_v1_prefix_resolves_single_pilot_policy():
    decision_id = mom_etf_v1_decision_id("20261112", "US.QQQ")
    assert decision_id == "MOM-ETF-V1-20261112-US.QQQ"
    assert policy_for(decision_id) is MOM_ETF_V1
    assert mom_etf_v1_decision_id("2026-11-12", "US.QQQ") == decision_id


def test_pilot_policy_is_swing_allowlist_without_fixed_tp():
    assert MOM_ETF_V1.entry_mode == "scheduled"
    assert MOM_ETF_V1.exit_profile == "swing_allowlist"
    assert MOM_ETF_V1.max_hold_trading_days == 21
    assert MOM_ETF_V1.take_profit_enabled is False
    assert MOM_ETF_V1.exit_allowlist == ("SL_INITIAL", "OVERNIGHT_GAP_STOP", "MAX_HOLD")


def test_v1_freezes_geometry_weight_validity_and_cash_discipline():
    assert MOM_ETF_V1.initial_sl_pct == 3.0
    assert MOM_ETF_V1.overnight_gap_stop_pct == 5.0
    assert MOM_ETF_V1.weight_scheme == "equal_notional"
    assert MOM_ETF_V1.instruction_validity == "same_day_only"
    assert MOM_ETF_V1.rebalance_interval_trading_days == 21
    assert MOM_ETF_V1.cash_filter == "QQQ_CLOSE_ABOVE_MA200"


def test_pilot_allowlist_excludes_da_intraday_repairs():
    forbidden = {
        "PRE_CLOSE_EXIT",
        "EOD_FORCE_CLOSE",
        "SL_BREAKEVEN",
        "SL_TRAIL_LOCK",
        "DEAD_SIGNAL_EXIT",
        "MFE_STALL_EXIT",
        "TAKE_PROFIT",
        "W_WEEK",
    }
    assert forbidden.isdisjoint(MOM_ETF_V1.exit_allowlist)


def test_entry_flag_is_separate_from_policy_resolution():
    assert MOM_ETF_V1_ENTRIES_FLAG == "playbook.mom_etf_v1_entries_enabled"
    assert policy_for("MOM-ETF-V1-20261112-US.QQQ") is MOM_ETF_V1


def test_prefix_match_is_exact_and_case_sensitive():
    assert policy_for("mom-etf-v1-20261112-US.QQQ") is None
    assert policy_for("MOM-ETF-V2-20261112-US.QQQ") is None


def _bars(close_path, asof="2026-11-11"):
    rows = [[f"2026-01-{index % 28 + 1:02d}", 0, 0, 0, close]
            for index, close in enumerate(close_path)]
    rows[-1][0] = asof
    return rows


def test_momentum_uses_t_minus_252_denominator_and_requires_253_bars():
    closes = [100.0] * 253
    closes[0] = 100.0
    closes[-22] = 110.0
    closes[-1] = 120.0
    bars = {symbol: _bars(closes) for symbol in MOM_ETF_V1.universe}
    candidates = compute_mom_candidates(bars, "2026-11-11")
    assert candidates
    assert candidates[0]["momentum_12_1"] == pytest.approx(0.10)
    short = {symbol: _bars(closes[1:]) for symbol in MOM_ETF_V1.universe}
    assert compute_mom_candidates(short, "2026-11-11") == []


def test_momentum_fails_closed_when_universe_is_incomplete():
    closes = [100.0] * 253
    closes[-22] = 110.0
    closes[-1] = 120.0
    bars = {symbol: _bars(closes) for symbol in MOM_ETF_V1.universe}
    bars["US.XLY"] = bars["US.XLY"][:-1]
    assert compute_mom_candidates(bars, "2026-11-11") == []


def test_max_hold_calendar_failure_uses_conservative_fallback(monkeypatch):
    from datetime import date, timedelta

    class BrokenCalendar:
        def nth_trading_day_after(self, *args, **kwargs):
            raise RuntimeError("calendar down")

    monkeypatch.setattr("app.utils.holiday_calendar.get_holiday_calendar", lambda: BrokenCalendar())
    entry = date(2026, 11, 13)
    assert max_hold_deadline(entry) == entry + timedelta(days=29)


def test_exit_priority_gap_sl_and_max_hold():
    from datetime import datetime, timedelta, timezone

    et = timezone(timedelta(hours=-5))
    now = datetime(2026, 11, 16, 10, 0, tzinfo=et)
    entry_time = "2026-11-13T10:00:00-05:00"
    assert check_mom_exit("LONG", 100.0, 94.0, 97.0, entry_time, now)[0] == "OVERNIGHT_GAP_STOP"
    assert check_mom_exit("LONG", 100.0, 96.0, 97.0, entry_time, now)[0] == "SL_INITIAL"
    same_day = datetime(2026, 11, 13, 10, 0, tzinfo=et)
    assert check_mom_exit("LONG", 100.0, 94.0, 97.0, entry_time, same_day)[0] == "SL_INITIAL"
    assert check_mom_exit("LONG", 100.0, 101.0, 97.0, "", now)[0] == "MAX_HOLD"


def test_mom_reprice_and_final_size_checks():
    params = mom_reprice_params(
        {"entry_price": 95.0, "stop_loss": 92.0},
        {"lv3_ask": 100.0, "price": 99.5},
        "2026-11-13",
        fee_estimator=lambda quantity, price: 4.0,
    )
    assert params["entry_price"] == 100.0
    assert params["stop_loss"] == pytest.approx(97.0)
    assert params["take_profit"] == 0.0
    assert params["position_size"] == 49
    assert mom_final_order_reject_reason(
        674.76, 3, "2026-11-13", fee_estimator=lambda quantity, price: 4.13,
    ) == "MOM_FINAL_NOTIONAL_TOO_SMALL"
    assert mom_final_order_reject_reason(
        674.76, 7, "2026-11-13", fee_estimator=lambda quantity, price: 4.13,
    ) is None
    assert mom_final_order_reject_reason(
        674.76, 7, "2026-11-13", fee_estimator=lambda quantity, price: 100.0,
    ) == "MOM_FINAL_FEE_TOO_HIGH"


def test_budget_includes_global_buying_power_cap():
    assert mom_budget_reject_reason(
        4900, 0, 0, 0, 0, 100000, 16,
        global_deployed_notional=96000, global_budget_limit=100000,
    ) == "GLOBAL_BUYING_POWER_CAP"


def test_entry_window_boundaries_and_kill_switch_does_not_change_exit_policy(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.playbook import pilot_policy

    et = timezone(timedelta(hours=-5))
    assert not pilot_policy.in_entry_window(datetime(2026, 11, 13, 9, 49, tzinfo=et))
    assert pilot_policy.in_entry_window(datetime(2026, 11, 13, 9, 50, tzinfo=et))
    assert pilot_policy.in_entry_window(datetime(2026, 11, 13, 13, 59, tzinfo=et))
    assert not pilot_policy.in_entry_window(datetime(2026, 11, 13, 14, 0, tzinfo=et))
    assert pilot_policy.after_entry_cutoff(datetime(2026, 11, 13, 14, 0, tzinfo=et))

    monkeypatch.setattr(pilot_policy, "entries_enabled", lambda: False)
    assert policy_for("MOM-ETF-V1-20261113-US.QQQ") is MOM_ETF_V1
    now = datetime(2026, 11, 16, 10, 0, tzinfo=et)
    assert check_mom_exit(
        "LONG", 100.0, 96.0, 97.0, "2026-11-13T10:00:00-05:00", now,
    )[0] == "SL_INITIAL"
