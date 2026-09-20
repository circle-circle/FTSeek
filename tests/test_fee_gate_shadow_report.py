import json
from pathlib import Path

import yaml

import scripts.fee_gate_shadow_report as report_mod
from scripts.fee_gate_shadow_report import (
    build_report,
    load_current_gate,
    render_report,
    run_report,
)


def _row(trade_id, price, quantity, pnl_net=-10.0):
    notional = price * quantity
    return {
        "trade_id": trade_id,
        "entry_price": price,
        "quantity": quantity,
        "notional": notional,
        "fees_est": 5.0,
        "pnl_net": pnl_net,
        "entry_time": "2026-09-21T10:00:00-04:00",
    }


def _config(tmp_path, enabled=True, enforce=True):
    path = tmp_path / "unified_config.yaml"
    path.write_text(
        yaml.safe_dump({
            "playbook": {
                "admission": {
                    "enabled": enabled,
                    "enforce": enforce,
                    "min_entry_price": 5.0,
                    "min_notional_usd": 2500.0,
                }
            }
        }),
        encoding="utf-8",
    )
    return path


def _artifacts(tmp_path, rows, excluded_rows=0):
    jsonl = tmp_path / "fee_gate_shadow.jsonl"
    latest = tmp_path / "fee_gate_shadow_latest.json"
    jsonl.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    latest.write_text(
        json.dumps({
            "updated_at": "2026-09-21T02:45:00+00:00",
            "excluded_rows": excluded_rows,
        }),
        encoding="utf-8",
    )
    return jsonl, latest


def test_current_gate_is_loaded_from_production_config_shape(tmp_path):
    gate = load_current_gate(_config(tmp_path))
    assert gate == {
        "enabled": True,
        "enforce": True,
        "min_entry_price": 5.0,
        "min_notional_usd": 2500.0,
    }


def test_report_separates_safe_increment_from_candidate_loosening(tmp_path):
    rows = [
        _row(1, 6.0, 500, -20.0),
        _row(2, 6.0, 834, 30.0),
        _row(3, 2.0, 2500, -50.0),
        _row(4, 6.0, 334, -15.0),
    ]
    jsonl, latest = _artifacts(tmp_path, rows)
    report = build_report(
        jsonl_path=jsonl,
        latest_path=latest,
        config_path=_config(tmp_path),
        now=report_mod.datetime.fromisoformat("2026-09-21T02:47:00+00:00"),
    )
    classification = report["classification"]
    assert classification["safe_incremental_blocked"]["n"] == 3
    assert classification["safe_incremental_kept"]["n"] == 1
    assert classification["safe_incremental_by_floor"]["4000"]["blocked"]["n"] == 3
    assert classification["safe_incremental_by_floor"]["5000"]["blocked"]["n"] == 3
    assert classification["unsafe_loosened"]["n"] == 1
    assert classification["unsafe_loosened_by_floor"]["4000"]["n"] == 1
    assert classification["unsafe_loosened_by_floor"]["5000"]["n"] == 1
    assert classification["current_blocked"]["n"] == 2
    assert report["status"] == "CURRENT_GATE_BYPASS"
    assert report["diagnosis"]["label"] == "SETTLEMENT_FEE_TOXIC / CURRENT_GATE_BYPASS"
    assert "保持现行生产价格门" in report["candidate"]["safe_semantics"]
    assert report["candidate"]["notional_floors"] == [4000.0, 5000.0]


def test_report_marks_small_sample_before_recommending_adjustment(tmp_path):
    rows = [_row(1, 6.0, 500), _row(2, 6.0, 834, 30.0)]
    jsonl, latest = _artifacts(tmp_path, rows)
    report = build_report(
        jsonl_path=jsonl,
        latest_path=latest,
        config_path=_config(tmp_path),
        now=report_mod.datetime.fromisoformat("2026-09-21T02:47:00+00:00"),
    )
    assert report["classification"]["current_blocked"]["n"] == 0
    assert report["classification"]["safe_incremental_blocked"]["n"] == 1
    assert report["status"] == "INSUFFICIENT_N"
    assert report["diagnosis"]["label"] == "INSUFFICIENT_N"


def test_report_flags_disabled_current_gate(tmp_path):
    rows = [_row(1, 6.0, 834)]
    jsonl, latest = _artifacts(tmp_path, rows)
    report = build_report(
        jsonl_path=jsonl,
        latest_path=latest,
        config_path=_config(tmp_path, enforce=False),
        now=report_mod.datetime.fromisoformat("2026-09-21T02:47:00+00:00"),
    )
    assert report["status"] == "CURRENT_GATE_NOT_ENFORCED"
    assert "inactive" in render_report(report)


def test_report_flags_stale_or_missing_chain(tmp_path):
    report = build_report(
        jsonl_path=tmp_path / "missing.jsonl",
        latest_path=tmp_path / "missing.json",
        config_path=_config(tmp_path),
    )
    assert report["status"] == "CHAIN_BROKEN"


class _Hub:
    def __init__(self):
        self.messages = []

    def push(self, message, sync=False):
        self.messages.append((message, sync))
        return True


def test_push_dispatch_is_stateful_and_uses_push_hub_sync(monkeypatch, tmp_path):
    rows = [_row(1, 6.0, 834)]
    jsonl, latest = _artifacts(tmp_path, rows)
    state = tmp_path / "push_state.json"
    hub = _Hub()
    monkeypatch.setattr(report_mod, "get_push_hub", lambda: hub)

    first = run_report(
        jsonl_path=jsonl,
        latest_path=latest,
        config_path=_config(tmp_path),
        state_path=state,
        push=True,
    )
    second = run_report(
        jsonl_path=jsonl,
        latest_path=latest,
        config_path=_config(tmp_path),
        state_path=state,
        push=True,
    )

    assert first["pushed"] is True
    assert second["pushed"] is False
    assert len(hub.messages) == 1
    message, sync = hub.messages[0]
    assert sync is True
    assert message.category.value == "shadow_compare"
    assert "T3 费用门影子诊断" in message.title
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["last_n"] == 1
    assert saved["last_status"] == first["report"]["status"]


def test_weekly_checkpoint_reads_fee_gate_shadow_section():
    source = (
        Path(__file__).resolve().parents[1]
        / "ops/hermes/ftseek_weekly_checkpoint.py"
    ).read_text(encoding="utf-8")
    assert "def check_fee_gate_shadow():" in source
    assert "check_fee_gate_shadow()" in source
