"""DA bridge 推送候选/可交易口径契约测试。"""

import importlib.util
import json
import sqlite3
from datetime import datetime
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


def test_instruction_summary_includes_ue_rows(tmp_path):
    """[DEB-20261008 F5] 推送口径不得漏 UE 行 — universe_expansion: 前缀的指令
    (含 SELL_SHORT) 必须计入, 否则推送在方向上系统性失真 (实测漏 9 条/5 空)。"""
    db_path = tmp_path / "ftseek_us.db"
    _write_instructions(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO playbook_instructions VALUES (?, ?, ?, ?, ?, ?)",
        (99, "2026-10-07", "US.CORT", "SELL_SHORT", "",
         "universe_expansion:2026-10-07:CORT"),
    )
    connection.commit()
    connection.close()
    summary = PUSH._instruction_summary("2026-10-07", db_path=db_path)
    assert summary["instruction_count"] == 6
    assert summary["executable_count"] == 3
    ue_rows = [r for r in summary["rows"] if r["symbol"] == "US.CORT"]
    assert ue_rows and ue_rows[0]["direction"] == "SELL_SHORT"


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


def test_push_window_boundaries():
    # 2026-10-16: 窗尾 09:26→09:29 (合并 UE fresh-event 内容版, 死线 09:28)
    def at(hour, minute):
        return PUSH.ET.localize(datetime(2026, 10, 7, hour, minute))

    assert not PUSH._in_push_window(at(8, 49))
    assert PUSH._in_push_window(at(8, 50))
    assert PUSH._in_push_window(at(9, 28))
    assert not PUSH._in_push_window(at(9, 29))


def test_outside_window_records_skip_and_does_not_push(tmp_path, monkeypatch):
    monkeypatch.setattr(PUSH, "DATA", tmp_path)
    (tmp_path / "opportunity_scan_2026-10-07.json").write_text(
        json.dumps(_bridge()), encoding="utf-8")
    monkeypatch.setattr(
        PUSH, "_now_et",
        lambda: PUSH.ET.localize(datetime(2026, 10, 7, 7, 55)))
    monkeypatch.setattr(PUSH, "_build_message", lambda date, bridge: "message")
    monkeypatch.setattr(PUSH, "_instruction_summary", lambda date: {"status": "ok"})
    pushes = []
    monkeypatch.setattr("app.push.push_text_safe", lambda *args, **kwargs: pushes.append(args))

    PUSH.main(date="2026-10-07")

    assert pushes == []
    record = json.loads(
        (tmp_path / "da_push_instruction_summary.jsonl").read_text(encoding="utf-8"))
    assert record["status"] == "skipped"
    assert record["reason"] == "outside_window"


def test_missing_bridge_records_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(PUSH, "DATA", tmp_path)
    monkeypatch.setattr(
        PUSH, "_now_et",
        lambda: PUSH.ET.localize(datetime(2026, 10, 7, 8, 55)))

    PUSH.main(date="2026-10-07")

    record = json.loads(
        (tmp_path / "da_push_instruction_summary.jsonl").read_text(encoding="utf-8"))
    assert record["status"] == "skipped"
    assert record["reason"] == "bridge_missing"


def test_successful_push_is_once_per_date(tmp_path, monkeypatch):
    monkeypatch.setattr(PUSH, "DATA", tmp_path)
    (tmp_path / "opportunity_scan_2026-10-07.json").write_text(
        json.dumps(_bridge()), encoding="utf-8")
    monkeypatch.setattr(
        PUSH, "_now_et",
        lambda: PUSH.ET.localize(datetime(2026, 10, 7, 8, 55)))
    monkeypatch.setattr(PUSH, "_build_message", lambda date, bridge: "message")
    monkeypatch.setattr(
        PUSH, "_instruction_summary",
        lambda date: {"status": "ok", "instruction_count": 5})
    pushes = []
    monkeypatch.setattr("app.push.push_text_safe", lambda *args, **kwargs: pushes.append(args))

    PUSH.main(date="2026-10-07")
    PUSH.main(date="2026-10-07")

    assert len(pushes) == 1
    records = [
        json.loads(line)
        for line in
        (tmp_path / "da_push_instruction_summary.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in records] == ["pushed", "skipped"]
    assert records[1]["reason"] == "already_pushed"


def test_wait_fresh_event_done_when_batch_complete(tmp_path, monkeypatch):
    """批次终态行在册 ⇒ 立即返回不等待 (2026-10-16 用户裁延后推送)。"""
    ledger = tmp_path / "ue_fresh_da_candidate_pool.jsonl"
    ledger.write_text(json.dumps(
        {"type": "fresh_event_batch", "date": "2026-10-16", "mode": "execute",
         "status": "complete"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(PUSH, "FRESH_EVENT_LEDGER", ledger)
    assert PUSH._fresh_event_terminal_today("2026-10-16") is True
    assert PUSH._fresh_event_terminal_today("2026-10-17") is False
    assert PUSH._wait_fresh_event_done("2026-10-16") == "batch_done"


def test_wait_fresh_event_deadline_proceeds(tmp_path, monkeypatch):
    """批次无终态行 + 过死线 ⇒ 照推 (桥接内容是主面, 不被批次故障拖死)。"""
    monkeypatch.setattr(PUSH, "FRESH_EVENT_LEDGER", tmp_path / "absent.jsonl")
    monkeypatch.setattr(PUSH, "_now_et",
                        lambda: PUSH.ET.localize(datetime(2026, 10, 16, 9, 29)))
    assert PUSH._wait_fresh_event_done("2026-10-16") == "deadline_proceed_without_batch"


def test_wait_fresh_event_skip_is_not_terminal(tmp_path, monkeypatch):
    """skipped_* 行不算终态 (2026-10-16 巡检: EST 季错季行 08:10 写 skip,
    误认终态会让 09:12 推送读到它而漏掉 09:10 才发车的真批次)。"""
    ledger = tmp_path / "ue_fresh_da_candidate_pool.jsonl"
    ledger.write_text(json.dumps(
        {"type": "fresh_event_batch", "date": "2026-10-16", "mode": "execute",
         "status": "skipped_outside_window"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(PUSH, "FRESH_EVENT_LEDGER", ledger)
    assert PUSH._fresh_event_terminal_today("2026-10-16") is False
    # 追加真终态行 ⇒ 才认
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "fresh_event_batch", "date": "2026-10-16",
                            "mode": "execute", "status": "complete"}) + "\n")
    assert PUSH._fresh_event_terminal_today("2026-10-16") is True
