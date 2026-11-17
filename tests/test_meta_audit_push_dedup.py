"""meta_audit 相同 findings 不跨日重推，真实状态变化与人工强制查验不受静默。"""
import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("meta_audit_carrier_test", REPO / "ops" / "hermes" / "meta_audit_carrier.py")
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_fingerprint_ignores_only_run_timestamp():
    a = "[meta_audit] 2026-11-13 07:20 | LOOP_DONE=FALSE | P0 OPEN x\nSTATE=/tmp/x"
    b = "[meta_audit] 2026-11-14 07:20 | LOOP_DONE=FALSE | P0 OPEN x\nSTATE=/tmp/x"
    changed = b.replace("LOOP_DONE=FALSE", "LOOP_DONE=TRUE")
    assert MOD._fingerprint(a) == MOD._fingerprint(b)
    assert MOD._fingerprint(a) != MOD._fingerprint(changed)


def test_same_findings_suppressed_and_new_findings_delivered(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(MOD, "PUSH_STATE", state)
    monkeypatch.delenv("META_AUDIT_FORCE", raising=False)
    text = "[meta_audit] 2026-11-14 07:20 | P0 OPEN x"
    assert MOD._same_as_last_delivery(text) is False
    MOD._mark_delivered(text)
    assert MOD._same_as_last_delivery(text) is True
    assert MOD._same_as_last_delivery(text.replace("P0 OPEN x", "P0 OPEN y")) is False


def test_force_and_corrupt_state_fail_open(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(MOD, "PUSH_STATE", state)
    text = "[meta_audit] 2026-11-14 07:20 | P0 OPEN x"
    assert MOD._same_as_last_delivery(text) is False
    MOD._mark_delivered(text)
    monkeypatch.setenv("META_AUDIT_FORCE", "1")
    assert MOD._same_as_last_delivery(text) is False
