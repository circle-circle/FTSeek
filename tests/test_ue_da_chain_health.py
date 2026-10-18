"""UE/DA chain health check tests (2026-10-18)."""
from scripts.ue_da_chain_health import summarize_instruction, summarize_scan


def test_summarize_scan_contract_fields():
    scan = {
        "type": "scan", "date": "2026-10-18", "scan_window": "premarket",
        "scanned_at": "2026-10-18T09:10:00-04:00", "batch_status": "complete",
        "prompt_version": "v2.15", "brief_template_version": "ue_brief_v7",
        "brief_material_stats": {"event_stream": 12, "borrow_snapshot": 8},
        "surfaced": [
            {"direction": "SHORT", "short_check": {
                "borrow_data_basis": "UNKNOWN", "borrow_feasible": False},
             "evidence_matched_event_meta": {"event_family": "filing_8k"}},
            {"direction": "SHORT", "short_check": {
                "borrow_data_basis": "MATERIAL", "borrow_feasible": True}},
            {"direction": "LONG"},
        ],
        "borrow_on_demand": {"checked": 2, "resolved_true": 1},
    }
    out = summarize_scan(scan)
    assert out["prompt_version"] == "v2.15"
    assert out["brief_template_version"] == "ue_brief_v7"
    assert out["directions"] == {"SHORT": 2, "LONG": 1}
    assert out["short_basis"] == {"UNKNOWN": 1, "MATERIAL": 1}
    assert out["event_meta_coverage"] == 1
    assert out["borrow_on_demand"]["checked"] == 2


def test_summarize_scan_missing():
    assert summarize_scan(None) == {"scan_found": False}


def test_summarize_instruction_reads_ue_fields():
    row = {
        "id": 1, "date": "2026-10-18", "symbol": "US.AMD",
        "direction": "BUY", "decision_id": "DA-1", "confidence": 70,
        "raw_instruction_json": {
            "score_band": "HIGH", "evidence_gaps": ["gap1"],
            "debate_outcome": "挑战者有质疑",
            "origin": {"evidence_matched_event_meta": {
                "event_family": "filing_8k", "form": "8-K"}},
            "ue_short_check": {"borrow_data_basis": "MATERIAL", "borrow_feasible": True},
        },
    }
    out = summarize_instruction(row)
    assert out["score_band"] == "HIGH"
    assert out["evidence_gaps_n"] == 1
    assert out["ue_event_meta"] is True
    assert out["ue_event_family"] == "filing_8k"
    assert out["ue_borrow_basis"] == "MATERIAL"
