import pytest

from app.playbook.pilot_policy import (
    MOM_ETF_V1,
    MOM_ETF_V1_ENTRIES_FLAG,
    MomDataIncompleteError,
    check_mom_exit,
    compute_mom_candidates,
    is_rebalance_day,
    max_hold_deadline,
    mom_budget_reject_reason,
    mom_etf_v1_decision_id,
    mom_final_order_reject_reason,
    mom_reprice_params,
    policy_for,
    previous_local_trading_day,
    rebalance_day_status,
)


@pytest.fixture(autouse=True)
def _clear_max_hold_deadline_cache():
    """到期日缓存是进程级状态, 逐测试清空防串味 (失败兜底也会入缓存)。"""
    from app.playbook import pilot_policy

    pilot_policy._MAX_HOLD_DEADLINE_CACHE.clear()
    pilot_policy._INVALID_ENTRY_TIME_LOGGED.clear()
    yield
    pilot_policy._MAX_HOLD_DEADLINE_CACHE.clear()
    pilot_policy._INVALID_ENTRY_TIME_LOGGED.clear()


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
    with pytest.raises(MomDataIncompleteError, match="bars=252"):
        compute_mom_candidates(short, "2026-11-11")


def test_momentum_fails_closed_when_universe_is_incomplete():
    closes = [100.0] * 253
    closes[-22] = 110.0
    closes[-1] = 120.0
    bars = {symbol: _bars(closes) for symbol in MOM_ETF_V1.universe}
    bars["US.XLY"] = bars["US.XLY"][:-1]
    with pytest.raises(MomDataIncompleteError, match="bars=252"):
        compute_mom_candidates(bars, "2026-11-11")


def test_max_hold_calendar_failure_uses_conservative_fallback(monkeypatch):
    from datetime import date, timedelta

    from app.playbook import pilot_policy

    def _boom(entry_day, policy):
        raise RuntimeError("calendar down")

    entry = date(2026, 11, 13)
    monkeypatch.setattr(pilot_policy, "_compute_max_hold_deadline", _boom)
    monkeypatch.setattr("app.utils.holiday_calendar.get_holiday_calendar",
                        lambda: pytest.fail("不得回落到 Futu 日历入口"))
    assert max_hold_deadline(entry) == entry + timedelta(days=29)


def test_max_hold_deadline_is_local_and_cached(monkeypatch):
    """21 交易日语义不变 + 纯本地 (不触 Futu) + 20s 热循环命中缓存。"""
    from datetime import date

    from app.playbook import pilot_policy

    monkeypatch.setattr("app.utils.holiday_calendar.get_holiday_calendar",
                        lambda: pytest.fail("热循环不得调用 Futu 日历单例"))
    entry = date(2026, 11, 13)
    # anchor 2026-11-13 起第 21 个交易日 (跳过 11-26 感恩节) = 2026-12-15
    assert max_hold_deadline(entry) == date(2026, 12, 15)

    calls = []
    real = pilot_policy._compute_max_hold_deadline
    monkeypatch.setattr(pilot_policy, "_compute_max_hold_deadline",
                        lambda day, policy: calls.append(day) or real(day, policy))
    assert max_hold_deadline(entry) == date(2026, 12, 15)
    assert calls == [], "同一 (entry_day, policy) 第二次调用应命中有界缓存"


def test_rebalance_day_anchor_cycle_and_observable_status():
    from datetime import date

    anchor = date(2026, 11, 13)
    status = rebalance_day_status(anchor)
    assert status.is_rebalance_day is True
    assert status.reason == "rebalance_day"
    assert status.trading_day_index == 0
    assert is_rebalance_day(anchor) is True

    # 第 21 个交易日 (2026-12-15) 是下一调仓日; 前一天不是
    assert is_rebalance_day(date(2026, 12, 15)) is True
    assert is_rebalance_day(date(2026, 12, 14)) is False
    assert rebalance_day_status(date(2026, 12, 14)).reason == "off_cycle"

    # anchor 之前 / 非交易日
    assert is_rebalance_day(date(2026, 11, 12)) is False
    assert rebalance_day_status(date(2026, 11, 12)).reason == "before_anchor"
    assert is_rebalance_day(date(2026, 11, 26)) is False  # 感恩节
    assert rebalance_day_status(date(2026, 11, 26)).reason == "not_trading_day"


def test_previous_local_trading_day_skips_weekend_and_holiday():
    from datetime import date

    assert previous_local_trading_day(date(2026, 11, 13)) == date(2026, 11, 12)
    assert previous_local_trading_day(date(2026, 11, 16)) == date(2026, 11, 13)
    # 2026-11-27 的上一交易日跳过感恩节，不依赖 Futu 日历单例。
    assert previous_local_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)


def test_rebalance_day_calendar_failure_is_observable_not_silent(monkeypatch, caplog):
    """日历故障不得与「非调仓日」同形: status.error 可见, bool API 仍 fail-close。"""
    from datetime import date

    from app.playbook import pilot_policy

    def _boom():
        raise RuntimeError("local calendar down")

    monkeypatch.setattr(pilot_policy, "_load_local_holidays", _boom)
    with caplog.at_level("ERROR", logger="app.playbook.pilot_policy"):
        status = rebalance_day_status(date(2026, 12, 15))
        assert status.is_rebalance_day is False
        assert status.reason == "calendar_unavailable"
        assert isinstance(status.error, RuntimeError)
        assert is_rebalance_day(date(2026, 12, 15)) is False
    assert "调仓日历不可用" in caplog.text
    assert "local calendar down" in caplog.text


