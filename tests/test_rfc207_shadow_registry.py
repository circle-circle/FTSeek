from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.playbook import shadow_registry as registry
from scripts.shadow_runtime import env_flag


REPO = Path(__file__).resolve().parents[1]
IDS = ("da_event_context_shadow", "ext_event_risk_live_shadow", "da_instruction_quality_shadow")


def test_rfc207_items_are_permanent_read_only_measures():
    items = {item.id: item for item in registry.REGISTRY}
    for item_id in IDS:
        item = items[item_id]
        assert item.status == registry.ShadowStatus.OBSERVING
        assert item.no_adjudication_reason.startswith("PERMANENT_MEASURE")
        assert item.eval_on_demand is True
        assert item.config_key is None
        assert item.eval_script and (REPO / item.eval_script).exists()
        assert item.graduation_criteria.startswith("PERMANENT_MEASURE")


def test_rfc207_env_flags_fail_closed(tmp_path, monkeypatch):
    for name in ("DA_EVENT_CONTEXT_SHADOW", "EXT_EVENT_RISK_SHADOW", "DA_INSTRUCTION_QUALITY_SHADOW"):
        monkeypatch.delenv(name, raising=False)
        assert env_flag(name, env_file=tmp_path / ".env.us") is False


def test_rfc208_canary_is_single_c1_with_rollback_and_eval():
    item = next(item for item in registry.REGISTRY if item.id == "da_event_prompt_canary")
    assert item.min_n == 30
    assert item.no_adjudication_reason == ""
    assert item.eval_on_demand is True
    assert (REPO / item.eval_script).exists()
    assert "DA_EVENT_CONTEXT_INJECT" in item.notes
    assert "≥10 ET 日" in item.graduation_criteria


def test_preexecute_freshness_is_record_only_with_no_cron_assumption():
    item = next(item for item in registry.REGISTRY if item.id == "da_event_preexecute_shadow")
    assert item.no_adjudication_reason.startswith("PERMANENT_MEASURE")
    assert item.eval_on_demand is True
    assert "不自动" in item.description or "不自动" in item.graduation_criteria
    assert (REPO / item.eval_script).exists()
    assert "待明确批准" in item.notes


def test_rfc207_json_count_modes(tmp_path):
    items = {item.id: item for item in registry.REGISTRY}
    payloads = {
        "da_event_context_shadow": {"fact_count": 96},
        "ext_event_risk_live_shadow": {"counts": {"positions_total": 1}},
        "da_instruction_quality_shadow": {"metrics": {"total": 34}},
    }
    for item_id, payload in payloads.items():
        item = items[item_id]
        relative = item.data_path
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        expected = (payload.get("fact_count")
                    or payload.get("counts", {}).get("positions_total")
                    or payload.get("metrics", {}).get("total"))
        assert registry._count_samples(replace(item), tmp_path) == expected
