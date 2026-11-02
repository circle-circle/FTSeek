"""Tests for the C-class ATR trail replay primitives (DEB §17)."""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.playbook.atr_trail_replay import (  # noqa: E402
    C1_FLOOR_HI,
    C1_FLOOR_LO,
    C1_K,
    adaptive_cfg_with_floor,
    atr_floor_pct,
    atr_fraction_from_metadata,
    build_activation_anchor,
    bucket_from_metadata,
    daily_cluster_stats,
    evaluate_gates,
    fidelity_summary,
    find_activation_bar,
    is_modelable_live_reason,
    paired_delta_summary,
)
from app.utils.timezone import ET  # noqa: E402


def _bar(hour: int, minute: int, high: float, low: float) -> dict:
    return {
        "t": ET.localize(datetime(2026, 9, 18, hour, minute)),
        "o": 100.0,
        "h": high,
        "l": low,
        "c": 100.0,
    }


def test_atr_floor_mapping_boundaries():
    assert atr_floor_pct(0.010) == pytest.approx(C1_FLOOR_LO)
    assert atr_floor_pct(0.0267) == pytest.approx(0.4005, abs=1e-4)
    assert atr_floor_pct(0.030) == pytest.approx(0.45)
    assert atr_floor_pct(0.050) == pytest.approx(0.75)
    assert atr_floor_pct(0.080) == pytest.approx(C1_FLOOR_HI)
    assert atr_floor_pct(0.200) == pytest.approx(C1_FLOOR_HI)
    assert atr_floor_pct(None) is None
    assert atr_floor_pct(0) is None
    assert atr_floor_pct(0.03, k=0) is None
    assert atr_floor_pct(0.03, floor_lo=0.5, floor_hi=0.4) is None


def test_atr_metadata_prefers_repriced_and_bucket_effective():
    meta = {
        "bucket_lineage": {
            "atr_pct_generated": 0.031,
            "atr_pct_repriced": 0.052,
            "bucket_effective": "high",
        }
    }
    assert atr_fraction_from_metadata(meta) == pytest.approx(0.052)
    assert bucket_from_metadata(meta) == "high"
    assert atr_fraction_from_metadata({"bucket_lineage": {"atr_pct_generated": 0.03}}) == pytest.approx(0.03)
    assert atr_fraction_from_metadata({}) is None
    assert bucket_from_metadata({"attribution": {"vol_bucket": "medium"}}) == "medium"


def test_adaptive_cfg_changes_floor_only():
    base = {
        "adaptive_trailing_enabled": True,
        "adaptive_trailing_target_capture_pct": 60.0,
        "adaptive_trailing_min_drawdown_pct": 0.4,
        "adaptive_trailing_max_drawdown_pct": 1.2,
    }
    cfg = adaptive_cfg_with_floor(base, 0.75)
    assert cfg["adaptive_trailing_min_drawdown_pct"] == pytest.approx(0.75)
    assert cfg["adaptive_trailing_max_drawdown_pct"] == pytest.approx(1.2)
    assert cfg["adaptive_trailing_target_capture_pct"] == pytest.approx(60.0)
    assert cfg["adaptive_trailing_enabled"] is True
    assert base["adaptive_trailing_min_drawdown_pct"] == pytest.approx(0.4)


def test_adaptive_cfg_raises_max_if_floor_above_it():
    base = {"adaptive_trailing_max_drawdown_pct": 0.5}
    cfg = adaptive_cfg_with_floor(base, 0.8)
    assert cfg["adaptive_trailing_min_drawdown_pct"] == pytest.approx(0.8)
    assert cfg["adaptive_trailing_max_drawdown_pct"] == pytest.approx(0.8)


def test_modelable_reason_classification():
    for reason in ("SL_INITIAL", "SL_BREAKEVEN", "SL_TRAIL_LOCK", "TAKE_PROFIT",
                   "PRE_CLOSE_EXIT", "MFE_STALL_EXIT", "TIME_EXIT", "REPLAY_EOD"):
        assert is_modelable_live_reason(reason)
    for reason in ("DEAD_SIGNAL_EXIT", "OVERNIGHT_GAP_STOP", "PATIENT_EXIT_EOD",
                   "SL_CERT_LOCK", "", None):
        assert not is_modelable_live_reason(reason)