def test_exit_priority_gap_sl_and_max_hold():
    from datetime import date, datetime, timedelta, timezone

    et = timezone(timedelta(hours=-5))
    now = datetime(2026, 11, 16, 10, 0, tzinfo=et)
    entry_time = "2026-11-13T10:00:00-05:00"
    assert check_mom_exit("LONG", 100.0, 94.0, 97.0, entry_time, now)[0] == "OVERNIGHT_GAP_STOP"
    assert check_mom_exit("LONG", 100.0, 96.0, 97.0, entry_time, now)[0] == "SL_INITIAL"
    same_day = datetime(2026, 11, 13, 10, 0, tzinfo=et)
    assert check_mom_exit("LONG", 100.0, 94.0, 97.0, entry_time, same_day)[0] == "SL_INITIAL"

    # 正常 MAX_HOLD 语义不变: 到期日当天/之后才退, 前一天不退
    before_deadline = datetime(2026, 12, 14, 10, 0, tzinfo=et)
    on_deadline = datetime(2026, 12, 15, 10, 0, tzinfo=et)
    assert check_mom_exit("LONG", 100.0, 101.0, 97.0, entry_time, before_deadline)[0] == ""
    assert check_mom_exit("LONG", 100.0, 101.0, 97.0, entry_time, on_deadline)[0] == "MAX_HOLD"
    assert max_hold_deadline(date(2026, 11, 13)) == date(2026, 12, 15)


def test_exit_with_missing_entry_time_never_fakes_max_hold(caplog):
    """P0: entry_time 缺失/非法不得伪造 MAX_HOLD 立即错误平仓。"""
    from datetime import datetime, timedelta, timezone

    et = timezone(timedelta(hours=-5))
    # 时间过去很久也不得按 MAX_HOLD 退 — 时间型退出缺证据 → 本轮不判定
    now = datetime(2027, 3, 1, 10, 0, tzinfo=et)
    with caplog.at_level("ERROR", logger="app.playbook.pilot_policy"):
        for bad in ("", None, "not-a-date", "2026-13-45T99:00:00"):
            assert check_mom_exit("LONG", 100.0, 101.0, 97.0, bad, now) == ("", 0.0)
    assert "entry_time 缺失/非法" in caplog.text

    # 价格型 SL_INITIAL 不依赖 entry_time, 必须继续生效
    assert check_mom_exit("LONG", 100.0, 96.0, 97.0, "", now) == ("SL_INITIAL", 96.0)
    assert check_mom_exit("LONG", 100.0, 96.0, 0.0, "garbage", now)[0] == "SL_INITIAL"


def test_invalid_entry_time_error_is_deduped_per_day(caplog):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
    with caplog.at_level("ERROR", logger="app.playbook.pilot_policy"):
        check_mom_exit("LONG", 100.0, 101.0, 97.0, "", now)
        check_mom_exit("LONG", 100.0, 101.0, 97.0, "", now)
        assert len(caplog.records) == 1
        check_mom_exit("LONG", 100.0, 101.0, 97.0, "", now + timedelta(days=1))
        assert len(caplog.records) == 2
    assert check_mom_exit("LONG", 100.0, 96.0, 97.0, "", now)[0] == "SL_INITIAL"


def test_exit_entry_time_naive_aware_combinations():
    """naive entry + aware now / aware entry + naive now 都要能算 entry_day。"""
    from datetime import datetime, timedelta, timezone

    et = timezone(timedelta(hours=-5))
    aware_now = datetime(2026, 11, 16, 10, 0, tzinfo=et)
    naive_now = datetime(2026, 11, 16, 10, 0)

    assert check_mom_exit(
        "LONG", 100.0, 94.0, 97.0, "2026-11-13T10:00:00", aware_now,
    )[0] == "OVERNIGHT_GAP_STOP"
    assert check_mom_exit(
        "LONG", 100.0, 94.0, 97.0, "2026-11-13T10:00:00-05:00", naive_now,
    )[0] == "OVERNIGHT_GAP_STOP"
    # 同一 ET 日 (23:00 CST = 10:00 ET) 不得误判隔夜跳空。
    assert check_mom_exit(
        "LONG", 100.0, 94.0, 97.0, "2026-11-13T23:00:00",
        naive_now.replace(day=13),
    )[0] == "SL_INITIAL"
    # naive=CST：03:00 CST 是前一日 14:00 ET，不能把 entry_day 错推一天。
    assert check_mom_exit(
        "LONG", 100.0, 94.0, 97.0, "2026-11-17T03:00:00",
        aware_now.replace(day=17),
    )[0] == "OVERNIGHT_GAP_STOP"


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


def test_rebalance_day_status_has_explicit_bool_semantics():
    """frozen dataclass 恒 truthy ⇒ 必须显式定义 __bool__ (2026-11-12 核查加固)。

    没有它, 任何 `if rebalance_day_status(...)` 误写都会让闸**每天都开**且不报错。
    """
    from datetime import date as _date_cls
    from app.playbook.pilot_policy import rebalance_day_status
    off = rebalance_day_status(_date_cls(2026, 11, 12))       # before_anchor
    on = rebalance_day_status(_date_cls(2026, 12, 15))        # 周期上
    assert off.is_rebalance_day is False and bool(off) is False
    assert on.is_rebalance_day is True and bool(on) is True
    assert not off and on                                # 误用形态下也给出正确结论
