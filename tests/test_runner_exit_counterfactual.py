import sys

sys.path.insert(0, ".")

from scripts import runner_exit_counterfactual as runner


def test_long_runner_stop_and_eod():
    bars = [
        ["2026-09-29 10:01:00", 100.0, 101.0, 99.5, 100.8],
        ["2026-09-29 10:02:00", 100.8, 102.0, 100.7, 101.8],
        ["2026-09-29 10:03:00", 101.8, 102.2, 101.2, 101.5],
    ]
    stopped = runner.settle_runner_leg("LONG", 100.0, 1.0, bars, 0.25)
    assert stopped["exit_reason"] == "runner_stop"
    assert stopped["exit_price"] == 99.75

    safe_bars = [
        ["2026-09-29 10:01:00", 100.0, 100.2, 99.9, 100.1],
        ["2026-09-29 10:02:00", 100.2, 100.6, 100.1, 100.5],
    ]
    eod = runner.settle_runner_leg("LONG", 100.0, 1.0, safe_bars, 0.25)
    assert eod["exit_reason"] == "eod_close"
    assert eod["exit_price"] == 100.5


def test_short_runner_stop_direction():
    bars = [
        ["2026-09-29 10:01:00", 100.0, 100.4, 99.5, 99.8],
        ["2026-09-29 10:02:00", 99.8, 100.3, 99.7, 99.9],
    ]
    result = runner.settle_runner_leg("SHORT", 100.0, 1.0, bars, 0.25)
    assert result["exit_reason"] == "runner_stop"
    assert result["exit_price"] == 100.25


def test_summary_gate_is_paired_and_deterministic():
    records = [
        {"status": "ok", "runner_fraction": 0.25, "atr_multiplier": 0.25, "delta_usd": 10.0},
        {"status": "ok", "runner_fraction": 0.25, "atr_multiplier": 0.25, "delta_usd": 20.0},
        {"status": "ok", "runner_fraction": 0.25, "atr_multiplier": 0.25, "delta_usd": 15.0},
    ]
    summary = runner.summarize(records, iterations=100)
    variant = summary["fraction=0.25/atr_mult=0.25"]
    assert variant["n"] == 3
    assert variant["sum_delta_usd"] == 45.0
    assert variant["median_delta_usd"] == 15.0
    assert variant["proposal_gate"] is False
