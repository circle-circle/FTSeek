"""Contract tests for analyst-versus-challenger true reverse telemetry."""

import json

from app.playbook.true_reverse_shadow import (
    append_events,
    build_true_reverse_event,
    classify_true_reverse,
)


def _analysis(**overrides):
    payload = {
        "direction": "SELL_SHORT",
        "score": 8.0,
        "confidence": "中低",
        "debate_outcome": "⚠️ 2模型真反向(SELL_SHORT vs BUY), score接近→降confidence",
        "_model_reported": "qwen3.7-max",
    }
    payload.update(overrides)
    return payload


def _debate(**overrides):
    payload = {
        "challenger_model": "deepseek-v4.1-flash",
        "challenger_tier": "primary",
        "challenger_self_debate": False,
        "challenger_direction": "BUY",
        "challenger_independent_direction": "BUY",
        "challenger_score": 6.0,
        "challenger_verdict": "DISAGREE",
        "challenger_skip_reason": "independent challenger reasoning",
        "falsification_target": "loss mechanism",
        "flip_condition": "flip condition",
    }
    payload.update(overrides)
    return payload


class TestClassification:
    def test_independent_true_reverse(self):
        result = classify_true_reverse(_analysis(), _debate())
        assert result["classification"] == "independent_true_reverse"
        assert result["independent_confirms_reverse"] is True
        assert result["score_gap"] == 2.0
        assert result["proposed_resolution"] == "no_trade_pending_independent_adjudication"

    def test_score_gap_gt_3_proposes_skip(self):
        result = classify_true_reverse(
            _analysis(score=8.0), _debate(challenger_score=4.0)
        )
        assert result["proposed_resolution"] == "skip_if_score_gap_gt_3"

    def test_skip_is_abstention_not_reverse(self):
        assert classify_true_reverse(_analysis(), _debate(challenger_direction="SKIP")) is None

    def test_same_direction_is_not_reverse(self):
        assert classify_true_reverse(_analysis(), _debate(challenger_direction="SELL_SHORT")) is None

    def test_self_debate_is_not_independent(self):
        result = classify_true_reverse(
            _analysis(), _debate(challenger_self_debate=True)
        )
        assert result["classification"] == "anchored_or_non_independent_reverse"
        assert result["independent_confirms_reverse"] is False


class TestEvent:
    def test_event_preserves_counter_evidence(self):
        event = build_true_reverse_event(
            "2026-09-23", "US.TSLA", _analysis(), _debate(), "artifact.json"
        )
        assert event["event_id"].startswith("DA-TRUE-REVERSE:")
        assert event["analyst"]["direction"] == "SELL_SHORT"
        assert event["challenger"]["direction"] == "BUY"
        assert event["challenger"]["reasoning"] == "independent challenger reasoning"
        assert event["challenger"]["falsification_target"] == "loss mechanism"
        assert event["challenger"]["flip_condition"] == "flip condition"

    def test_append_events_is_idempotent(self, tmp_path):
        path = tmp_path / "shadow" / "true_reverse.jsonl"
        event = build_true_reverse_event(
            "2026-09-23", "US.TSLA", _analysis(), _debate(), "artifact.json"
        )
        assert append_events([event, event], path) == (1, 1)
        assert append_events([event], path) == (0, 1)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1


class TestCheckpointScan:
    def test_scan_captures_unselected_true_reverse(self, tmp_path):
        from scripts.da_true_reverse_shadow import scan_checkpoints

        checkpoint_root = tmp_path / "da_checkpoint"
        checkpoint_dir = checkpoint_root / "2026-09-23"
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / "US.TSLA.json").write_text(
            json.dumps({
                "symbol": "US.TSLA",
                "_llm_output": {
                    **_analysis(),
                    "_debate": _debate(),
                },
            }),
            encoding="utf-8",
        )
        output = tmp_path / "true_reverse.jsonl"
        events = scan_checkpoints(checkpoint_root, "2026-09-01")
        assert append_events(events, output) == (1, 0)
        record = json.loads(output.read_text(encoding="utf-8"))
        assert record["symbol"] == "US.TSLA"
        assert record["classification"] == "independent_true_reverse"
