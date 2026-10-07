"""DA bridge 推送候选/可交易口径契约测试。"""

import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "da_bridge_dingtalk_push.py"
SPEC = importlib.util.spec_from_file_location("da_bridge_dingtalk_push", SCRIPT)
PUSH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUSH)


def _write_instructions(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE playbook_instructions ("
        "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, "
        "skip_reason TEXT, decision_id TEXT)"
    )
    rows = [
        ("US.PENG", "SKIP", "方向偏斜超限 guardrail 降级"),
        ("US.CEG", "BUY", ""),
        ("US.NOK", "SKIP", "方向偏斜超限 guardrail 降级"),
        ("US.BKV", "BUY", ""),
        ("US.MRVL", "SKIP", "sector 集中度 guardrail 降级"),
    ]
    for index, (symbol, direction, skip_reason) in enumerate(rows, 1):
        connection.execute(
            "INSERT INTO playbook_instructions VALUES (?, ?, ?, ?, ?, ?)",
            (index, "2026-10-07", symbol, direction, skip_reason,
             f"DA-20261007-{index}"),
        )
    connection.commit()
    connection.close()


def _bridge():
    symbols = ["US.PENG", "US.CEG", "US.NOK", "US.BKV", "US.MRVL"]
    return {
        "trade_date": "2026-10-07",
        "candidates": [
            {"symbol": symbol, "direction": "LONG", "confidence": 0.5,
             "entry_price": 10, "stop_loss": 9, "debate_outcome": "ok"}
            for symbol in symbols
        ],
    }


def test_instruction_summary_separates_candidates_and_executable_rows(tmp_path):
    db_path = tmp_path / "ftseek_us.db"
    _write_instructions(db_path)
    summary = PUSH._instruction_summary("2026-10-07", db_path=db_path)
    assert summary["status"] == "ok"
    assert summary["instruction_count"] == 5
    assert summary["executable_count"] == 2
    assert summary["skipped_count"] == 3


def test_message_states_bridge_candidates_and_db_executable_count(
        tmp_path, monkeypatch):
    db_path = tmp_path / "ftseek_us.db"
    _write_instructions(db_path)
    monkeypatch.setattr(PUSH, "_finnhub_price", lambda symbol: 0.0)
    monkeypatch.setattr(PUSH, "_health_section", lambda date: "")
    monkeypatch.setattr(PUSH, "DB_PATH", db_path)
    message = PUSH._build_message("2026-10-07", _bridge())
    assert "候选: 5只(动态扫描+熟股)" in message
    assert "DB可交易: 2/5" in message
    assert "guardrail SKIP: 3" in message
    assert "US.CEG: LONG" in message
    assert "DB状态: BUY ✅可交易" in message
    assert "DB状态: SKIP ⚠️SKIP — 方向偏斜超限 guardrail 降级" in message
    assert "仅 BUY/SELL_SHORT 进入执行" in message


def test_summary_record_is_persisted(tmp_path):
    db_path = tmp_path / "ftseek_us.db"
    _write_instructions(db_path)
    summary = PUSH._instruction_summary("2026-10-07", db_path=db_path)
    record_path = tmp_path / "record.jsonl"
    PUSH._append_summary_record(
        "2026-10-07", _bridge(), summary, "pushed", path=record_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["type"] == "da_push_instruction_summary"
    assert record["bridge_candidate_count"] == 5
    assert record["db_instruction_count"] == 5
    assert record["executable_count"] == 2
    assert record["skipped_count"] == 3
    assert len(record["symbols"]) == 5
