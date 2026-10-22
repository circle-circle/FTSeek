"""UE/DA chain health check tests (2026-10-18)."""
from scripts.ue_da_chain_health import summarize_instruction, summarize_scan


def test_summarize_scan_contract_fields():
    scan = {
        "type": "scan", "date": "2026-10-18", "scan_window": "premarket",
        "scanned_at": "2026-10-18T09:10:00-04:00", "batch_status": "complete",
        "prompt_version": "v2.15", "brief_template_version": "ue_brief_v8",
        "brief_material_stats": {"event_stream": 12, "borrow_snapshot": 8},
        "surfaced": [
            {"direction": "SHORT", "short_check": {
                "borrow_data_basis": "UNKNOWN", "borrow_feasible": False},
             "evidence_matched_event_meta": {"event_family": "filing_8k"},
             "evidence_matched_event_reaction": {
                 "age_h": 23.2, "post_event_move_pct": 3.77,
                 "reaction_basis": "same_day_close"}},
            {"direction": "SHORT", "short_check": {
                "borrow_data_basis": "MATERIAL", "borrow_feasible": True}},
            {"direction": "LONG"},
        ],
        "borrow_on_demand": {"checked": 2, "resolved_true": 1},
    }
    out = summarize_scan(scan)
    assert out["prompt_version"] == "v2.15"
    assert out["brief_template_version"] == "ue_brief_v8"
    assert out["directions"] == {"SHORT": 2, "LONG": 1}
    assert out["short_basis"] == {"UNKNOWN": 1, "MATERIAL": 1}
    assert out["event_meta_coverage"] == 1
    assert out["event_reaction_coverage"] == 1
    assert out["reaction_basis"] == {"same_day_close": 1}
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
            "ue_event_reaction": {
                "age_h": 22.0, "post_event_move_pct": -1.92,
                "reaction_basis": "same_day_close"},
        },
    }
    out = summarize_instruction(row)
    assert out["score_band"] == "HIGH"
    assert out["evidence_gaps_n"] == 1
    assert out["ue_event_meta"] is True
    assert out["ue_event_family"] == "filing_8k"
    assert out["ue_borrow_basis"] == "MATERIAL"
    assert out["ue_event_reaction"] is True
    assert out["post_event_move_pct"] == -1.92
    assert out["reaction_basis"] == "same_day_close"


def test_summarize_instruction_reads_checkpoint_prompt(tmp_path):
    """prompt-only 字段必须从 checkpoint user_prompt 验证, 不能只看 raw 回显。"""
    day = tmp_path / "2026-10-20"
    day.mkdir()
    (day / "US.NBIS.raw.json").write_text(
        '{"user_prompt": "事件扫描器(UE)今晨浮现该标的为SHORT候选\\n'
        'UE SHORT三问: 可借券=True basis=MATERIAL 催化剂时效=24h\\n'
        'UE 事件属性: family=filing_8k form=8-K\\n'
        'UE 事件反应: +3.77% (same_day_close, 23.2h前)"}',
        encoding="utf-8",
    )
    row = {
        "id": 2, "date": "2026-10-20", "symbol": "US.NBIS",
        "direction": "SELL_SHORT", "decision_id": "DA-2",
        "raw_instruction_json": {"score_band": "MEDIUM"},
    }
    out = summarize_instruction(row, checkpoint_dir=tmp_path)
    assert out["prompt_found"] is True
    assert out["prompt_ue_context"] is True
    assert out["prompt_ue_short_check"] is True
    assert out["prompt_ue_event_meta"] is True
    assert out["prompt_ue_event_reaction"] is True
    assert out["prompt_event_reaction_pct"] == 3.77
    assert out["prompt_borrow_basis"] == "MATERIAL"
    assert out["prompt_event_family"] == "filing_8k"

    missing = summarize_instruction(
        {**row, "symbol": "US.LITE"}, checkpoint_dir=tmp_path)
    assert missing["prompt_found"] is False
    assert missing["prompt_ue_context"] is False
