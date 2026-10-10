from __future__ import annotations

from pathlib import Path
from inspect import getsource

from scripts import deb20261009_strategy_opt_probe as P


def test_probe_is_read_only_and_has_candidate_c_segment():
    source = Path(P.__file__).read_text(encoding="utf-8")
    assert 'sqlite3.connect(f"file:{DB}?mode=ro", uri=True)' in source
    assert "def seg_i_candidate_c_entry_anchor" in source
    assert "seg_i_candidate_c_entry_anchor(db)" in source
    assert "def seg_j_candidate_b_d2_delay" in source
    assert "seg_j_candidate_b_d2_delay()" in source


def test_candidate_c_uses_exact_instruction_join_and_net_ssot():
    source = Path(P.__file__).read_text(encoding="utf-8")
    assert "ON i.id = (SELECT MAX(p.id) FROM playbook_instructions p" in source
    assert "COALESCE(pnl_net, realized_pnl)" in source
    assert "insufficient_for_gate" in source


def test_candidate_b_exposes_carrier_cadence_and_ttl_conflict():
    source = Path(P.__file__).read_text(encoding="utf-8")
    assert "host_auto_execute_cadence" in source
    assert "entry_archetype_ttl_seconds" in source
    assert "five_min_confirm_exceeds_ttl" in source
    assert "sample_threshold_note" in source


def test_candidate_a_eod_metrics_ignore_pre_entry_and_lifecycle_mfe():
    row = {
        "direction": "LONG",
        "entry_time": "2026-10-01T10:00:00-04:00",
        "marked_at_et": "2026-10-01T16:00:00-04:00",
        "entry_price": 100.0,
        "quantity": 10,
        "fees": 2.0,
    }
    bars = [
        ["2026-10-01 09:30:00", 99.0, 101.0, 99.0, 99.5],
        ["2026-10-01 09:59:00", 99.0, 100.8, 99.0, 99.5],
        ["2026-10-01 10:01:00", 100.0, 100.4, 99.8, 100.1],
        ["2026-10-01 15:59:00", 99.2, 100.3, 99.0, 99.0],
    ]
    metrics = P._candidate_a_eod_metrics(row, bars)
    assert metrics is not None
    assert metrics["eod_mfe_pct"] == 0.4
    assert metrics["eod_close"] == 99.0
    assert metrics["eod_close_net"] == -12.0
    assert metrics["underwater_close"] is True


def test_candidate_a_eod_cutoff_respects_mark_time_before_close():
    row = {
        "direction": "SHORT",
        "entry_time": "2026-10-01T10:00:00-04:00",
        "marked_at_et": "2026-10-01T15:52:00-04:00",
        "entry_price": 100.0,
        "quantity": 10,
        "fees": 2.0,
    }
    bars = [
        ["2026-10-01 10:01:00", 100.0, 100.2, 99.8, 100.0],
        ["2026-10-01 15:50:00", 100.5, 100.6, 100.3, 100.4],
        ["2026-10-01 16:00:00", 100.9, 101.0, 100.8, 100.8],
    ]
    metrics = P._candidate_a_eod_metrics(row, bars)
    assert metrics is not None
    assert metrics["bar_last_time"] == "2026-10-01 15:50:00"
    assert metrics["eod_mfe_pct"] == 0.2
    assert metrics["eod_close"] == 100.4


def test_candidate_a_segment_uses_latest_instruction_and_local_bars_only():
    source = Path(P.__file__).read_text(encoding="utf-8")
    segment_source = getsource(P.seg_k_candidate_a_eod_counterfactual)
    assert "def seg_k_candidate_a_eod_counterfactual" in source
    assert "seg_k_candidate_a_eod_counterfactual(db)" in source
    assert "SELECT MAX(p2.id) FROM playbook_instructions p2" in segment_source
    assert "_local_replay_bars" in segment_source
    assert "null_actual_net_n" in segment_source
    assert "missing_required_fields" in segment_source
    assert "OpenQuoteContext" not in source
    assert "t.mfe_pct" not in segment_source
