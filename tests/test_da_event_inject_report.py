"""RFC-208 · EventContext prompt-injection canary report — hermetic pins.

Covered:
  1. valid report: metadata from both analysis top-level and raw checkpoint,
     per-symbol fields, metrics, flags, latest + immutable history, idempotency;
  2. ungrounded model-cited event IDs fail the canary;
  3. stale metadata (brief_id / injection_hash) and missing event_context;
  4. control rate drop greater than 5pp fails the canary;
  5. ``event_role=NONE`` may not attribute event IDs;
  6. injected samples must carry a non-baseline prompt hash;
  7. ``--eval`` reads latest only (OK/WARN/ERROR, zero writes);
  8. record-only claim validation: unsupported indicator claims (KDJ vs a
     legitimately collected RSI/MA/OSC), PE valuation contradictions
     (TTM vs dynamic/normalized spread, undisclosed calibers, <5 peer sample),
     "officially confirmed" over UNVERIFIED sources, and no false positives on
     clean data.  These never move ``canary_pass``.

All inputs live under ``tmp_path``; ``day_bars`` and the baseline prompt hash
are monkeypatched so no real bars, skill module, network, DB or paid call is
touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import da_event_inject_report as report  # noqa: E402

DATE = "2026-11-12"
AS_OF = "2026-11-12T21:00:00+00:00"
BRIEF_ID = "brief-canary-1"
EVENT_ID = "EV-NVDA-1"
VALID_UNTIL = "2026-11-12T16:00:00-05:00"
INJECTED_PROMPT_HASH = "prompt-hash-with-injection"
BASELINE_PROMPT_HASH = "prompt-hash-baseline"
BARS = [[0, 100.0, 115.0, 98.0, 108.0, 0]]


def _brief_doc(
    *,
    brief_id: str = BRIEF_ID,
    as_of: str = AS_OF,
    fact_status: str = "UNVERIFIED",
    member_status: str | None = None,
) -> dict:
    member = {"event_id": "EV-MEMBER-1", "headline": "member"}
    if member_status is None:
        member["status"] = fact_status
    else:
        member["status"] = member_status
    return {
        "record_only": True,
        "status": "OK",
        "as_of": as_of,
        "validation_errors": [],
        "brief": {
            "schema": "market_event_brief_v2",
            "brief_id": brief_id,
            "as_of": as_of,
            "validation_errors": [],
            "facts": [{
                "event_id": EVENT_ID,
                "event_time": "2026-11-12T12:00:00+00:00",
                "first_seen_at": "2026-11-12T12:05:00+00:00",
                "headline": "Fixture headline",
                "status": fact_status,
                "member_event_ids": ["EV-MEMBER-1"],
                "member_facts": [member],
            }],
            "transmissions": [{
                "event_ids": [EVENT_ID],
                "theme": "ai_demand",
                "sign": "POSITIVE",
                "confidence": 0.6,
                "beneficiary_symbols": ["NVDA"],
                "loser_symbols": [],
            }],
        },
    }


def _write_brief(
    path: Path,
    *,
    brief_id: str = BRIEF_ID,
    as_of: str = AS_OF,
    fact_status: str = "UNVERIFIED",
) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_brief_doc(brief_id=brief_id, as_of=as_of, fact_status=fact_status)),
        encoding="utf-8",
    )
    return report.load_brief_latest(path)


def _context(brief_info: dict, **overrides) -> dict:
    ctx = {
        "schema": "da_event_prompt_injection_v1",
        "injected": True,
        "reason": "injected",
        "brief_id": brief_info["brief_id"],
        "status": brief_info["status"],
        "as_of": AS_OF,
        "valid_until": VALID_UNTIL,
        "event_ids": [EVENT_ID],
        "latest_path": brief_info["path"],
        "gate_visible": False,
        "advisory_only": True,
        "record_only": False,
    }
    ctx.update(overrides)
    if "injection_hash" not in overrides:
        ctx["injection_hash"] = report.expected_injection_hash(ctx, brief_info)
    return ctx


def _llm(
    *,
    direction: str = "BUY",
    event_ids=("EV-NVDA-1",),
    event_role: str = "PRIMARY",
    prompt_hash: str = INJECTED_PROMPT_HASH,
    **overrides,
) -> dict:
    card = {
        "executive_summary": "Fixture summary",
        "causal_chain": "demand -> revenue",
        "bull_case": ["demand"],
        "bear_case": ["valuation"],
        "direction": direction,
        "score": 7.5,
        "event_role": event_role,
        "event_ids": list(event_ids),
        "_prompt_hash": prompt_hash,
        "execution_plan": {
            "entry_price": 100.0,
            "stop_loss_price": 95.0,
            "take_profit_price": 110.0,
            "entry_archetype": "breakout",
        },
    }
    card.update(overrides)
    return card


def _write_analysis(
    checkpoint_root: Path,
    symbol: str,
    *,
    llm: dict | None = None,
    ctx: dict | None = None,
    ctx_in_llm: bool = False,
    top_level: bool = True,
    extra: dict | None = None,
) -> Path:
    directory = checkpoint_root / DATE
    directory.mkdir(parents=True, exist_ok=True)
    card = _llm() if llm is None else llm
    if ctx is not None and ctx_in_llm:
        card = dict(card)
        card["_event_context"] = ctx
    analysis = {
        "symbol": symbol,
        "rating": "看多",
        "score": 7.5,
        "_llm_output": card,
    }
    if extra:
        analysis.update(extra)
    if ctx is not None and top_level:
        analysis["_event_context"] = ctx
    path = directory / f"{symbol}.json"
    path.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    return path


def _write_raw(
    checkpoint_root: Path,
    symbol: str,
    *,
    ctx: dict | None = None,
    user_prompt: str | None = "fixture user prompt",
    collected: dict | None = None,
) -> Path:
    directory = checkpoint_root / DATE
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"symbol": symbol, "trade_date": DATE}
    if user_prompt is not None:
        payload["user_prompt"] = user_prompt
    if ctx is not None:
        payload["event_context"] = ctx
    if collected is not None:
        payload["collected"] = collected
    path = directory / f"{symbol}.raw.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    checkpoint_root = tmp_path / "da_checkpoint"
    (checkpoint_root / DATE).mkdir(parents=True)
    brief_path = tmp_path / "event_intelligence" / "latest.json"
    brief_info = _write_brief(brief_path)
    out = tmp_path / "canary"
    latest = out / "latest.json"
    monkeypatch.setattr(report, "day_bars", lambda symbol, day: BARS)
    monkeypatch.setattr(report, "baseline_prompt_hash", lambda: BASELINE_PROMPT_HASH)
    return SimpleNamespace(
        tmp=tmp_path,
        checkpoint_root=checkpoint_root,
        checkpoint_dir=checkpoint_root / DATE,
        brief_path=brief_path,
        brief_info=brief_info,
        out=out,
        latest=latest,
    )


def _argv(env, *extra: str) -> list[str]:
    return [
        "--date", DATE,
        "--checkpoint-root", str(env.checkpoint_root),
        "--brief-latest", str(env.brief_path),
        "--out-root", str(env.out),
        "--latest-path", str(env.latest),
        *extra,
    ]


def _run(capsys, env, *extra: str) -> tuple[int, dict]:
    code = report.main(_argv(env, *extra))
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    return code, payload


def _detail(report_doc: dict, symbol: str) -> dict:
    rows = [row for row in report_doc["symbols"] if row["symbol"] == symbol]
    assert len(rows) == 1, report_doc["symbols"]
    return rows[0]


def _history_files(env) -> list[Path]:
    directory = env.out / DATE
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


# ---------------------------------------------------------------------------
# 1. Valid report + idempotent latest/history
# ---------------------------------------------------------------------------


def test_valid_report_fields_metrics_and_idempotent_history(sandbox, capsys):
    ctx_nvda = _context(sandbox.brief_info)
    _write_analysis(sandbox.checkpoint_root, "US.NVDA", ctx=ctx_nvda)
    ctx_amd = _context(sandbox.brief_info)
    _write_analysis(sandbox.checkpoint_root, "US.AMD")          # metadata only in raw
    _write_raw(sandbox.checkpoint_root, "US.AMD", ctx=ctx_amd)

    code, doc = _run(capsys, sandbox)
    assert code == 0, doc
    assert doc["status"] == "OK"
    assert doc["canary_pass"] is True
    assert doc["economic_value_claimed"] is False
    assert doc["production_write"] is False
    assert doc["paid_calls"] == 0
    assert doc["db_writes"] is False

    metrics = doc["metrics"]
    assert metrics["injected_count"] == 2
    assert metrics["total"] == 2
    assert metrics["skipped"] == 0
    assert metrics["directions"] == {"BUY": 2}
    assert metrics["skipped_reasons"] == {}
    assert metrics["valid_card_rate"] == 1.0
    assert metrics["research_complete_rate"] == 1.0
    assert metrics["ungrounded_event_count"] == 0
    assert metrics["role_none_with_event_ids_count"] == 0

    nvda = _detail(doc, "US.NVDA")
    assert nvda["event_context_source"] == "analysis_top_level"
    assert nvda["injected"] is True
    assert nvda["reason"] == "injected"
    assert nvda["brief_id"] == BRIEF_ID
    assert nvda["injection_hash"]
    assert nvda["event_ids"] == [EVENT_ID]
    assert nvda["prompt_hash"] == INJECTED_PROMPT_HASH
    assert nvda["user_prompt_present"] is False
    assert nvda["direction"] == "BUY"
    assert nvda["score"] == 7.5
    assert nvda["valid_card"] is True
    assert nvda["research_complete"] is True
    assert nvda["price_geometry_valid"] is True
    assert nvda["checks"]["metadata_ok"] is True
    assert nvda["checks"]["prompt_hash_not_baseline"] is True
    assert nvda["checks"]["grounding_ok"] is True
    assert nvda["checks"]["role_event_ids_consistent"] is True
    assert nvda["blocking"] is False

    amd = _detail(doc, "US.AMD")
    assert amd["event_context_source"] == "raw_checkpoint"
    assert amd["raw_user_prompt_present"] is True
    assert amd["user_prompt_present"] is True

    assert sandbox.latest.is_file()
    latest = json.loads(sandbox.latest.read_text(encoding="utf-8"))
    assert latest["schema"] == report.SCHEMA
    assert latest["content_hash"] == doc["content_hash"]
    assert latest["check_count"] == 1
    artifacts = _history_files(sandbox)
    assert len(artifacts) == 1
    assert artifacts[0].name == f"{doc['content_hash']}.json"
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["canary_pass"] is True
    assert artifact["production_write"] is False
    artifact_bytes = artifacts[0].read_bytes()

    # Same inputs => same immutable artifact, latest only bumps check_count.
    code, second = _run(capsys, sandbox)
    assert code == 0
    assert second["content_hash"] == doc["content_hash"]
    assert second["check_count"] == 2
    assert len(_history_files(sandbox)) == 1
    assert artifacts[0].read_bytes() == artifact_bytes


# ---------------------------------------------------------------------------
# 2. Ungrounded model event IDs
# ---------------------------------------------------------------------------


def test_ungrounded_event_id_fails_canary(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(event_ids=("EV-NOT-IN-BRIEF",), event_role="PRIMARY"),
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["status"] == "WARN"
    assert doc["canary_pass"] is False
    assert doc["metrics"]["ungrounded_event_count"] == 1
    detail = _detail(doc, "US.NVDA")
    assert detail["ungrounded_event_ids"] == ["EV-NOT-IN-BRIEF"]
    assert "ungrounded_event:EV-NOT-IN-BRIEF" in detail["issues"]
    assert detail["blocking"] is True
    assert any("ungrounded_event:EV-NOT-IN-BRIEF" in item for item in doc["issues"])


# ---------------------------------------------------------------------------
# 3. Stale / missing metadata
# ---------------------------------------------------------------------------


def test_stale_metadata_fails_canary(sandbox, capsys):
    stale_ctx = _context(sandbox.brief_info)
    _write_analysis(sandbox.checkpoint_root, "US.NVDA", ctx=stale_ctx)
    # The shared brief moved on after this checkpoint was written.
    _write_brief(sandbox.brief_path, brief_id="brief-canary-2")

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["status"] == "WARN"
    assert doc["canary_pass"] is False
    detail = _detail(doc, "US.NVDA")
    assert detail["checks"]["brief_id_match"] is False
    assert detail["checks"]["injection_hash_match"] is False
    assert detail["checks"]["metadata_ok"] is False
    assert "brief_id_mismatch" in detail["issues"]
    assert "injection_hash_mismatch" in detail["issues"]


def test_missing_event_context_is_warn_and_skipped(sandbox, capsys):
    _write_analysis(sandbox.checkpoint_root, "US.NVDA")  # no context anywhere

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["status"] == "WARN"
    assert doc["canary_pass"] is False
    assert doc["metrics"]["injected_count"] == 0
    assert doc["metrics"]["skipped"] == 1
    assert doc["metrics"]["skipped_reasons"] == {"event_context_missing": 1}
    assert "no_injected_samples" in doc["issues"]
    detail = _detail(doc, "US.NVDA")
    assert detail["reason"] == "event_context_missing"
    assert detail["event_context_present"] is False


# ---------------------------------------------------------------------------
# 4. Control drop > 5pp
# ---------------------------------------------------------------------------


def test_control_rate_drop_above_5pp_fails_canary(sandbox, capsys, tmp_path):
    ctx = _context(sandbox.brief_info)
    _write_analysis(sandbox.checkpoint_root, "US.NVDA", ctx=ctx)
    ctx_amd = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.AMD", ctx=ctx_amd,
        llm=_llm(direction="HOLD"),  # invalid card => canary valid rate 0.5
    )

    control = tmp_path / "control.jsonl"
    control.write_text("".join(json.dumps(row) + "\n" for row in [
        {"model": "qwen3.7-max", "day": DATE, "symbol": "US.NVDA",
         "direction": "BUY", "valid_card": True, "research_complete": True},
        {"model": "qwen3.7-max", "day": DATE, "symbol": "US.AMD",
         "direction": "BUY", "valid_card": True, "research_complete": True},
    ]), encoding="utf-8")

    code, doc = _run(capsys, sandbox, "--control", str(control))
    assert code == 1
    assert doc["status"] == "WARN"
    assert doc["canary_pass"] is False
    comparison = doc["control"]
    assert comparison["enabled"] is True
    assert comparison["pass"] is False
    valid_check = comparison["rates"]["valid_card"]
    assert valid_check["control_rate"] == 1.0
    assert valid_check["canary_rate"] == 0.5
    assert valid_check["delta_pp"] == -50.0
    assert valid_check["pass"] is False
    assert "control_rate_drop_above_5pp" in doc["issues"]
    assert comparison["direction_compared"] == 1
    assert comparison["direction_matched"] == 1


def test_control_v3_input_arms_require_explicit_selection_and_never_mix(tmp_path):
    control = tmp_path / "control.jsonl"
    rows = [
        {"model": "qwen4-max", "input_mode": "full", "day": DATE,
         "symbol": "US.NVDA", "direction": "BUY", "valid_card": True,
         "research_complete": True},
        {"model": "qwen4-max", "input_mode": "legacy", "day": DATE,
         "symbol": "US.NVDA", "direction": "BUY", "valid_card": False,
         "research_complete": False},
    ]
    control.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    manifest = {
        "production_settings": {"model": "qwen4-max"},
        "input_modes": ["full", "legacy"],
        "summary": {
            "qwen4-max|full": {"n": 1, "valid_card_rate": 1.0,
                               "research_complete_rate": 1.0},
            "qwen4-max|legacy": {"n": 1, "valid_card_rate": 0.0,
                                 "research_complete_rate": 0.0},
        },
    }
    control.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="multiple input modes"):
        report.load_control(control)
    full = report.load_control(control, input_mode="full")
    legacy = report.load_control(control, input_mode="legacy")
    assert full["record_count"] == 1
    assert full["valid_card_rate"] == 1.0
    assert full["input_mode"] == "full"
    assert legacy["record_count"] == 1
    assert legacy["valid_card_rate"] == 0.0


def test_control_v3_manifest_only_uses_exact_model_input_key(tmp_path):
    control = tmp_path / "control.json"
    control.write_text(json.dumps({
        "production_settings": {"model": "qwen4-max"},
        "input_modes": ["full", "legacy"],
        "summary": {
            "qwen4-max|full": {"n": 2, "valid_card_rate": 1.0,
                               "research_complete_rate": 1.0},
            "qwen4-max|legacy": {"n": 3, "valid_card_rate": 0.25,
                                 "research_complete_rate": 0.5},
        },
    }), encoding="utf-8")
    selected = report.load_control(control, input_mode="legacy")
    assert selected["record_count"] == 3
    assert selected["valid_card_rate"] == 0.25
    assert selected["research_complete_rate"] == 0.5


def test_control_empty_arm_or_missing_production_model_fails_closed(tmp_path):
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({
        "model": "qwen4-max", "symbol": "US.NVDA", "direction": "BUY",
        "valid_card": True, "research_complete": True,
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="do not declare input_mode"):
        report.load_control(legacy, input_mode="full")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "production_settings": {"model": "qwen4-max"},
        "input_modes": ["full"],
        "summary": {"qwen4-max|legacy": {"n": 1, "valid_card_rate": 1.0}},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="zero usable records"):
        report.load_control(manifest, input_mode="full")

    wrong_model = tmp_path / "wrong.jsonl"
    wrong_model.write_text(json.dumps({
        "model": "qwen3.7-max", "input_mode": "full", "symbol": "US.NVDA",
        "direction": "BUY", "valid_card": True, "research_complete": True,
    }) + "\n", encoding="utf-8")
    wrong_model.with_suffix(".manifest.json").write_text(json.dumps({
        "production_settings": {"model": "qwen4-max"}, "input_modes": ["full"],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="zero usable records"):
        report.load_control(wrong_model, input_mode="full")


# ---------------------------------------------------------------------------
# 5. event_role=NONE may not attribute event IDs
# ---------------------------------------------------------------------------


def test_event_role_none_cannot_attribute_event_ids(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(event_role="NONE", event_ids=(EVENT_ID,)),
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["canary_pass"] is False
    detail = _detail(doc, "US.NVDA")
    assert detail["event_role"] == "NONE"
    assert detail["llm_event_ids"] == [EVENT_ID]
    assert "role_none_with_event_ids" in detail["issues"]
    assert detail["checks"]["role_event_ids_consistent"] is False
    assert doc["metrics"]["role_none_with_event_ids_count"] == 1


# ---------------------------------------------------------------------------
# 6. Injected prompt hash must not be the baseline
# ---------------------------------------------------------------------------


def test_baseline_prompt_hash_when_injected_fails_canary(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(prompt_hash=BASELINE_PROMPT_HASH),
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["canary_pass"] is False
    detail = _detail(doc, "US.NVDA")
    assert detail["checks"]["prompt_hash_not_baseline"] is False
    assert "prompt_hash_baseline_when_injected" in detail["issues"]


# ---------------------------------------------------------------------------
# 7. --eval reads latest only
# ---------------------------------------------------------------------------


def test_eval_is_read_only_and_reports_ok_warn_error(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(sandbox.checkpoint_root, "US.NVDA", ctx=ctx)
    code, doc = _run(capsys, sandbox)
    assert code == 0 and doc["canary_pass"] is True

    latest_before = sandbox.latest.read_bytes()
    history_before = [path.read_bytes() for path in _history_files(sandbox)]

    eval_code = report.main(["--eval", "--latest-path", str(sandbox.latest)])
    eval_doc = json.loads(capsys.readouterr().out)
    assert eval_code == 0
    assert eval_doc["status"] == "OK"
    assert eval_doc["canary_pass"] is True
    assert eval_doc["artifact_exists"] is True
    assert sandbox.latest.read_bytes() == latest_before
    assert [path.read_bytes() for path in _history_files(sandbox)] == history_before

    # WARN: latest exists but the canary did not pass.
    warned = json.loads(latest_before)
    warned["canary_pass"] = False
    warned["status"] = "WARN"
    sandbox.latest.write_text(json.dumps(warned), encoding="utf-8")
    warn_code = report.main(["--eval", "--latest-path", str(sandbox.latest)])
    warn_doc = json.loads(capsys.readouterr().out)
    assert warn_code == 1
    assert warn_doc["status"] == "WARN"
    assert warn_doc["canary_pass"] is False

    # Date mismatch and missing latest are ERROR.
    date_code = report.main([
        "--eval", "--latest-path", str(sandbox.latest), "--date", "2026-11-11",
    ])
    date_doc = json.loads(capsys.readouterr().out)
    assert date_code == 2
    assert date_doc["status"] == "ERROR"
    assert any(item.startswith("date_mismatch") for item in date_doc["issues"])

    sandbox.latest.unlink()
    missing_code = report.main(["--eval", "--latest-path", str(sandbox.latest)])
    missing_doc = json.loads(capsys.readouterr().out)
    assert missing_code == 2
    assert missing_doc["status"] == "ERROR"
    assert missing_doc["reason"] == "latest_missing"


# ---------------------------------------------------------------------------
# 8. Record-only claim validation (evidence quality; never a gate)
# ---------------------------------------------------------------------------

# Mirrors the real 2026-11-13 US.NVDA checkpoint: only MA/RSI are computed,
# the card still claims "OSC超卖且KDJ低位金叉".
NVDA_TECHNICAL = {
    "price": 166.19,
    "ma5": 174.82,
    "ma10": 186.92,
    "rsi14": 24.5,
}
KDJ_BULL = [{
    "thesis": "技术面超卖反弹：盘前+1.7%至169.01，OSC超卖且KDJ低位金叉，机械买盘承接。",
    "evidence": "52周最低=148.80, 昨收=166.19, 盘前涨跌幅=+1.70%",
    "source_type": "technical",
    "claim_type": "technical_breakout",
}]


def _peers(rows) -> dict:
    return {
        "industry": "Semiconductors",
        "peer_count": 8,
        "peers": [{"symbol": symbol, "pe": pe} for symbol, pe in rows],
        "caliber_version": "session_effective_v1",
        "_source": "industry_peers_v039",
    }


def test_unsupported_kdj_claim_recorded_without_touching_canary_pass(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(bull_case=KDJ_BULL, bear_case=["valuation"]),
        extra={"technical": dict(NVDA_TECHNICAL)},
    )
    _write_raw(
        sandbox.checkpoint_root, "US.NVDA",
        collected={"snapshot": {"pe_ratio": 33.916}},
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1                       # WARN
    assert doc["status"] == "WARN"
    # RFC-208 pass criteria are untouched by claim-quality findings.
    assert doc["canary_pass"] is True
    assert doc["canary_issues"] == []
    assert doc["evidence_quality_pass"] is False
    assert doc["claim_validation"] == {
        "record_only": True,
        "gate_visible": False,
        "affects_canary_pass": False,
        "issue_types": [
            "unsupported_indicator_claim",
            "valuation_contradiction",
            "event_factual_precision_warning",
        ],
        "thresholds": {"valuation_relative_deviation": 0.25, "min_peer_sample": 5},
    }
    assert doc["metrics"]["unsupported_claim_count"] == 1
    assert doc["metrics"]["valuation_contradiction_count"] == 0
    assert doc["metrics"]["event_factual_precision_count"] == 0
    assert doc["metrics"]["evidence_quality_fail_symbol_count"] == 1
    assert "US.NVDA:unsupported_indicator_claim:KDJ" in doc["issues"]
    assert doc["evidence_quality_issues"] == ["US.NVDA:unsupported_indicator_claim:KDJ"]

    detail = _detail(doc, "US.NVDA")
    assert detail["blocking"] is False
    assert detail["evidence_quality_pass"] is False
    assert detail["evidence_quality_issues"] == ["unsupported_indicator_claim:KDJ"]
    assert "unsupported_indicator_claim:KDJ" in detail["issues"]
    assert detail["valuation_contradictions"] == []
    assert detail["event_factual_precision_warnings"] == []

    records = detail["unsupported_indicator_claims"]
    assert len(records) == 1
    record = records[0]
    assert record["issue"] == "unsupported_indicator_claim"
    assert record["indicator"] == "KDJ"
    # KDJ is documented-only: lv3_technical_calculator.py never computes it.
    assert record["reason"] == "no_implementation"
    assert record["machine_series_present"] is False
    assert record["claim_fields"] == ["bull_case[0].thesis"]
    assert "KDJ低位金叉" in record["claim_text"]
    assert record["claim_count"] == 1
    assert record["source_fields_checked"] == [
        "analysis.technical", "raw.collected",
    ]
    assert record["expected_evidence_key_patterns"] == [
        "kdj(_[kdj])?", "stoch(astic)?(_[kd])?",
    ]

    # --eval keeps the same non-interference contract.
    eval_code = report.main(["--eval", "--latest-path", str(sandbox.latest)])
    eval_doc = json.loads(capsys.readouterr().out)
    assert eval_code == 1
    assert eval_doc["status"] == "WARN"
    assert eval_doc["canary_pass"] is True
    assert eval_doc["evidence_quality_pass"] is False
    assert "US.NVDA:unsupported_indicator_claim:KDJ" in eval_doc["issues"]


def test_collected_osc_rsi_ma_claims_are_not_flagged(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=[{
                "thesis": "OSC超卖，RSI14=24.5 处于低位，MA5=174.82 均线支撑明显。",
                "evidence": "RSI14=24.5, MA5=174.82",
                "claim_type": "technical_breakout",
            }],
            bear_case=["valuation"],
        ),
        extra={"technical": dict(NVDA_TECHNICAL)},
    )

    code, doc = _run(capsys, sandbox)
    assert code == 0
    assert doc["status"] == "OK"
    assert doc["canary_pass"] is True
    assert doc["evidence_quality_pass"] is True
    assert doc["metrics"]["unsupported_claim_count"] == 0
    assert doc["evidence_quality_issues"] == []

    detail = _detail(doc, "US.NVDA")
    assert detail["unsupported_indicator_claims"] == []
    machine_series = detail["evidence_quality_data"]["machine_series"]
    # RSI/MA are real series; OSC is an umbrella label backed by rsi14.
    assert set(machine_series) >= {"RSI", "MA", "OSC"}
    assert machine_series["RSI"] == {"key": "rsi14", "source_field": "analysis.technical.rsi14"}
    assert machine_series["MA"] == {"key": "ma5", "source_field": "analysis.technical.ma5"}


def test_valuation_contradiction_records_calibers_and_small_peer_sample(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=[{
                "thesis": "估值极度错杀：PE(TTM)仅21.01，远低于半导体行业中枢47.54。",
                "evidence": "PE(TTM)=21.01, 行业PE中位数=47.54",
                "claim_type": "valuation_cheap",
            }],
            bear_case=["valuation"],
        ),
        extra={"snapshot": {"pe_ttm": 21.01, "pe_ratio": 33.916}},
    )
    _write_raw(
        sandbox.checkpoint_root, "US.NVDA",
        collected={
            "snapshot": {"pe_ttm_ratio": 21.01, "pe_ratio": 33.916},
            "fundamentals": {"pe_normalized": 34.8713},
            "industry_peers": _peers([
                ("AMD", 126.963),
                ("AVGO", 46.474),
                ("MU", 10.741),
                ("INTC", -26.042),
                ("ADI", 48.609),
            ]),
        },
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["status"] == "WARN"
    assert doc["canary_pass"] is True
    assert doc["evidence_quality_pass"] is False
    assert doc["metrics"]["valuation_contradiction_count"] == 1
    assert doc["metrics"]["unsupported_claim_count"] == 0

    detail = _detail(doc, "US.NVDA")
    assert len(detail["valuation_contradictions"]) == 1
    record = detail["valuation_contradictions"][0]
    assert record["issue"] == "valuation_contradiction"
    assert record["triggered_by"] == [
        "material_deviation", "undisclosed_estimators", "small_peer_sample",
    ]
    assert record["pe_estimators"]["pe_ttm"] == {
        "value": 21.01, "source_field": "analysis.snapshot.pe_ttm",
    }
    assert record["pe_estimators"]["pe_dynamic"] == {
        "value": 33.916, "source_field": "analysis.snapshot.pe_ratio",
    }
    assert record["pe_estimators"]["pe_normalized"] == {
        "value": 34.8713,
        "source_field": "raw.collected.fundamentals.pe_normalized",
    }
    assert record["max_relative_deviation"] == pytest.approx(0.3975, abs=1e-4)
    assert record["relative_deviation_threshold"] == 0.25
    assert [pair["pair"] for pair in record["pe_pairs"]] == [
        "pe_ttm_vs_pe_dynamic", "pe_ttm_vs_pe_normalized",
    ]
    assert all(pair["exceeds_threshold"] for pair in record["pe_pairs"])
    assert record["disclosed_estimators"] == ["pe_ttm"]
    assert record["undisclosed_estimators"] == ["pe_dynamic", "pe_normalized"]
    assert record["industry_pe_median"] == {
        "claimed": 47.54,
        "computed_from_peers": 47.5415,
        "matches": True,
    }
    peer_sample = record["peer_sample"]
    assert peer_sample["source_field"] == "raw.collected.industry_peers"
    assert peer_sample["declared_peer_count"] == 8
    assert peer_sample["listed_peer_count"] == 5
    assert peer_sample["pe_peer_count"] == 4
    assert peer_sample["min_required"] == 5
    assert peer_sample["peer_symbols_with_pe"] == ["AMD", "AVGO", "MU", "ADI"]
    assert peer_sample["excluded_non_positive_pe"] == ["INTC"]
    assert record["small_peer_sample"] is True
    assert "valuation_contradiction:small_peer_sample" in detail["evidence_quality_issues"]
    assert "valuation_contradiction:material_deviation" in detail["evidence_quality_issues"]


def test_small_peer_sample_alone_warns(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=[{
                "thesis": "PE(TTM)=30.00、PE(动态)=30.00、PE(normalized)=30.00，"
                          "行业PE中位数=21.00 由3家peer算得，估值折价明显。",
                "evidence": "PE(TTM)=30.00, PE(动态)=30.00, PE(normalized)=30.00",
            }],
            bear_case=["valuation"],
        ),
        extra={"snapshot": {"pe_ttm": 30.0, "pe_ratio": 30.0}},
    )
    _write_raw(
        sandbox.checkpoint_root, "US.NVDA",
        collected={
            "fundamentals": {"pe_normalized": 30.0},
            "industry_peers": _peers([("AAA", 20.0), ("BBB", 21.0), ("CCC", 22.0)]),
        },
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["canary_pass"] is True
    assert doc["metrics"]["valuation_contradiction_count"] == 1

    record = _detail(doc, "US.NVDA")["valuation_contradictions"][0]
    assert record["triggered_by"] == ["small_peer_sample"]
    assert record["undisclosed_estimators"] == []
    assert record["max_relative_deviation"] == 0.0
    assert record["peer_sample"]["pe_peer_count"] == 3
    assert record["industry_pe_median"]["computed_from_peers"] == 21.0
    assert record["industry_pe_median"]["matches"] is True


def test_valid_disclosed_valuation_has_no_false_positive(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=[{
                "thesis": "PE(TTM)=30.00、PE(动态)=30.50、PE(normalized)=31.00 三个口径一致，"
                          "行业PE中位数=21.00（5家peer），RSI14=24.5 超卖，MA5=174.82 支撑。",
                "evidence": "PE(TTM)=30.00, PE(动态)=30.50, PE(normalized)=31.00",
            }],
            bear_case=["valuation"],
        ),
        extra={
            "technical": dict(NVDA_TECHNICAL),
            "snapshot": {"pe_ttm": 30.0, "pe_ratio": 30.5},
        },
    )
    _write_raw(
        sandbox.checkpoint_root, "US.NVDA",
        collected={
            "fundamentals": {"pe_normalized": 31.0},
            "industry_peers": _peers([
                ("AAA", 19.0), ("BBB", 20.0), ("CCC", 21.0),
                ("DDD", 22.0), ("EEE", 23.0),
            ]),
        },
    )

    code, doc = _run(capsys, sandbox)
    assert code == 0
    assert doc["status"] == "OK"
    assert doc["canary_pass"] is True
    assert doc["evidence_quality_pass"] is True
    assert doc["metrics"]["unsupported_claim_count"] == 0
    assert doc["metrics"]["valuation_contradiction_count"] == 0
    assert doc["metrics"]["event_factual_precision_count"] == 0
    assert doc["evidence_quality_issues"] == []

    detail = _detail(doc, "US.NVDA")
    assert detail["evidence_quality_issues"] == []
    assert detail["valuation_contradictions"] == []
    assert detail["unsupported_indicator_claims"] == []


def test_claim_findings_never_enter_canary_issues(sandbox, capsys):
    """A blocking symbol keeps canary/claim issues in separate buckets."""
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=KDJ_BULL,
            bear_case=["valuation"],
            event_ids=("EV-NOT-IN-BRIEF",),
            event_role="PRIMARY",
        ),
        extra={"technical": dict(NVDA_TECHNICAL)},
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["canary_pass"] is False
    assert doc["evidence_quality_pass"] is False
    assert doc["canary_issues"] == ["US.NVDA:ungrounded_event:EV-NOT-IN-BRIEF"]
    assert doc["evidence_quality_issues"] == ["US.NVDA:unsupported_indicator_claim:KDJ"]
    assert doc["issues"] == doc["canary_issues"] + doc["evidence_quality_issues"]
    detail = _detail(doc, "US.NVDA")
    assert detail["blocking"] is True
    assert "unsupported_indicator_claim:KDJ" in detail["issues"]
    assert "ungrounded_event:EV-NOT-IN-BRIEF" in detail["issues"]


def test_officially_confirmed_claim_against_unverified_sources_warns(sandbox, capsys):
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=[{
                "thesis": "公司官方确认CEO辞职，管理层过渡安排已完成。",
                "evidence": "官方确认",
            }],
            bear_case=["valuation"],
        ),
    )

    code, doc = _run(capsys, sandbox)
    assert code == 1
    assert doc["status"] == "WARN"
    assert doc["canary_pass"] is True
    assert doc["evidence_quality_pass"] is False
    assert doc["metrics"]["event_factual_precision_count"] == 2

    detail = _detail(doc, "US.NVDA")
    assert detail["evidence_quality_issues"] == [
        "event_factual_precision_warning:官方确认",
    ]
    records = detail["event_factual_precision_warnings"]
    # One record per claim field that carries the wording.
    assert [item["claim_field"] for item in records] == [
        "bull_case[0].thesis", "bull_case[0].evidence",
    ]
    record = records[0]
    assert record["issue"] == "event_factual_precision_warning"
    assert record["confirmation_marker"] == "官方确认"
    assert record["scope"] == "cited_events"
    assert record["source_status"] == "UNVERIFIED"
    assert record["resolved_event_count"] == 1
    assert record["unverified_event_count"] == 1
    assert record["brief_fact_status_counts"] == {"UNVERIFIED": 1}
    assert doc["brief_latest"]["fact_status_counts"] == {"UNVERIFIED": 1}
    assert doc["brief_latest"]["verified_event_count"] == 0


def test_officially_confirmed_claim_with_corroborated_source_is_quiet(sandbox, capsys):
    sandbox.brief_info = _write_brief(sandbox.brief_path, fact_status="CORROBORATED")
    ctx = _context(sandbox.brief_info)
    _write_analysis(
        sandbox.checkpoint_root, "US.NVDA", ctx=ctx,
        llm=_llm(
            bull_case=[{
                "thesis": "公司官方确认CEO辞职，管理层过渡安排已完成。",
                "evidence": "官方确认",
            }],
            bear_case=["valuation"],
        ),
    )

    code, doc = _run(capsys, sandbox)
    assert code == 0
    assert doc["status"] == "OK"
    assert doc["evidence_quality_pass"] is True
    assert doc["metrics"]["event_factual_precision_count"] == 0
    detail = _detail(doc, "US.NVDA")
    assert detail["event_factual_precision_warnings"] == []
    assert doc["brief_latest"]["verified_event_count"] == 1
