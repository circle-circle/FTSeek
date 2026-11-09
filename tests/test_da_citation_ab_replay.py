import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, "/root/.hermes/skills/deep-stock-analysis/scripts")

import da_citation_ab_replay as replay
import llm_synth


def _report(tmp_path):
    rows = []
    for direction, count in (("BUY", 8), ("SELL_SHORT", 3), ("SKIP", 3)):
        for index in range(count):
            rows.append({
                "symbol": f"US.{direction}{index}",
                "direction_pre_gate": direction,
                "news_references": 1,
                "linked_claims": 0,
            })
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def _collected():
    return {
        "symbol": "US.TEST",
        "news": [{"news_id": "visible", "title": "visible title", "publish_time": "1793971491"}],
        "news_v2": [],
    }


def test_sample_selection_is_deterministic_and_stratified(tmp_path):
    report = _report(tmp_path)
    first = replay.select_samples(report)
    second = replay.select_samples(report)
    assert first == second
    assert len(first) == 10
    assert sum(row["direction_pre_gate"] == "BUY" for row in first) == 6
    assert sum(row["direction_pre_gate"] == "SELL_SHORT" for row in first) == 2
    assert sum(row["direction_pre_gate"] == "SKIP" for row in first) == 2


def test_citation_section_contains_only_real_visible_ids():
    references = replay.citation_references(_collected())
    section = replay.build_citation_section(references)
    assert references[0]["evidence_id"] in section
    assert references[0]["story_id"] in section
    assert "visible title" in section
    assert "不代表利好/利空" in section


@pytest.mark.parametrize("arm", ["control_1", "treatment_1"])
def test_run_arm_changes_only_citation_layer(monkeypatch, arm):
    captured = {}

    def fake_synthesize(collected, trade_experience="", system_override=None):
        captured["data_text"] = llm_synth._format_data_for_prompt(collected)
        captured["system_override"] = system_override
        captured["trade_experience"] = trade_experience
        return {"direction": "BUY", "_user_prompt": captured["data_text"]}

    monkeypatch.setattr(llm_synth, "synthesize", fake_synthesize)
    result = replay.run_arm(_collected(), arm)
    has_citation = "新闻证据引用表" in captured["data_text"]
    assert has_citation is arm.startswith("treatment")
    if arm.startswith("treatment"):
        assert replay.CITATION_MAIN_CONTRACT in captured["system_override"]
    else:
        assert captured["system_override"] is None
    assert captured["trade_experience"] == ""
    assert result["production_write"] is False


def test_summarize_counts_invalid_ids_and_direction_noise(tmp_path):
    references = replay.citation_references(_collected())
    valid_id = references[0]["evidence_id"]
    for run_name in replay.RUN_NAMES:
        treatment = run_name.startswith("treatment")
        claims = [{"thesis": "news", "evidence": "title", "evidence_ids": [valid_id, "SRC-bad"]}] if treatment else []
        record = {
            "symbol": "US.TEST",
            "run": run_name,
            "arm": "treatment" if treatment else "control",
            "result": {
                "direction": "BUY" if run_name != "treatment_2" else "SKIP",
                "bull_case": claims,
                "bear_case": [],
            },
            "references": references if treatment else [],
            "calls": [],
        }
        (tmp_path / f"US.TEST.{run_name}.json").write_text(json.dumps(record), encoding="utf-8")
    summary = replay.summarize(tmp_path)
    assert summary["runs_completed"] == 4
    assert summary["treatment_valid_results"] == 2
    assert summary["evidence_id_field_coverage"] == 1.0
    assert summary["cited_claims"] == 2
    assert len(summary["invalid_citation_ids"]) == 2
    assert summary["valid_citation_id_rate"] == 0.5
    assert summary["control_stable_symbols"] == 1
    assert summary["treatment_stable_symbols"] == 0
    assert summary["manual_audit"]["status"] == "PENDING"


def test_finalize_manual_audit_uses_conservative_denominator(tmp_path):
    rows = [{"audit_id": "one"}, {"audit_id": "two"}, {"audit_id": "three"}]
    audit_hash = replay._sha(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    (tmp_path / "citation_audit_sample.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    (tmp_path / "citation_audit_adjudicated.json").write_text(json.dumps({
        "audit_sha256": audit_hash,
        "rows": [
            {"audit_id": "one", "adjudication": "SUPPORTED", "reason": "direct"},
            {"audit_id": "two", "adjudication": "UNSUPPORTED", "reason": "contradicted"},
            {"audit_id": "three", "adjudication": "INSUFFICIENT", "reason": "partial"},
        ],
    }), encoding="utf-8")
    (tmp_path / "summary.pre_manual.json").write_text(json.dumps({
        "runs_completed": 40,
        "treatment_valid_results": 20,
        "evidence_id_field_coverage": 1.0,
        "invalid_citation_ids": [],
        "valid_citation_id_rate": 1.0,
        "symbols_with_citations": 10,
    }), encoding="utf-8")
    summary = replay.finalize_manual_audit(tmp_path)
    assert summary["manual_audit"]["support_rate_all"] == 0.3333
    assert summary["manual_audit"]["support_rate_evaluable"] == 0.5
    assert summary["feasibility_gates"]["manual_support"] is False
    assert summary["feasibility_pass"] is False
    assert summary["promotion"] == "NOT_APPROVED_N10_STRUCTURE_ONLY"


def test_finalize_manual_audit_rejects_duplicate_ids(tmp_path):
    rows = [{"audit_id": "one"}, {"audit_id": "two"}]
    audit_hash = replay._sha(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    (tmp_path / "citation_audit_sample.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    (tmp_path / "citation_audit_adjudicated.json").write_text(json.dumps({
        "audit_sha256": audit_hash,
        "rows": [
            {"audit_id": "one", "adjudication": "SUPPORTED", "reason": "first"},
            {"audit_id": "one", "adjudication": "UNSUPPORTED", "reason": "duplicate"},
            {"audit_id": "two", "adjudication": "SUPPORTED", "reason": "direct"},
        ],
    }), encoding="utf-8")
    (tmp_path / "summary.pre_manual.json").write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        replay.finalize_manual_audit(tmp_path)
