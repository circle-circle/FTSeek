"""EXT 入场风险影子必须注册为 record-only 测量项。"""
from __future__ import annotations

from app.playbook.shadow_registry import REGISTRY, ShadowStatus


def test_ext_entry_risk_shadow_registered():
    item = next(item for item in REGISTRY if item.id == "ext_entry_risk_shadow")
    assert item.status == ShadowStatus.OBSERVING
    assert item.data_path == "data/shadow/ext_entry_risk_shadow.jsonl"
    assert item.config_key == "EXT_ENTRY_RISK_SHADOW"
    assert item.eval_on_demand is True
    assert item.no_adjudication_reason.startswith("PERMANENT_MEASURE")
    assert "不转生产门" in item.graduation_criteria
    assert item.eval_script == "scripts/session_attribution.py"
    assert item.intent == "ext_entry_risk_measurement"
