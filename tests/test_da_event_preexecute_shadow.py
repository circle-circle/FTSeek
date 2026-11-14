"""DA 执行前 EventContext 新鲜度/生命周期影子 — hermetic pins.

守七件事：
  1. 同一 brief_id + 同一 injection 内容哈希 ⇒ NO_CHANGE；
  2. 有效但不同的 brief ⇒ REVIEW，带 old/new ID、变化 transmission/theme ID、
     受影响 canary 标的；
  3. 当前 latest 缺失/无效/超龄/时间戳非法或未来 ⇒ REVIEW_INVALID_BRIEF，
     绝不静默当作新鲜；
  4. treatment canary 缺失/未注入/哈希不一致 ⇒ REVIEW_INVALID_BRIEF；
  5. 幂等：相同输入重跑共用同一 content_hash / 不可变 artifact，只递增 check_count；
  6. disabled 零副作用：不读业务文件、不建目录、不写文件；
  7. `--execute-refresh` 双开关闸：未显式请求或未同时打开两个 env flag 时零付费调用；
     评估器只读，只有 OK/REVIEW/NO_DATA/ERROR 四态，且无 PnL。

所有输入都在 ``tmp_path`` 内；不碰生产 data/、DB、LLM 或网络。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.event_stream.da_prompt_injection import (  # noqa: E402
    load_event_context,
    valid_until_et,
)
from scripts import da_event_preexecute_shadow as shadow  # noqa: E402
from scripts import da_event_preexecute_shadow_eval as evaluator  # noqa: E402

NOW = datetime(2026, 11, 13, 13, 35, 0, tzinfo=timezone.utc)
DATE = "2026-11-13"
AS_OF = "2026-11-13T12:20:31+00:00"
GENERATED_AT = "2026-11-13T12:20:31+00:00"
VALID_UNTIL = valid_until_et(NOW)
INJECT_FLAG = "DA_EVENT_CONTEXT_INJECT"


# ---------------------------------------------------------------------------
# Hermetic fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv(shadow.ENV_FLAG, "1")
    for alias in shadow.ENV_FLAG_ALIASES:
        monkeypatch.setenv(alias, "0")
    monkeypatch.setenv(shadow.CONTEXT_FLAG, "0")


def _write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _transmission(
    *,
    theme: str = "AI demand",
    sign: str = "POSITIVE",
    event_ids=("EV-NVDA-1",),
    loser=(),
    beneficiary=("NVDA",),
) -> dict:
    return {
        "event_ids": list(event_ids),
        "theme": theme,
        "sign": sign,
        "horizon": "INTRADAY",
        "price_in": "UNKNOWN",
        "confidence": 0.6,
        "causal_chain": "fixture causal chain",
        "invalidation": "fixture invalidation",
        "counter_evidence": "no known counter-evidence",
        "loser_symbols": list(loser),
        "beneficiary_symbols": list(beneficiary),
    }


def _fact(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "first_seen_at": "2026-11-13T12:00:00+00:00",
        "headline": f"fixture {event_id}",
        "member_event_ids": [event_id],
        "member_facts": [{"event_id": event_id}],
    }


def _brief(
    brief_id: str = "brief-new",
    *,
    as_of: str = AS_OF,
    transmissions=None,
    facts=None,
) -> dict:
    return {
        "schema": "market_event_brief_v4",
        "brief_id": brief_id,
        "as_of": as_of,
        "facts": facts if facts is not None else [_fact("EV-NVDA-1")],
        "transmissions": transmissions if transmissions is not None else [_transmission()],
        "market_evidence": [],
        "validation_errors": [],
        "llm_calls": 2,
    }


def _latest_doc(
    brief: dict,
    *,
    status: str = "OK",
    generated_at: str = GENERATED_AT,
    date_et: str = DATE,
    stored_errors=None,
) -> dict:
    return {
        "schema": shadow.CURRENT_REPORT_SCHEMA,
        "status": status,
        "date_et": date_et,
        "as_of": brief.get("as_of"),
        "generated_at": generated_at,
        "last_checked_at": generated_at,
        "record_only": True,
        "production_writes": False,
        "validation_errors": list(stored_errors or []),
        "brief_id": brief.get("brief_id"),
        "brief": brief,
        "input_hash": "fixture-input-hash",
        "event_count": 1,
        "fact_count": len(brief.get("facts") or []),
        "transmission_count": len(brief.get("transmissions") or []),
        "llm_calls": 2,
        "artifact_path": "",
    }


def _row(
    symbol: str,
    injection_hash: str,
    *,
    event_ids=(),
    injected: bool = True,
    valid_until: str = VALID_UNTIL,
) -> dict:
    return {
        "symbol": symbol,
        "injected": injected,
        "injection_hash": injection_hash,
        "event_ids": list(event_ids),
        "valid_until": valid_until,
    }


def _canary(brief_id: str, injection_hash: str, *, rows, date: str = DATE) -> dict:
    return {
        "schema": "da_event_prompt_canary_v1",
        "date": date,
        "status": "OK",
        "brief_latest": {
            "path": "data/shadow/event_intelligence/live/latest.json",
            "available": True,
            "error": None,
            "brief_id": brief_id,
            "as_of": AS_OF,
            "status": "OK",
            "fact_count": 1,
            "allowed_event_id_count": 1,
        },
        "content_hash": "fixture-canary-content-hash",
        "symbols": rows,
        "issues": [],
    }


def _injection_hash(latest_path: Path, *, now: datetime = NOW) -> str:
    load = load_event_context(
        latest_path=Path(latest_path),
        now=now,
        environ={INJECT_FLAG: "1"},
    )
    assert load.injected, load.reason
    return load.injection_hash


def _run(
    tmp_path: Path,
    *,
    latest: Path,
    canary: Path,
    now: datetime = NOW,
    history: Path | None = None,
    extra=(),
):
    out = tmp_path / "out"
    code = shadow.main([
        "--latest", str(latest),
        "--canary-latest", str(canary),
        "--out-root", str(out),
        "--history-root", str(history or (tmp_path / "history")),
        "--now", now.isoformat(),
        *extra,
    ])
    report = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    return code, report


def _same_setup(tmp_path: Path):
    brief = _brief("brief-1")
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(brief))
    injection_hash = _injection_hash(latest)
    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-1", injection_hash, rows=[
            _row("US.NVDA", injection_hash, event_ids=["EV-NVDA-1"]),
            _row("US.AMD", injection_hash),
        ]),
    )
    return brief, latest, canary, injection_hash


# ---------------------------------------------------------------------------
# 1. NO_CHANGE
# ---------------------------------------------------------------------------


def test_same_brief_and_injection_hash_is_no_change(tmp_path):
    _, latest_path, canary_path, injection_hash = _same_setup(tmp_path)

    code, report = _run(tmp_path, latest=latest_path, canary=canary_path)

    assert code == 0
    assert report["status"] == shadow.STATUS_NO_CHANGE
    assert report["current_brief_id"] == "brief-1"
    assert report["previous_brief_id"] == "brief-1"
    assert report["current_injection_hash"] == injection_hash
    assert report["previous_injection_hash"] == injection_hash
    assert report["changed_transmission_ids"] == []
    assert report["changed_theme_ids"] == []
    assert report["affected_canary_symbols"] == []
    assert report["reasons"] == ["brief_and_injection_unchanged"]
    assert report["record_only"] is True
    assert report["production_writes"] is False
    assert report["db_writes"] is False
    assert report["gate_visible"] is False
    assert report["pnl_included"] is False
    assert report["paid_calls"] == 0
    assert Path(report["artifact_path"]).is_file()
    assert report["date_et"] == DATE


# ---------------------------------------------------------------------------
# 2. REVIEW with changed transmission / theme IDs and affected symbols
# ---------------------------------------------------------------------------


def test_same_brief_id_but_different_injection_hash_reviews(tmp_path):
    brief = _brief("brief-1")
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(brief))
    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-1", "prior-day-identity", rows=[
            _row("US.NVDA", "prior-day-identity"),
        ]),
    )

    code, report = _run(tmp_path, latest=latest, canary=canary)

    assert code == 1
    assert report["status"] == shadow.STATUS_REVIEW
    assert report["current_brief_id"] == report["previous_brief_id"] == "brief-1"
    assert report["current_injection_hash"] != report["previous_injection_hash"]
    assert "injection_identity_changed" in report["reasons"]


def test_changed_valid_brief_reviews_with_diff(tmp_path):
    old_brief = _brief("brief-old")
    old_latest = _write(tmp_path / "old" / "latest.json", _latest_doc(old_brief))
    old_hash = _injection_hash(old_latest)

    new_brief = _brief(
        "brief-new",
        transmissions=[
            _transmission(theme="AI demand", sign="NEGATIVE", loser=("NVDA",), beneficiary=()),
            _transmission(
                theme="Oil weakness", sign="NEGATIVE", event_ids=("EV-XOM-1",),
                loser=("XOM",), beneficiary=(),
            ),
        ],
        facts=[_fact("EV-NVDA-1"), _fact("EV-XOM-1")],
    )
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(new_brief))
    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-old", old_hash, rows=[
            _row("US.NVDA", old_hash, event_ids=["EV-NVDA-1"]),
            _row("US.AMD", old_hash),
        ]),
    )
    history = _write(
        tmp_path / "history" / DATE / "old-artifact.json",
        {"brief": old_brief},
    ).parent.parent

    code, report = _run(
        tmp_path, latest=latest, canary=canary, history=history,
    )

    assert code == 1
    assert report["status"] == shadow.STATUS_REVIEW
    assert report["current_brief_id"] == "brief-new"
    assert report["previous_brief_id"] == "brief-old"
    assert report["current_injection_hash"] != report["previous_injection_hash"]
    assert report["diff_available"] is True
    assert report["diff_reason"] == "available"
    assert report["previous_brief_source"].startswith("history:")
    assert len(report["changed_transmission_ids"]) == 3
    assert len(report["added_transmissions"]) == 2
    assert len(report["removed_transmissions"]) == 1
    assert shadow._theme_id("AI demand") in report["changed_theme_ids"]
    assert shadow._theme_id("Oil weakness") in report["changed_theme_ids"]
    assert report["affected_canary_symbols"] == ["US.NVDA"]
    assert "brief_id_changed" in report["reasons"]


def test_previous_brief_missing_still_reviews(tmp_path):
    old_brief = _brief("brief-old")
    old_latest = _write(tmp_path / "old" / "latest.json", _latest_doc(old_brief))
    old_hash = _injection_hash(old_latest)
    new_brief = _brief("brief-new", transmissions=[_transmission(theme="Different theme")])
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(new_brief))
    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-old", old_hash, rows=[_row("US.NVDA", old_hash)]),
    )

    code, report = _run(tmp_path, latest=latest, canary=canary)

    assert code == 1
    assert report["status"] == shadow.STATUS_REVIEW
    assert report["diff_available"] is False
    assert report["diff_reason"] == "previous_brief_unavailable"
    assert "previous_brief_unavailable" in report["reasons"]
    assert report["affected_canary_symbols"] == []


# ---------------------------------------------------------------------------
# 3. 当前 latest 无效 / 缺失 / 时间戳非法或未来
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", "current_latest_missing"),
        ("bad_status", "current_status_invalid:ERROR"),
        ("bad_generated_at", "current_generated_at_invalid"),
        ("future_generated_at", "current_generated_at_in_future"),
        ("future_as_of", "current_brief_as_of_in_future"),
        ("stale", "current_latest_stale"),
        ("not_record_only", "current_record_only_not_true"),
    ],
)
def test_invalid_current_latest_is_review_invalid_brief(tmp_path, case, expected):
    brief = _brief("brief-new")
    live = tmp_path / "live" / "latest.json"
    if case == "bad_status":
        _write(live, _latest_doc(brief, status="ERROR"))
    elif case == "bad_generated_at":
        _write(live, _latest_doc(brief, generated_at="not-a-timestamp"))
    elif case == "future_generated_at":
        _write(live, _latest_doc(
            brief, generated_at=(NOW + timedelta(hours=2)).isoformat(),
        ))
    elif case == "future_as_of":
        future_brief = _brief("brief-new", as_of=(NOW + timedelta(hours=1)).isoformat())
        _write(live, _latest_doc(future_brief))
    elif case == "stale":
        _write(live, _latest_doc(
            brief, generated_at=(NOW - timedelta(hours=30)).isoformat(),
        ))
    elif case == "not_record_only":
        doc = _latest_doc(brief)
        doc["record_only"] = False
        _write(live, doc)
    # "missing" intentionally writes nothing.

    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-new", "stale-treatment-hash", rows=[
            _row("US.NVDA", "stale-treatment-hash"),
        ]),
    )
    code, report = _run(tmp_path, latest=live, canary=canary)

    assert code == 2
    assert report["status"] == shadow.STATUS_INVALID
    assert any(expected in item for item in report["validation_errors"])
    assert "current_latest_invalid" in report["reasons"]


def test_invalid_current_brief_record_is_review_invalid(tmp_path):
    brief = _brief("brief-new")
    brief["transmissions"][0]["sign"] = "NEUTRAL"  # not in SIGNS
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(brief))
    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-new", "h", rows=[_row("US.NVDA", "h")]),
    )

    code, report = _run(tmp_path, latest=latest, canary=canary)

    assert code == 2
    assert report["status"] == shadow.STATUS_INVALID
    assert any(
        item.startswith("brief:transmissions[0]:invalid_sign")
        for item in report["validation_errors"]
    )


# ---------------------------------------------------------------------------
# 4. treatment canary 无效
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", "canary_latest_missing"),
        ("unavailable", "canary_brief_unavailable"),
        ("no_injected", "canary_no_injected_symbols"),
        ("inconsistent_hash", "canary_injection_hash_inconsistent"),
        ("missing_hash", "canary_injection_hash_missing"),
        ("partial_missing_hash", "canary_injection_hash_missing"),
    ],
)
def test_invalid_canary_is_review_invalid_brief(tmp_path, case, expected):
    brief = _brief("brief-new")
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(brief))
    canary_path = tmp_path / "canary" / "latest.json"
    if case == "unavailable":
        doc = _canary("brief-new", "h", rows=[_row("US.NVDA", "h")])
        doc["brief_latest"]["available"] = False
        _write(canary_path, doc)
    elif case == "no_injected":
        _write(canary_path, _canary(
            "brief-new", "h", rows=[_row("US.NVDA", "h", injected=False)],
        ))
    elif case == "inconsistent_hash":
        _write(canary_path, _canary("brief-new", "h", rows=[
            _row("US.NVDA", "hash-a"), _row("US.AMD", "hash-b"),
        ]))
    elif case == "missing_hash":
        _write(canary_path, _canary("brief-new", "h", rows=[
            _row("US.NVDA", ""),
        ]))
    elif case == "partial_missing_hash":
        _write(canary_path, _canary("brief-new", "h", rows=[
            _row("US.NVDA", "hash-a"), _row("US.AMD", ""),
        ]))
    # "missing" intentionally writes nothing.

    code, report = _run(tmp_path, latest=latest, canary=canary_path)

    assert code == 2
    assert report["status"] == shadow.STATUS_INVALID
    assert any(expected in item for item in report["validation_errors"])
    assert "canary_treatment_invalid" in report["reasons"]


# ---------------------------------------------------------------------------
# 5. 幂等 / 不可变
# ---------------------------------------------------------------------------


def test_idempotent_rerun_reuses_same_artifact(tmp_path):
    _, latest_path, canary_path, _ = _same_setup(tmp_path)

    code_a, report_a = _run(tmp_path, latest=latest_path, canary=canary_path)
    code_b, report_b = _run(
        tmp_path, latest=latest_path, canary=canary_path,
        now=NOW + timedelta(minutes=10),
    )

    assert code_a == code_b == 0
    assert report_a["content_hash"] == report_b["content_hash"]
    assert report_a["artifact_path"] == report_b["artifact_path"]
    assert report_b["check_count"] == 2
    # latest + exactly one immutable artifact (age is excluded from the hash).
    assert len(list((tmp_path / "out").rglob("*.json"))) == 2


def test_artifact_is_never_overwritten(tmp_path):
    _, latest_path, canary_path, _ = _same_setup(tmp_path)
    _, report = _run(tmp_path, latest=latest_path, canary=canary_path)
    artifact = Path(report["artifact_path"])
    artifact.write_text('{"tampered": true}\n', encoding="utf-8")
    rerun_content = json.loads(
        (tmp_path / "out" / "latest.json").read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="no overwrite"):
        shadow.write_report(
            rerun_content,
            out_root=tmp_path / "out",
            now=NOW,
        )


# ---------------------------------------------------------------------------
# 6. disabled 零副作用
# ---------------------------------------------------------------------------


def test_disabled_has_zero_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(shadow.ENV_FLAG, "0")
    for alias in shadow.ENV_FLAG_ALIASES:
        monkeypatch.setenv(alias, "0")
    out = tmp_path / "out"
    missing_latest = tmp_path / "does-not-exist" / "latest.json"
    missing_canary = tmp_path / "does-not-exist" / "canary.json"

    code = shadow.main([
        "--latest", str(missing_latest),
        "--canary-latest", str(missing_canary),
        "--out-root", str(out),
        "--history-root", str(tmp_path / "history"),
        "--now", NOW.isoformat(),
        "--execute-refresh",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == shadow.STATUS_DISABLED
    assert payload["reason"] == "flag_disabled"
    assert not out.exists()
    assert not (tmp_path / "history").exists()


# ---------------------------------------------------------------------------
# 7. --execute-refresh 双开关闸
# ---------------------------------------------------------------------------


def test_refresh_not_requested_means_zero_paid_calls(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        shadow.subprocess, "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    _, latest_path, canary_path, _ = _same_setup(tmp_path)

    code, report = _run(tmp_path, latest=latest_path, canary=canary_path)

    assert code == 0
    assert calls == []
    assert report["refresh"]["requested"] is False
    assert report["refresh"]["attempted"] is False
    assert report["paid_calls"] == 0


def test_refresh_requires_both_flags(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        shadow.subprocess, "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    _, latest_path, canary_path, _ = _same_setup(tmp_path)

    code, report = _run(
        tmp_path, latest=latest_path, canary=canary_path,
        extra=["--execute-refresh"],
    )

    assert code == 0
    assert calls == []
    assert report["refresh"]["requested"] is True
    assert report["refresh"]["allowed"] is False
    assert report["refresh"]["attempted"] is False
    assert report["refresh"]["reason"] == "requires_flags"


def test_refresh_runs_with_both_flags(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv(shadow.CONTEXT_FLAG, "1")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(shadow.subprocess, "run", fake_run)
    _, latest_path, canary_path, _ = _same_setup(tmp_path)

    code, report = _run(
        tmp_path, latest=latest_path, canary=canary_path,
        extra=["--execute-refresh"],
    )

    assert code == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1].endswith("scripts/da_event_context_shadow.py")
    assert "--allow-no-checkpoint" in command
    assert "--execute" in command
    assert kwargs["cwd"] == str(ROOT)
    assert report["refresh"]["allowed"] is True
    assert report["refresh"]["attempted"] is True
    assert report["refresh"]["returncode"] == 0
    assert report["refresh"]["reason"] == "completed"


def test_refresh_already_current_does_not_overcount_paid_calls(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv(shadow.CONTEXT_FLAG, "1")
    monkeypatch.setattr(
        shadow.subprocess, "run",
        lambda cmd, **kwargs: calls.append(cmd)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    _, latest_path, canary_path, _ = _same_setup(tmp_path)
    doc = json.loads(latest_path.read_text())
    doc["status"] = "ALREADY_CURRENT"
    latest_path.write_text(json.dumps(doc), encoding="utf-8")

    code, report = _run(
        tmp_path, latest=latest_path, canary=canary_path,
        extra=["--execute-refresh"],
    )

    assert code == 0
    assert len(calls) == 1
    assert report["paid_calls"] == 0


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def test_evaluator_ok_carries_age_and_ids(tmp_path):
    _, latest_path, canary_path, _ = _same_setup(tmp_path)
    _, report = _run(tmp_path, latest=latest_path, canary=canary_path)

    evaluated = evaluator.evaluate(tmp_path / "out" / "latest.json", now=NOW)

    assert evaluated["status"] == "OK"
    assert evaluated["writer_status"] == shadow.STATUS_NO_CHANGE
    assert evaluated["age_hours"] == 0.0
    assert evaluated["current_brief_id"] == "brief-1"
    assert evaluated["previous_brief_id"] == "brief-1"
    assert evaluated["report_generated_at"] == report["generated_at"]
    assert evaluated["record_only"] is True


def test_evaluator_reviews_writer_review(tmp_path):
    old_brief = _brief("brief-old")
    old_latest = _write(tmp_path / "old" / "latest.json", _latest_doc(old_brief))
    old_hash = _injection_hash(old_latest)
    new_brief = _brief("brief-new")
    latest = _write(tmp_path / "live" / "latest.json", _latest_doc(new_brief))
    canary = _write(
        tmp_path / "canary" / "latest.json",
        _canary("brief-old", old_hash, rows=[_row("US.NVDA", old_hash)]),
    )
    _, report = _run(tmp_path, latest=latest, canary=canary)

    evaluated = evaluator.evaluate(tmp_path / "out" / "latest.json", now=NOW)

    assert evaluated["status"] == "REVIEW"
    assert evaluated["writer_status"] == shadow.STATUS_REVIEW
    assert evaluated["current_brief_id"] == "brief-new"
    assert evaluated["previous_brief_id"] == "brief-old"


def test_evaluator_no_data_and_errors(tmp_path):
    missing = evaluator.evaluate(tmp_path / "nope.json", now=NOW)
    assert missing["status"] == "NO_DATA"

    future = tmp_path / "future.json"
    _write(future, {
        "schema": shadow.REPORT_SCHEMA,
        "status": shadow.STATUS_NO_CHANGE,
        "generated_at": (NOW + timedelta(hours=2)).isoformat(),
        "current_brief_id": "brief-1",
        "previous_brief_id": "brief-1",
    })
    evaluated_future = evaluator.evaluate(future, now=NOW)
    assert evaluated_future["status"] == "ERROR"
    assert "generated_at_in_future" in evaluated_future["errors"]

    invalid = tmp_path / "invalid.json"
    _write(invalid, {
        "schema": shadow.REPORT_SCHEMA,
        "status": shadow.STATUS_NO_CHANGE,
        "generated_at": "not-a-time",
    })
    evaluated_invalid = evaluator.evaluate(invalid, now=NOW)
    assert evaluated_invalid["status"] == "ERROR"
    assert "generated_at_invalid" in evaluated_invalid["errors"]

    unknown = tmp_path / "unknown.json"
    _write(unknown, {
        "schema": shadow.REPORT_SCHEMA,
        "status": "SOMETHING_ELSE",
        "generated_at": GENERATED_AT,
    })
    evaluated_unknown = evaluator.evaluate(unknown, now=NOW)
    assert evaluated_unknown["status"] == "ERROR"
    assert any(
        "latest_status_invalid" in item for item in evaluated_unknown["errors"]
    )


def test_evaluator_stale_report_is_error(tmp_path):
    stale = tmp_path / "stale.json"
    _write(stale, {
        "schema": shadow.REPORT_SCHEMA,
        "status": shadow.STATUS_NO_CHANGE,
        "generated_at": (NOW - timedelta(hours=48)).isoformat(),
    })

    evaluated = evaluator.evaluate(stale, now=NOW, max_age_hours=36)

    assert evaluated["status"] == "ERROR"
    assert any("latest_stale" in item for item in evaluated["errors"])