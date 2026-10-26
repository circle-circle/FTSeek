"""On-demand evaluator for the two P0/P1 live-safety shadows."""
import json

import scripts.live_safety_shadow_report as report


def test_report_summarizes_capacity_and_drift(tmp_path):
    capacity = tmp_path / "capacity.jsonl"
    drift = tmp_path / "drift.jsonl"
    capacity.write_text("\n".join(json.dumps(row) for row in [
        {
            "symbol": "US.MU", "intent": "entry", "capacity_status": "sufficient",
            "external_active_order_count": 0, "capacity_error": "",
        },
        {
            "symbol": "US.MU", "intent": "entry", "capacity_status": "insufficient",
            "external_active_order_count": 2, "capacity_error": "",
        },
        {
            "symbol": "US.TSM", "intent": "entry", "capacity_status": "unknown",
            "external_active_order_count": None, "capacity_error": "timeout",
        },
    ]) + "\n", encoding="utf-8")
    drift.write_text("\n".join(json.dumps(row) for row in [
        {
            "stop_order_id": "STOP-1", "observation_status": "observed",
            "abs_drift_bps": 5.0, "capacity_status": None,
        },
        {
            "stop_order_id": "STOP-1", "observation_status": "observed",
            "abs_drift_bps": 20.0,
        },
        {
            "stop_order_id": "STOP-2", "observation_status": "unavailable",
            "abs_drift_bps": None,
        },
    ]) + "\n", encoding="utf-8")

    result = report.build_report(capacity, drift)

    assert result["capacity"]["n"] == 3
    assert result["capacity"]["status_counts"] == {
        "sufficient": 1, "insufficient": 1, "unknown": 1,
    }
    assert result["capacity"]["insufficient_rate_pct"] == 33.33
    assert result["capacity"]["rows_with_external_active_orders"] == 1
    assert result["capacity"]["capacity_error_counts"] == {"timeout": 1}
    assert result["stop_drift"]["observed"] == 2
    assert result["stop_drift"]["unique_stop_ids"] == 2
    assert result["stop_drift"]["abs_drift_bps_median"] == 12.5
    assert result["stop_drift"]["abs_drift_bps_max"] == 20.0
    assert result["stop_drift"]["drift_gt_10bp"] == 1


def test_missing_files_produce_empty_report(tmp_path):
    result = report.build_report(tmp_path / "none.jsonl", tmp_path / "none2.jsonl")
    assert result["capacity"]["n"] == 0
    assert result["capacity"]["insufficient_rate_pct"] is None
    assert result["stop_drift"]["n"] == 0
    assert result["stop_drift"]["abs_drift_bps_median"] is None


def test_malformed_lines_are_counted_not_fatal(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text('{"symbol":"US.MU","capacity_status":"sufficient"}\nnot-json\n', encoding="utf-8")
    result = report.build_report(path, tmp_path / "none.jsonl")
    assert result["capacity"]["n"] == 1
    assert result["capacity"]["malformed"] == 1