def test_find_activation_long_crosses_before_exit():
    bars = [
        _bar(9, 31, 100.5, 99.8),
        _bar(9, 32, 101.2, 100.2),
        _bar(9, 33, 101.0, 100.0),
    ]
    found = find_activation_bar(bars, "LONG", 100.0, 1.0, bars[-1]["t"])
    assert found is not None
    assert found["index"] == 1
    assert found["peak_pct"] == pytest.approx(1.2)


def test_find_activation_returns_none_on_exit_bar():
    bars = [
        _bar(9, 31, 100.5, 99.8),
        _bar(9, 32, 101.2, 100.2),
    ]
    assert find_activation_bar(bars, "LONG", 100.0, 1.0, bars[1]["t"]) is None


def test_find_activation_short_and_no_cross():
    bars = [
        _bar(9, 31, 100.5, 99.5),
        _bar(9, 32, 100.2, 98.8),
        _bar(9, 33, 100.1, 99.9),
    ]
    found = find_activation_bar(bars, "SHORT", 100.0, 1.0, bars[-1]["t"])
    assert found is not None
    assert found["index"] == 1
    assert found["peak_pct"] == pytest.approx(1.2)
    assert find_activation_bar(bars, "LONG", 100.0, 5.0, bars[-1]["t"]) is None


def test_build_activation_anchor_is_next_bar():
    bars = [_bar(9, 31, 100.5, 99.8), _bar(9, 32, 101.2, 100.2),
            _bar(9, 33, 101.0, 100.0)]
    anchor = build_activation_anchor(bars, 1, 1.2)
    assert anchor is not None
    assert anchor["t"] == bars[2]["t"]
    assert anchor["peak_pct"] == pytest.approx(1.2)
    assert build_activation_anchor(bars, 2, 1.2) is None


def test_daily_cluster_stats_known_values_and_zero_day():
    stats = daily_cluster_stats({"d1": [1.0, 1.0], "d2": [3.0], "d3": [5.0]})
    assert stats["days"] == 3
    assert stats["mean"] == pytest.approx(3.0)
    assert stats["t"] == pytest.approx(3.0 / (2.0 / math.sqrt(3)), rel=1e-4)
    assert stats["mde80"] == pytest.approx(2.8015852 * 2.0 / math.sqrt(3), rel=1e-4)
    zero = daily_cluster_stats({"d1": [1.0], "d2": [0.0], "d3": [-1.0]})
    assert zero["mean"] == pytest.approx(0.0)
    assert zero["t"] == pytest.approx(0.0)
    assert daily_cluster_stats({"d1": [1.0]})["t"] is None
    assert daily_cluster_stats({})["days"] == 0


def test_paired_delta_summary_tails_and_concentration():
    rows = [
        {"id": 1, "day": "d1", "baseline": {"net": 100, "net_bps": 100, "reason": "SL_TRAIL_LOCK"},
         "arm": {"net": 50, "net_bps": 50, "reason": "SL_BREAKEVEN"}},
        {"id": 2, "day": "d1", "baseline": {"net": -100, "net_bps": -100, "reason": "SL_INITIAL"},
         "arm": {"net": 0, "net_bps": 0, "reason": "SL_BREAKEVEN"}},
        {"id": 3, "day": "d2", "baseline": {"net": -200, "net_bps": -200, "reason": "SL_INITIAL"},
         "arm": {"net": -50, "net_bps": -50, "reason": "SL_BREAKEVEN"}},
        {"id": 4, "day": "d2", "baseline": {"net": 50, "net_bps": 50, "reason": "SL_TRAIL_LOCK"},
         "arm": {"net": -10, "net_bps": -10, "reason": "SL_INITIAL"}},
        {"id": 5, "day": "d3", "baseline": {"net": -300, "net_bps": -300, "reason": "SL_INITIAL"},
         "arm": {"net": -100, "net_bps": -100, "reason": "SL_BREAKEVEN"}},
    ]
    out = paired_delta_summary(rows)
    assert out["n"] == 5
    assert out["days"] == 3
    assert out["delta_net_sum"] == pytest.approx(340.0)
    assert out["avg_delta_bps"] == pytest.approx(68.0)
    assert out["killed_winners_n"] == 1
    assert out["killed_winners_profit_usd"] == pytest.approx(60.0)
    assert out["rescued_losers_n"] == 0
    assert out["left_tail_n"] == 3
    assert out["left_tail_reduction_usd"] == pytest.approx(450.0)
    assert out["right_tail_n"] == 1
    assert out["right_tail_change_usd"] == pytest.approx(-50.0)
    assert out["max_day"] == "d3"
    assert out["max_day_abs_delta_usd"] == pytest.approx(200.0)
    assert out["max_trade_id"] == 5
    assert out["max_trade_abs_delta_usd"] == pytest.approx(200.0)
    assert out["baseline_sl_initial_n"] == 3
    assert out["arm_sl_initial_n"] == 1


