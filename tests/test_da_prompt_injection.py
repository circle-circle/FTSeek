"""RFC-207 bridge: switch-gated EventContext injection into DA prompts.

Covers:
  1. default off => no injection, prompt / hash byte-identical;
  2. valid brief => production-compatible section + main/challenger contracts;
  3. stale / invalid / missing brief => bypass with explicit reason, DA not blocked;
  4. prompt hash changes when injected and pipeline checkpoint policy invalidates
     the old baseline cache.

All LLM calls are stubbed; no paid calls and no production writes.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
for _path in (str(PROJECT_ROOT), str(SKILL_SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.event_stream import da_prompt_injection as injection  # noqa: E402
from app.event_stream.instructions import (  # noqa: E402
    CHALLENGER_EVENT_CONTRACT,
    MAIN_EVENT_CONTRACT,
)

try:
    import llm_synth as L  # noqa: E402
except Exception as _exc:  # pragma: no cover - external skill not installed
    L = None
    _LLM_IMPORT_ERR = str(_exc)

try:
    import pipeline as P  # noqa: E402
except Exception as _exc:  # pragma: no cover
    P = None
    _PIPELINE_IMPORT_ERR = str(_exc)

requires_llm = pytest.mark.skipif(L is None, reason="deep-stock-analysis skill unavailable")
requires_pipeline = pytest.mark.skipif(P is None, reason="deep-stock-analysis pipeline unavailable")

NOW = datetime(2026, 11, 13, 14, 0, tzinfo=timezone.utc)
VALID_UNTIL = "2026-11-13T16:00:00-05:00"
BRIEF_ID = "brieftest123"
EVENT_ID = "EV-TEST-1"
ENV_ON = {"DA_EVENT_CONTEXT_INJECT": "1"}
ENV_OFF = {"DA_EVENT_CONTEXT_INJECT": "0"}

COLLECTED = {
    "symbol": "US.NVDA",
    "company_profile": {"name": "NVIDIA", "industry": "Semiconductors"},
    "plates": None,
}


def _brief(*, as_of: str, validation_errors=None):
    return {
        "brief_id": BRIEF_ID,
        "as_of": as_of,
        "validation_errors": list(validation_errors or []),
        "facts": [{
            "event_id": EVENT_ID,
            "event_time": "Fri, 13 Nov 2026 03:23:00 GMT",
            "first_seen_at": as_of,
            "headline": "NVIDIA headline",
            "member_event_ids": [],
            "member_facts": [],
        }],
        "transmissions": [{
            "event_ids": [EVENT_ID],
            "theme": "Semiconductor risk",
            "sign": "POSITIVE",
            "price_in": "UNPRICED",
            "invalidation": "news reversed",
            "causal_chain": "chip demand -> NVDA",
            "counter_evidence": "none",
            "beneficiary_symbols": ["NVDA"],
            "loser_symbols": [],
            "confidence": 0.8,
            "horizon": "INTRADAY",
        }],
    }


def _latest_doc(*, as_of: str, **overrides):
    doc = {
        "record_only": True,
        "status": "OK",
        "as_of": as_of,
        "validation_errors": [],
        "brief": _brief(as_of=as_of),
    }
    doc.update(overrides)
    return doc


def _write_latest(tmp_path: Path, doc) -> Path:
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _write_env_file(tmp_path: Path, value: str) -> Path:
    path = tmp_path / ".env.us"
    path.write_text(f"DA_EVENT_CONTEXT_INJECT={value}\n", encoding="utf-8")
    return path


def test_module_docstring_cites_rfc_208():
    assert "RFC-208" in injection.__doc__
    assert "RFC-207" not in injection.__doc__


# ── 0. flag resolution: process env > .env.us > default false ────────────────


def test_env_file_flag_used_when_process_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("DA_EVENT_CONTEXT_INJECT", raising=False)
    env_file = _write_env_file(tmp_path, "1")
    assert injection.injection_enabled(environ=None, env_file=env_file) is True
    assert injection.injection_enabled(environ={}, env_file=env_file) is False
    env_file.write_text("DA_EVENT_CONTEXT_INJECT=unknown\n", encoding="utf-8")
    assert injection.injection_enabled(environ=None, env_file=env_file) is False
    env_file.write_text("DA_EVENT_CONTEXT_INJECT=0\n", encoding="utf-8")
    assert injection.injection_enabled(environ=None, env_file=env_file) is False


def test_env_file_flag_drives_valid_injection(tmp_path, monkeypatch):
    monkeypatch.delenv("DA_EVENT_CONTEXT_INJECT", raising=False)
    env_file = _write_env_file(tmp_path, "1")
    path = _write_latest(tmp_path, _latest_doc(as_of="2026-11-13T13:00:00+00:00"))
    out = injection.build_event_context_injection(
        COLLECTED, now=NOW, latest_path=path, environ=None, env_file=env_file,
    )
    assert out.injected is True
    assert out.reason == "injected"


@requires_llm
def test_synthesize_reads_flag_from_env_file(monkeypatch, tmp_path):
    """DA cron has no systemd env; .env.us must gate synthesize itself."""
    monkeypatch.delenv("DA_EVENT_CONTEXT_INJECT", raising=False)
    env_file = _write_env_file(tmp_path, "1")
    monkeypatch.setattr(L.config, "FTSEEK_ENV", str(env_file))
    monkeypatch.setattr(injection, "DEFAULT_ENV_FILE", env_file)
    path = _write_latest(tmp_path, _latest_doc(
        as_of=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
    monkeypatch.setattr(injection, "DEFAULT_LATEST_PATH", path)

    captured = {}
    monkeypatch.setattr(L, "_format_data_for_prompt", lambda c: "BASE_DATA\n")
    monkeypatch.setattr(L, "_apply_prev_close_fix", lambda c: c)
    monkeypatch.setattr(L, "_load_negative_claims_warning_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_condition_blacklist_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_symbol_card_for_prompt", lambda *a: ("", False))
    monkeypatch.setattr(L, "_load_trading_card_for_prompt", lambda *a: "")
    monkeypatch.setattr(L, "_validate_output_contract",
                        lambda *a, **k: {"violations": [], "repairable": []})
    monkeypatch.setattr(L, "_verify_sourced_numbers", lambda *a, **k: {})
    monkeypatch.setattr(L, "_run_challenger", lambda *a, **k: None)

    def fake_call(system, user, **kwargs):
        captured["user"] = user
        return {"direction": "SKIP", "rating": "观望", "score": 5.0,
                "skip_reason": "test", "execution_plan": {}}

    monkeypatch.setattr(L, "_call_llm", fake_call)
    result = L.synthesize(dict(COLLECTED))
    assert result["_event_context"]["injected"] is True
    assert "## 大盘环境" in captured["user"]


# ── 1. default off ────────────────────────────────────────────────────────────


def test_flag_off_is_disabled_and_has_no_policy_hash(tmp_path):
    path = _write_latest(tmp_path, _latest_doc(as_of="2026-11-13T13:00:00+00:00"))
    out = injection.build_event_context_injection(
        COLLECTED, now=NOW, latest_path=path, environ=ENV_OFF,
    )
    assert out.injected is False
    assert out.reason == "flag_disabled"
    assert out.section == ""
    assert out.event_ids == []
    assert injection.event_injection_policy_hash(
        now=NOW, latest_path=path, environ=ENV_OFF) is None


@requires_llm
def test_flag_off_synthesize_prompt_and_hash_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("DA_EVENT_CONTEXT_INJECT", raising=False)
    # Hermetic: do not inherit the real /opt/ftseek/FTSeek-US/.env.us.
    monkeypatch.setattr(L.config, "FTSEEK_ENV", str(_write_env_file(tmp_path, "0")))
    captured = {}
    monkeypatch.setattr(L, "_format_data_for_prompt", lambda c: "BASE_DATA\n")
    monkeypatch.setattr(L, "_apply_prev_close_fix", lambda c: c)
    monkeypatch.setattr(L, "_load_negative_claims_warning_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_condition_blacklist_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_symbol_card_for_prompt", lambda *a: ("", False))
    monkeypatch.setattr(L, "_load_trading_card_for_prompt", lambda *a: "")
    monkeypatch.setattr(L, "_validate_output_contract",
                        lambda *a, **k: {"violations": [], "repairable": []})
    monkeypatch.setattr(L, "_verify_sourced_numbers", lambda *a, **k: {})
    monkeypatch.setattr(L, "_run_challenger", lambda *a, **k: None)

    def fake_call(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return {"direction": "SKIP", "rating": "观望", "score": 5.0,
                "skip_reason": "test", "execution_plan": {}}

    monkeypatch.setattr(L, "_call_llm", fake_call)
    result = L.synthesize(dict(COLLECTED))

    assert result["_prompt_hash"] == L.PROMPT_HASH
    assert "_event_context" not in result
    assert captured["system"] == L.SYSTEM_PROMPT
    assert "共享日内事件简报" not in captured["user"]
    assert captured["user"] == L.build_user_prompt("BASE_DATA\n")[0]


# ── 2. valid injection ────────────────────────────────────────────────────────


def test_valid_brief_builds_production_section(tmp_path):
    path = _write_latest(tmp_path, _latest_doc(as_of="2026-11-13T13:00:00+00:00"))
    out = injection.build_event_context_injection(
        COLLECTED, now=NOW, latest_path=path, environ=ENV_ON,
    )
    assert out.injected is True
    assert out.reason == "injected"
    assert out.brief_id == BRIEF_ID
    assert out.status == "OK"
    assert out.valid_until == VALID_UNTIL
    assert EVENT_ID in out.event_ids
    assert out.section_hash
    assert "## 大盘环境" in out.section
    assert VALID_UNTIL in out.section
    assert "本次为生产日内分析。" in out.section
    assert "本次仅为离线日内研究。" not in out.section
    assert out.injection_hash == injection.event_injection_policy_hash(
        now=NOW, latest_path=path, environ=ENV_ON)

    fields = out.result_fields()
    assert fields["injection_hash"] == out.injection_hash
    assert fields["event_ids"] == out.event_ids
    assert fields["consumers"] == ["raw_checkpoint", "quality_evaluator"]
    assert fields["gate_visible"] is False
    assert fields["advisory_only"] is True
    assert fields["record_only"] is False


@requires_llm
def test_valid_injection_wires_prompt_contracts_and_hash(monkeypatch, tmp_path):
    path = _write_latest(tmp_path, _latest_doc(
        as_of=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
    monkeypatch.setattr(injection, "DEFAULT_LATEST_PATH", path)
    monkeypatch.setenv("DA_EVENT_CONTEXT_INJECT", "1")
    captured = {}
    challenger_calls = []

    monkeypatch.setattr(L, "_format_data_for_prompt", lambda c: "BASE_DATA\n")
    monkeypatch.setattr(L, "_apply_prev_close_fix", lambda c: c)
    monkeypatch.setattr(L, "_load_negative_claims_warning_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_condition_blacklist_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_symbol_card_for_prompt", lambda *a: ("", False))
    monkeypatch.setattr(L, "_load_trading_card_for_prompt", lambda *a: "")
    monkeypatch.setattr(L, "_validate_output_contract",
                        lambda *a, **k: {"violations": [], "repairable": []})
    monkeypatch.setattr(L, "_verify_sourced_numbers", lambda *a, **k: {})

    def fake_challenger(analysis, data_text, event_contract=""):
        challenger_calls.append(event_contract)
        return None

    def fake_call(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return {"direction": "SKIP", "rating": "观望", "score": 5.0,
                "skip_reason": "test", "execution_plan": {}}

    monkeypatch.setattr(L, "_run_challenger", fake_challenger)
    monkeypatch.setattr(L, "_call_llm", fake_call)
    result = L.synthesize(dict(COLLECTED))

    assert captured["system"].endswith(MAIN_EVENT_CONTRACT)
    assert challenger_calls == [CHALLENGER_EVENT_CONTRACT]
    assert "## 大盘环境" in captured["user"]
    ctx = result["_event_context"]
    assert ctx["valid_until"] in captured["user"]
    assert "## 大盘环境" in result["_user_prompt"]
    expected_hash = L._prompt_fingerprint(
        L.SYSTEM_PROMPT + MAIN_EVENT_CONTRACT,
        L.effective_user_template(),
        L._SAMPLING_TAG,
        ctx["brief_id"],
        ctx["injection_hash"],
    )
    assert result["_prompt_hash"] == expected_hash
    assert result["_prompt_hash"] != L.PROMPT_HASH
    assert ctx["injected"] is True
    assert ctx["brief_id"] == BRIEF_ID
    assert ctx["status"] == "OK"
    assert EVENT_ID in ctx["event_ids"]
    assert len(ctx["injection_hash"]) == 12
    assert ctx["gate_visible"] is False
    assert ctx["advisory_only"] is True
    assert ctx["record_only"] is False


# ── 3. stale / invalid / missing bypass ──────────────────────────────────────


@pytest.mark.parametrize("case", [
    "stale",
    "record_only_false",
    "bad_status",
    "validation_errors",
    "brief_validation_errors",
    "missing",
    "unreadable",
    "naive_as_of",
])
def test_invalid_brief_bypasses_with_explicit_reason(tmp_path, case):
    as_of = "2026-11-13T13:00:00+00:00"
    path = tmp_path / "latest.json"
    if case == "missing":
        pass
    elif case == "unreadable":
        path.write_text("{not json", encoding="utf-8")
    elif case == "stale":
        doc = _latest_doc(as_of="2026-11-11T00:00:00+00:00")
        _write_latest(tmp_path, doc)
    elif case == "record_only_false":
        _write_latest(tmp_path, _latest_doc(as_of=as_of, record_only=False))
    elif case == "bad_status":
        _write_latest(tmp_path, _latest_doc(as_of=as_of, status="ERROR"))
    elif case == "validation_errors":
        _write_latest(tmp_path, _latest_doc(as_of=as_of, validation_errors=["bad"]))
    elif case == "brief_validation_errors":
        doc = _latest_doc(as_of=as_of)
        doc["brief"]["validation_errors"] = ["bad"]
        _write_latest(tmp_path, doc)
    elif case == "naive_as_of":
        doc = _latest_doc(as_of="2026-11-13T13:00:00")
        doc["as_of"] = "2026-11-13T13:00:00"
        doc["brief"]["as_of"] = "2026-11-13T13:00:00"
        _write_latest(tmp_path, doc)
    else:  # pragma: no cover
        raise AssertionError(case)

    out = injection.build_event_context_injection(
        COLLECTED, now=NOW, latest_path=path, environ=ENV_ON,
    )
    assert out.injected is False
    assert out.section == ""
    assert out.event_ids == []
    assert out.reason and out.reason != "injected"
    # failure identity is still deterministic and checkpoint-relevant
    assert out.injection_hash


def test_previous_day_brief_is_rejected_even_within_26_hours(tmp_path):
    previous_day = (NOW - timedelta(hours=24)).isoformat()
    path = _write_latest(tmp_path, _latest_doc(as_of=previous_day))
    out = injection.build_event_context_injection(
        COLLECTED, now=NOW, latest_path=path, environ=ENV_ON,
    )
    assert out.injected is False
    assert out.reason == "brief_date_mismatch"
    assert out.section == ""


def test_writer_status_flip_does_not_change_injection_identity(tmp_path):
    as_of = "2026-11-13T13:00:00+00:00"
    ok_path = _write_latest(tmp_path, _latest_doc(as_of=as_of, status="OK"))
    current_path = _write_latest(
        tmp_path, _latest_doc(as_of=as_of, status="ALREADY_CURRENT"))
    ok = injection.load_event_context(now=NOW, latest_path=ok_path, environ=ENV_ON)
    current = injection.load_event_context(
        now=NOW, latest_path=current_path, environ=ENV_ON)
    assert ok.injected is True and current.injected is True
    assert ok.injection_hash == current.injection_hash


@requires_llm
def test_stale_brief_does_not_block_synthesize(monkeypatch, tmp_path):
    stale = _latest_doc(as_of="2026-11-10T00:00:00+00:00")
    path = _write_latest(tmp_path, stale)
    monkeypatch.setattr(injection, "DEFAULT_LATEST_PATH", path)
    monkeypatch.setenv("DA_EVENT_CONTEXT_INJECT", "1")
    captured = {}

    monkeypatch.setattr(L, "_format_data_for_prompt", lambda c: "BASE_DATA\n")
    monkeypatch.setattr(L, "_apply_prev_close_fix", lambda c: c)
    monkeypatch.setattr(L, "_load_negative_claims_warning_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_condition_blacklist_for_prompt", lambda: "")
    monkeypatch.setattr(L, "_load_symbol_card_for_prompt", lambda *a: ("", False))
    monkeypatch.setattr(L, "_load_trading_card_for_prompt", lambda *a: "")
    monkeypatch.setattr(L, "_validate_output_contract",
                        lambda *a, **k: {"violations": [], "repairable": []})
    monkeypatch.setattr(L, "_verify_sourced_numbers", lambda *a, **k: {})
    monkeypatch.setattr(L, "_run_challenger", lambda *a, **k: None)

    def fake_call(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return {"direction": "SKIP", "rating": "观望", "score": 5.0,
                "skip_reason": "test", "execution_plan": {}}

    monkeypatch.setattr(L, "_call_llm", fake_call)
    result = L.synthesize(dict(COLLECTED))

    assert "共享日内事件简报" not in captured["user"]
    assert captured["system"] == L.SYSTEM_PROMPT
    assert result["_prompt_hash"] == L.PROMPT_HASH
    assert result["_event_context"]["injected"] is False
    assert result["_event_context"]["reason"] == "brief_date_mismatch"


# ── 4. prompt hash + checkpoint policy / cache invalidation ──────────────────


@requires_pipeline
def test_checkpoint_policy_invalidates_old_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(injection, "DEFAULT_LATEST_PATH", tmp_path / "absent.json")
    monkeypatch.setenv("DA_EVENT_CONTEXT_INJECT", "0")
    off_policy = P._ckpt_policy_version()

    path = _write_latest(tmp_path, _latest_doc(
        as_of=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
    monkeypatch.setattr(injection, "DEFAULT_LATEST_PATH", path)
    monkeypatch.setenv("DA_EVENT_CONTEXT_INJECT", "1")
    on_policy = P._ckpt_policy_version()
    assert on_policy != off_policy

    # A checkpoint written under the off baseline must not be reused when the
    # injection is enabled.
    out_dir = str(tmp_path / "out")
    P._ckpt_save(out_dir, "2026-11-13", {
        "symbol": "US.NVDA", "_ckpt_model": "model-x", "rating": "看多",
    })
    # _ckpt_save used the current (on) policy; rewrite with the off baseline.
    ckpt_path = Path(P._ckpt_dir(out_dir, "2026-11-13")) / "US.NVDA.json"
    payload = json.loads(ckpt_path.read_text(encoding="utf-8"))
    payload["_ckpt_policy"] = off_policy
    ckpt_path.write_text(json.dumps(payload), encoding="utf-8")

    assert P._ckpt_load(
        out_dir, "2026-11-13", "US.NVDA", "model-x",
        expect_policy=on_policy) is None

    P._ckpt_save(out_dir, "2026-11-13", {
        "symbol": "US.NVDA", "_ckpt_model": "model-x", "rating": "看多",
    })
    assert P._ckpt_load(
        out_dir, "2026-11-13", "US.NVDA", "model-x",
        expect_policy=on_policy) is not None


@requires_pipeline
def test_checkpoint_promotes_event_context(monkeypatch, tmp_path):
    monkeypatch.setenv("DA_EVENT_CONTEXT_INJECT", "0")
    out_dir = str(tmp_path / "out")
    meta = {
        "schema": "da_event_prompt_injection_v1",
        "injected": True,
        "brief_id": BRIEF_ID,
        "status": "OK",
        "event_ids": [EVENT_ID],
        "injection_hash": "abc123def456",
        "consumers": ["raw_checkpoint", "quality_evaluator"],
        "gate_visible": False,
    }
    analysis = {
        "symbol": "US.NVDA", "_ckpt_model": "model-x", "rating": "看多",
        "_llm_output": {"_event_context": meta},
    }
    P._ckpt_save(out_dir, "2026-11-13", analysis)
    loaded = P._ckpt_load(
        out_dir, "2026-11-13", "US.NVDA", "model-x",
        expect_policy=P._ckpt_policy_version())
    assert loaded is not None
    assert loaded["_event_context"] == meta