import json

from scripts import ue_long_gate_observation as observation


def test_summarize_tri_state_log(tmp_path):
    path = tmp_path / "ue_long_momentum_gate.jsonl"
    rows = [
        {"date": "2026-09-28", "symbol": "RIVN", "decision": "blocked", "trend": "空头排列"},
        {"date": "2026-09-29", "symbol": "UBER", "decision": "passed", "trend": "短期偏多"},
        {"date": "2026-09-29", "symbol": "NOK", "decision": "no_data", "trend": "no_data"},
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n{bad",
        encoding="utf-8",
    )
    report = observation.summarize(path)
    assert report["valid_rows"] == 3
    assert report["raw_rows"] == 3
    assert report["dup_rows"] == 0
    assert report["invalid_rows"] == 1
    assert report["by_decision"] == {"blocked": 1, "passed": 1, "no_data": 1}
    assert report["by_date"][1]["by_decision"]["passed"] == 1


def test_summarize_dedups_rerun_rows_keep_last(tmp_path):
    """inject --dry/幂等重跑会对同 (date, symbol) 重复落行 — 去重保留最后判定。"""
    path = tmp_path / "ue_long_momentum_gate.jsonl"
    rows = [
        {"date": "2026-09-29", "symbol": "UBER", "decision": "passed", "trend": "短期偏多"},
        {"date": "2026-09-29", "symbol": "RIVN", "decision": "blocked", "trend": "空头排列"},
        # 同日同标的重跑行 (数据面变化 → 判定翻转): 后者覆盖前者
        {"date": "2026-09-29", "symbol": "UBER", "decision": "blocked", "trend": "空头排列"},
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    report = observation.summarize(path)
    assert report["raw_rows"] == 3
    assert report["valid_rows"] == 2
    assert report["dup_rows"] == 1
    assert report["by_decision"] == {"blocked": 2, "passed": 0, "no_data": 0}
    assert report["by_date"] == [
        {"date": "2026-09-29", "rows": 2,
         "by_decision": {"blocked": 2, "passed": 0, "no_data": 0}},
    ]


def test_summarize_accepts_since_boundary(tmp_path):
    path = tmp_path / "ue_long_momentum_gate.jsonl"
    rows = [
        {"date": "2026-09-28", "symbol": "NOK", "decision": "passed", "trend": "短期偏多"},
        {"date": "2026-09-29", "symbol": "UBER", "decision": "blocked", "trend": "空头排列"},
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    report = observation.summarize(path, since="2026-09-29")
    assert report["since"] == "2026-09-29"
    assert report["valid_rows"] == 1
    assert report["by_decision"]["blocked"] == 1


def test_cli_json_output(tmp_path, capsys, monkeypatch):
    path = tmp_path / "ue_long_momentum_gate.jsonl"
    path.write_text(
        json.dumps({"date": "2026-09-29", "decision": "blocked", "trend": "空头排列"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(observation, "_DEFAULT_LOG_PATH", path)
    assert observation.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["by_decision"]["blocked"] == 1
