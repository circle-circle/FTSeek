"""edge 影子执行侧写入与 decision_id 去重契约。"""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.decision import edge_shadow_gate as esg
from app.playbook.pipeline_parts import risk_gates


def test_edge_shadow_dedupes_same_decision_but_not_distinct_decisions(tmp_path, monkeypatch):
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({
        "threshold_recommendations": {"expectancy_by_bucket": {"0.3-0.5": 0.001}}
    }))
    shadow_dir = tmp_path / "shadow"
    monkeypatch.setattr(esg, "_LATEST", latest)
    monkeypatch.setattr(esg, "_SHADOW_DIR", shadow_dir)
    monkeypatch.setattr(esg, "_SHADOW_LOG", shadow_dir / "edge_gate_shadow.jsonl")
    gate = esg.EdgeShadowGate()
    monkeypatch.setattr(
        "app.utils.timezone.now_et",
        lambda: datetime(2026, 11, 13, 10, 0, tzinfo=ZoneInfo("America/New_York")))

    for _ in range(2):
        result = gate.evaluate(
            "US.AMD", "BUY", 40, live_pass=True,
            decision_id="DA-20261113-AMD")
    gate.evaluate("US.MU", "BUY", 40, live_pass=True,
                  decision_id="DA-20261113-MU")

    rows = [json.loads(line) for line in esg._SHADOW_LOG.read_text().splitlines()]
    assert len(rows) == 2
    assert {row["decision_id"] for row in rows} == {"DA-20261113-AMD", "DA-20261113-MU"}
    assert ("2026-11-13", "DA-20261113-AMD") in gate._logged_decision_days
    assert result["edge_verdict"] == "PASS"


def test_execution_audit_restores_edge_logging(tmp_path, monkeypatch):
    gate = SimpleNamespace(evaluate=MagicMock(return_value={
        "bucket": "0.3-0.5", "expectancy": 0.001,
        "edge_verdict": "PASS", "reason": "pos_expectancy",
    }))
    monkeypatch.setattr(
        "app.decision.edge_shadow_gate.get_edge_shadow_gate", lambda: gate)
    instruction = {
        "id": 5602, "symbol": "US.AMZN", "direction": "BUY", "confidence": 35,
        "decision_id": "DA-20261113-AMZN",
    }

    risk_gates._emit_exec_gate_audit(
        instruction, action="executed", entry=268.65, size=22,
        notional=5910.3, module_file=str(tmp_path / "a" / "b" / "module.py"))

    assert gate.evaluate.call_count == 1
    assert gate.evaluate.call_args.kwargs["log"] is True
    assert gate.evaluate.call_args.kwargs["decision_id"] == "DA-20261113-AMZN"
    audit = tmp_path / "data" / "shadow" / "exec_gate_audit.jsonl"
    assert json.loads(audit.read_text())["edge_verdict"] == "PASS"