def _fidelity_row(i: int, live: str, replay: str, live_pct: float, replay_pct: float) -> dict:
    return {"id": i, "live_reason": live, "replay_reason": replay,
            "live_exit_pct": live_pct, "replay_exit_pct": replay_pct}


def test_fidelity_pass_and_fail_modes():
    rows = [_fidelity_row(i, "SL_BREAKEVEN", "SL_BREAKEVEN", 0.35, 0.40) for i in range(8)]
    rows += [_fidelity_row(8, "SL_INITIAL", "SL_BREAKEVEN", -2.0, -2.2),
             _fidelity_row(9, "SL_TRAIL_LOCK", "SL_BREAKEVEN", 1.0, 0.9)]
    out = fidelity_summary(rows)
    assert out["reason_match_pct"] == pytest.approx(0.8)
    assert out["be_bucket_live"] == 8
    assert out["be_bucket_replay"] == 10
    assert out["be_bucket_range"] == [6, 10]
    assert out["exit_pct_delta_median_pp"] <= 0.35
    assert out["PASS"] is True

    bad_reason = fidelity_summary(rows[:7] + rows[8:])
    assert bad_reason["reason_match_pass"] is False
    assert bad_reason["PASS"] is False

    bad_be = fidelity_summary([_fidelity_row(i, "SL_INITIAL", "SL_BREAKEVEN", -1, -1)
                               for i in range(8)])
    assert bad_be["be_bucket_pass"] is False

    bad_pct = fidelity_summary([_fidelity_row(i, "SL_BREAKEVEN", "SL_BREAKEVEN", 0.0, 1.0)
                                for i in range(8)])
    assert bad_pct["exit_pct_delta_pass"] is False
    assert fidelity_summary([])["PASS"] is False


def _gate_inputs(**overrides):
    base = {
        "n_full": 40,
        "days_full": 12,
        "n_treat": 35,
        "days_treat": 11,
        "treat": {
            "delta_net_sum": 1000.0,
            "avg_delta_bps": 20.0,
            "daily_cluster_bps": {"t": 2.5},
            "mde80_bps": 15.0,
            "killed_winners_profit_usd": 100.0,
            "rescued_losers_usd": 200.0,
            "arm_sl_initial_n": 0,
            "baseline_sl_initial_n": 1,
            "max_day_abs_delta_usd": 400.0,
            "max_trade_abs_delta_usd": 400.0,
        },
        "pess": {"delta_net_sum": 1000.0},
        "opt": {"delta_net_sum": 500.0},
    }
    base.update(overrides)
    return base


def test_gates_pass_candidate_and_fail_modes():
    out = evaluate_gates(**_gate_inputs())
    assert out["verdict"] == "PASS_CANDIDATE"
    assert all(out["checks"].values())

    insufficient = evaluate_gates(**_gate_inputs(n_treat=20))
    assert insufficient["verdict"] == "INSUFFICIENT_N"

    wrong_order = evaluate_gates(**_gate_inputs(opt={"delta_net_sum": -1.0}))
    assert wrong_order["verdict"] == "FAIL_NO_CHANGE"
    assert wrong_order["checks"]["g4_both_orders"] is False

    concentrated = evaluate_gates(**_gate_inputs(treat={
        **(_gate_inputs()["treat"]), "max_day_abs_delta_usd": 600.0}))
    assert concentrated["checks"]["g7_day_concentration"] is False

    weak_mde = evaluate_gates(**_gate_inputs(treat={
        **(_gate_inputs()["treat"]), "avg_delta_bps": 10.0, "mde80_bps": 15.0}))
    assert weak_mde["checks"]["g3_mde"] is False


