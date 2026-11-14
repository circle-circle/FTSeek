from __future__ import annotations

import json

from scripts.da_repeatability_probe import (
    _flatten_runs,
    main,
    summarize_paired,
    summarize_runs,
)


def make_run(index, *, arm, direction, score, event_role="NONE", event_ids=None, sha=None, status="VALID"):
    return {
        "run": index,
        "arm": arm,
        "direction": direction,
        "score": score,
        "status": status,
        "event_role": event_role,
        "event_ids": event_ids or [],
        "price_in": {"status": "UNKNOWN", "evidence": ""},
        "prompt_sha256": sha or "a" * 64,
        "prompt_tokens_est": 100,
        "model_reported": "qwen3.7-max",
        "challenger_model": "deepseek-v4.1-flash",
        "llm_calls": 2,
        "validation_errors": [],
    }


def test_summarize_runs_reports_direction_and_score_noise():
    runs = [
        make_run(1, arm="control", direction="SELL_SHORT", score=6.0),
        make_run(2, arm="control", direction="SELL_SHORT", score=8.0),
        make_run(3, arm="control", direction="SKIP", score=6.0),
    ]
    summary = summarize_runs(runs, arm="control", symbol="US.AMD")
    assert summary["direction_counts"] == {"SELL_SHORT": 2, "SKIP": 1}
    assert summary["score_values"] == [6.0, 8.0]
    assert summary["stable_direction"] is False
    assert summary["stable_score"] is False
    assert summary["identical_prompts"] is True
    assert summary["valid_cards"] == 3


def test_summarize_paired_counts_direction_changes_by_run_index():
    control = [
        make_run(1, arm="control", direction="BUY", score=6.0),
        make_run(2, arm="control", direction="SKIP", score=6.0),
        make_run(3, arm="control", direction="BUY", score=6.0),
    ]
    treatment = [
        make_run(1, arm="treatment", direction="SKIP", score=6.0,
                 event_role="PRIMARY", event_ids=["e1"]),
        make_run(2, arm="treatment", direction="SKIP", score=6.0),
        make_run(3, arm="treatment", direction="BUY", score=5.0,
                 event_role="CONFIRM", event_ids=["e2"]),
    ]
    summary = summarize_paired(control, treatment, symbol="US.NVDA")
    assert summary["paired_run_count"] == 3
    assert summary["direction_changes"] == 1
    assert summary["score_changes"] == 1
    assert summary["treatment_runs_citing_events"] == 2
    assert [row["treatment_event_role"] for row in summary["pairs"]] == [
        "PRIMARY", "NONE", "CONFIRM",
    ]


def test_flatten_runs_returns_per_arm_records():
    runs = [make_run(1, arm="treatment", direction="SKIP", score=6.0)]
    result = {"symbol": "US.NVDA", "arms": {}, "runs": {"treatment": runs}}
    assert _flatten_runs(result, "treatment") == runs
    assert _flatten_runs(result, "control") == []


def test_main_paired_mode_writes_summary_without_model_calls(tmp_path, monkeypatch):
    import scripts.da_repeatability_probe as probe

    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    (raw_root / "US.NVDA.raw.json").write_text(
        json.dumps({"trade_date": "2026-11-12", "collected": {}}), encoding="utf-8",
    )
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps({"brief_id": "brief-1", "validation_errors": []}), encoding="utf-8")
    calls = []

    def fake_run_arm(collected, *, symbol, session_date, valid_until, brief, arm):
        calls.append((symbol, arm, (brief or {}).get("brief_id")))
        return {
            "card": {
                "direction": "SKIP", "score": 6.0, "status": "VALID",
                "event_role": "NONE", "event_ids": [], "price_in": {},
            },
            "result": {"_model_reported": "qwen3.7-max"},
            "challenger_raw": {"_model_reported": "deepseek-v4.1-flash"},
            "calls": [{}],
            "user_prompt": f"{symbol}:{arm}",
        }

    monkeypatch.setattr(probe, "run_arm", fake_run_arm)
    output = tmp_path / "out"
    rc = main([
        "--raw-root", str(raw_root),
        "--brief", str(brief_path),
        "--symbols", "NVDA",
        "--arm", "paired",
        "--runs", "2",
        "--session-date", "2026-11-12",
        "--valid-until", "2026-11-12T16:00:00-05:00",
        "--output-dir", str(output),
        "--execute",
    ])
    assert rc == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["total_llm_calls"] == 8
    assert summary["results"]["NVDA"]["paired"]["paired_run_count"] == 2
    assert [row[1] for row in calls] == ["control", "treatment", "control", "treatment"]
    assert summary["results"]["NVDA"]["runs"]["treatment"][0]["arm"] == "treatment"