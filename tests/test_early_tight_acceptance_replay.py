from scripts import early_tight_acceptance_replay as replay


def _trade(direction="LONG"):
    return {
        "trade_id": 1,
        "symbol": "US.X",
        "direction": direction,
        "entry_time": "2026-11-05T09:50:30",
        "exit_time": "2026-11-05T10:00:30",
        "entry_price": 100.0,
        "actual_exit_price": 97.5 if direction == "LONG" else 101.2,
        "tight_sl": 98.0 if direction == "LONG" else 101.0,
        "initial_sl": 95.0 if direction == "LONG" else 105.0,
        "quantity": 10,
        "actual_gross": -25.0 if direction == "LONG" else -12.0,
        "actual_net": -30.0 if direction == "LONG" else -17.0,
        "fees_est": 5.0,
    }


def _bar(minute, open_, high, low, close):
    return [f"2026-11-05 10:{minute:02d}:00", open_, high, low, close]


def _eod(close):
    return ["2026-11-05 15:55:00", close, close, close, close]


def test_long_close_acceptance_then_eod_uses_net_delta():
    trade = _trade()
    trade.update(actual_exit_price=98.0, actual_gross=-20.0, actual_net=-25.0)
    bars = [
        _bar(0, 99, 99, 96, 97),
        _bar(1, 97, 98, 96, 97.5),
        _bar(2, 97.5, 100, 97.4, 99),
        _bar(3, 99, 100, 98.5, 99.5),
        _eod(105),
    ]
    result = replay.evaluate_trade(trade, bars)
    assert result["status"] == "ok"
    assert result["close_accepted_120s"] is True
    assert result["recovered_at_bar"] == 1
    assert result["candidate_reason"] == "EOD_AFTER_RECOVERY"
    assert result["candidate_net"] == 45.0
    assert result["delta_net"] == 70.0
    assert result["effective_grace_sec"] == 150


def test_short_no_acceptance_exits_at_second_full_bar_close():
    trade = _trade("SHORT")
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 102, 100, 101.2),
        _bar(2, 101.2, 102, 101, 101.4),
        _bar(3, 101.4, 102, 101.2, 101.5),
        _eod(99),
    ]
    result = replay.evaluate_trade(trade, bars)
    assert result["status"] == "ok"
    assert result["close_accepted_120s"] is False
    assert result["candidate_reason"] == "GRACE_EXPIRED"
    assert result["candidate_net"] == -20.0
    assert result["delta_net"] == -3.0
    assert result["delta_bps"] < 0


def test_missing_net_field_falls_back_to_gross_minus_fees():
    trade = _trade("SHORT")
    trade["actual_net"] = None
    bars = [
        _bar(1, 100, 102, 100, 101.2),
        _bar(2, 101.2, 102, 101, 101.4),
        _bar(3, 101.4, 102, 101.2, 101.5),
        _eod(99),
    ]
    result = replay.evaluate_trade(trade, bars)
    assert result["actual_net"] == -17.0
    assert result["candidate_net"] == -20.0
    assert result["delta_net"] == -3.0


def test_original_stop_is_hard_backstop_with_gap_fill():
    trade = _trade()
    bars = [
        _bar(1, 97, 98, 96, 97.5),
        _bar(2, 94, 96, 93, 96),
        _bar(3, 96, 97, 95, 96),
        _eod(100),
    ]
    result = replay.evaluate_trade(trade, bars)
    assert result["status"] == "ok"
    assert result["candidate_reason"] == "ORIGINAL_SL_DURING_GRACE"
    assert result["candidate_exit_price"] == 94.0


def test_same_bar_hard_stop_and_recovery_has_explicit_two_sided_bound():
    trade = _trade()
    bars = [
        _bar(1, 97, 98, 96, 97.5),
        _bar(2, 97, 100, 94, 99),
        _bar(3, 99, 100, 98, 99),
        _eod(104),
    ]
    pessimistic = replay.evaluate_trade(trade, bars, optimistic=False)
    optimistic = replay.evaluate_trade(trade, bars, optimistic=True)
    assert pessimistic["candidate_reason"] == "ORIGINAL_SL_DURING_GRACE"
    assert optimistic["candidate_reason"] == "EOD_AFTER_RECOVERY"
    assert pessimistic["ambiguous_same_bar"] is True
    assert optimistic["ambiguous_same_bar"] is True
    assert pessimistic["candidate_net"] < optimistic["candidate_net"]


def test_pretrigger_recovery_and_invisible_trigger_are_not_accepted():
    trade = _trade()
    bars = [
        _bar(0, 99, 101, 99, 100),
        _bar(1, 100, 101, 99, 100),
        _bar(2, 100, 101, 99, 100),
        _bar(3, 100, 101, 99, 100),
        _eod(100),
    ]
    assert replay.evaluate_trade(trade, bars)["status"] == "trigger_not_visible"


def test_summary_never_promotes_small_positive_sample():
    trade = _trade()
    bars = [
        _bar(1, 97, 98, 96, 97.5),
        _bar(2, 97.5, 100, 97.4, 99),
        _bar(3, 99, 100, 98.5, 99.5),
        _eod(105),
    ]
    row = replay.evaluate_trade(trade, bars)
    summary = replay.summarize([row])
    assert summary["delta_net"] > 0
    assert summary["verdict"] == "EXPLORATORY"
    assert summary["day_clustered"]["n_days"] == 1


def test_forward_summary_uses_only_newly_captured_trade_ids():
    trade = _trade()
    bars = [
        _bar(1, 97, 98, 96, 97.5),
        _bar(2, 97.5, 100, 97.4, 99),
        _bar(3, 99, 100, 98.5, 99.5),
        _eod(105),
    ]
    row = replay.evaluate_trade(trade, bars)
    events = [{"event_id": "new", "trade_id": 999, "path": "early_tight_initial",
               "bid": 97.9, "ask": 98.1, "book_src": "l1_push"}]
    summary = replay.forward_summary(events, {}, [row])
    assert summary["n"] == 0
    assert summary["verdict"] == "TOO_EARLY"
    assert summary["capture"]["events"] == 1
    assert summary["capture"]["valid_book_n"] == 1