def test_c2_two_pass_anchor_and_activation_only():
    import scripts.atr_trail_replay_c as driver

    calls = []

    def fake_replay(tr, bars, exit_cfg, var, params, ordering, slippage_bps,
                    anchor=None, adaptive_cfg=None):
        calls.append({
            "exit_cfg": dict(exit_cfg), "var": dict(var), "anchor": anchor,
            "adaptive_cfg": None if adaptive_cfg is None else dict(adaptive_cfg),
        })
        if len(calls) == 1:
            return {"exit_price": 100.5, "reason": "SL_BREAKEVEN",
                    "held_min": 5.0, "mfe_pct": 0.5}
        return {"exit_price": 101.0, "reason": "SL_TRAIL_LOCK",
                "held_min": 6.0, "mfe_pct": 1.2}

    original = driver.eng.replay_trade
    driver.eng.replay_trade = fake_replay
    try:
        entry_et = ET.localize(datetime(2026, 9, 18, 9, 30))
        tr = {"direction": "LONG", "entry": 100.0, "qty": 1.0, "entry_et": entry_et}
        bars = [
            _bar(9, 31, 100.5, 99.8),
            _bar(9, 32, 101.2, 100.2),
            _bar(9, 33, 101.0, 100.0),
            _bar(9, 34, 100.8, 99.9),
            _bar(9, 35, 100.6, 99.7),
        ]
        exit_cfg = {"trailing_enabled": True, "drawdown_from_peak_pct": 0.8}
        base = {
            "adaptive_trailing_enabled": True,
            "adaptive_trailing_target_capture_pct": 60.0,
            "adaptive_trailing_min_drawdown_pct": 0.4,
            "adaptive_trailing_max_drawdown_pct": 1.2,
        }
        result = driver._run_c2(tr, bars, exit_cfg, {}, "pess", 1.0, base)
        assert result["reason"] == "SL_TRAIL_LOCK"
        assert len(calls) == 2
        assert calls[0]["adaptive_cfg"] is None
        assert calls[0]["exit_cfg"]["drawdown_from_peak_pct"] == pytest.approx(99.0)
        assert calls[1]["anchor"]["t"] == bars[2]["t"]
        assert calls[1]["anchor"]["peak_pct"] == pytest.approx(1.2)
        assert calls[1]["adaptive_cfg"] == base
        assert calls[1]["exit_cfg"]["drawdown_from_peak_pct"] == pytest.approx(0.8)
    finally:
        driver.eng.replay_trade = original


def test_c2_no_trail_and_no_activation_paths():
    import scripts.atr_trail_replay_c as driver

    calls = []

    def fake_replay(tr, bars, exit_cfg, var, params, ordering, slippage_bps,
                    anchor=None, adaptive_cfg=None):
        calls.append({"anchor": anchor, "var": dict(var),
                      "adaptive_cfg": None if adaptive_cfg is None else dict(adaptive_cfg)})
        return {"exit_price": 100.0, "reason": "REPLAY_EOD",
                "held_min": 10.0, "mfe_pct": 0.2}

    original = driver.eng.replay_trade
    driver.eng.replay_trade = fake_replay
    try:
        entry_et = ET.localize(datetime(2026, 9, 18, 9, 30))
        tr = {"direction": "LONG", "entry": 100.0, "qty": 1.0, "entry_et": entry_et}
        bars = [_bar(9, 31, 100.2, 99.8), _bar(9, 32, 100.3, 99.9)]
        base = {"adaptive_trailing_enabled": True,
                "adaptive_trailing_target_capture_pct": 60.0,
                "adaptive_trailing_min_drawdown_pct": 0.4,
                "adaptive_trailing_max_drawdown_pct": 1.2}
        no_trail = driver._run_c2(
            tr, bars, {"trailing_enabled": False}, {}, "pess", 1.0, base)
        assert no_trail["reason"] == "REPLAY_EOD"
        assert len(calls) == 1
        assert calls[0]["var"]["name"] == "C2_no_trail"
        assert calls[0]["adaptive_cfg"] == base

        calls.clear()
        no_cross = driver._run_c2(
            tr, bars, {"trailing_enabled": True, "drawdown_from_peak_pct": 0.8},
            {}, "pess", 5.0, base)
        assert no_cross["reason"] == "REPLAY_EOD"
        assert len(calls) == 1
        assert calls[0]["var"]["name"] == "C2_pre"
        assert calls[0]["anchor"] is None
    finally:
        driver.eng.replay_trade = original


def test_net_result_uses_fee_and_notional():
    import scripts.atr_trail_replay_c as driver

    tr = {"direction": "LONG", "entry": 100.0, "qty": 10.0}
    replay = {"exit_price": 101.0, "reason": "TAKE_PROFIT",
              "held_min": 5.0, "mfe_pct": 1.0}
    out = driver._net_result(tr, replay, 5.0)
    assert out["gross"] == pytest.approx(10.0)
    assert out["net"] == pytest.approx(5.0)
    assert out["net_bps"] == pytest.approx(50.0)
    assert out["exit_pct"] == pytest.approx(1.0)
