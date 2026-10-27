from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "da_scope_confidence_audit", ROOT / "scripts/da_scope_confidence_audit.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def _candidate(batch_id, symbol, day, stage="da_main_pool", selected=True):
    return {
        "batch_id": batch_id,
        "symbol": symbol,
        "observed_at": f"{day}T08:00:00-04:00",
        "selection_status": "selected" if selected else "budget_cut",
        "stage": stage,
        "status": "UNKNOWN",
        "reasons": [],
    }


def _batch(batch_id, day, symbols):
    return {
        "phase": "da_selection",
        "batch_id": batch_id,
        "policy_version": "test-v1",
        "candidates": [_candidate(batch_id, symbol, day) for symbol in symbols],
    }


def _write_batches(path, batches):
    path.write_text("".join(json.dumps(batch) + "\n" for batch in batches), encoding="utf-8")


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
            confidence INTEGER, decision_id TEXT, raw_instruction_json TEXT,
            skip_reason TEXT, executed INTEGER, execution_state TEXT, model_used TEXT
        );
        CREATE TABLE trade_records (decision_id TEXT, trade_date TEXT);
    """)
    conn.close()


def _instruction(conn, symbol="US.AMD", published="SELL_SHORT", current="SKIP"):
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (1,'2026-10-26',?,?,35,?,?,?,0,?,?)",
        (symbol, current, f"DA-test-{symbol}", json.dumps({"direction": published}),
         "entry_confirm_reversed", "skipped", "deterministic"))
    conn.commit()


def test_funnel_explicit_date_is_stable_and_default_is_latest(tmp_path, monkeypatch, capsys):
    path = tmp_path / "funnel.jsonl"
    _write_batches(path, [_batch("b1", "2026-10-23", ["US.AAA"]),
                          _batch("b2", "2026-10-26", ["US.BBB"])])
    monkeypatch.setattr(AUDIT, "FUNNEL", str(path))

    AUDIT.cmd_funnel(SimpleNamespace(date="2026-10-23", batch_id=""))
    assert "batch=b1 date=2026-10-23" in capsys.readouterr().out
    AUDIT.cmd_funnel(SimpleNamespace(date="", batch_id=""))
    assert "batch=b2 date=2026-10-26" in capsys.readouterr().out


def test_funnel_same_day_requires_batch_identity(tmp_path, monkeypatch):
    path = tmp_path / "funnel.jsonl"
    _write_batches(path, [_batch("b1", "2026-10-26", ["US.AAA"]),
                          _batch("b2", "2026-10-26", ["US.BBB"])])
    monkeypatch.setattr(AUDIT, "FUNNEL", str(path))
    with pytest.raises(ValueError, match="pass --batch-id"):
        AUDIT.cmd_funnel(SimpleNamespace(date="2026-10-26", batch_id=""))
    assert AUDIT._funnel_batches(batch_id="b1")["batch_id"] == "b1"


def test_funnel_empty_batch_can_be_selected_by_identity(tmp_path, monkeypatch, capsys):
    path = tmp_path / "funnel.jsonl"
    _write_batches(path, [{"phase": "da_selection", "batch_id": "empty", "candidates": []}])
    monkeypatch.setattr(AUDIT, "FUNNEL", str(path))
    AUDIT.cmd_funnel(SimpleNamespace(date="", batch_id="empty"))
    out = capsys.readouterr().out
    assert "date=unknown" in out
    assert "rows=0 unique_symbols=0" in out


def test_debate_uses_decision_source_and_exposes_populations(tmp_path, monkeypatch, capsys):
    checkpoint = tmp_path / "checkpoints/2026-10-26"
    checkpoint.mkdir(parents=True)
    for symbol, action, decision in (("US.AAA", "BUY", "WARN"),
                                     ("US.BBB", "BUY", "BLOCK"),
                                     ("US.CCC", "SKIP", "PASS")):
        (checkpoint / f"{symbol}.json").write_text(json.dumps({
            "symbol": symbol, "action": action,
            "_signal_gate": {"decision": decision, "decision_conf": 0.5},
            "_llm_output": {"direction": action, "_debate": {}},
        }), encoding="utf-8")
    bridge = {"candidates": [{"symbol": "US.AAA", "decision_id": "DA-test-US.AAA",
                              "confidence_semantic": 0.5, "confidence": 0.35}]}
    (tmp_path / "data/opportunity_scan_2026-10-26.json").parent.mkdir(parents=True)
    (tmp_path / "data/opportunity_scan_2026-10-26.json").write_text(
        json.dumps(bridge), encoding="utf-8")
    db_path = tmp_path / "test.db"
    _db(db_path)
    conn = sqlite3.connect(db_path)
    _instruction(conn, "US.AAA")
    conn.execute("INSERT INTO trade_records VALUES ('DA-test-US.AAA','2026-10-26')")
    conn.commit()
    monkeypatch.setattr(AUDIT, "CKPT", str(tmp_path / "checkpoints"))
    monkeypatch.setattr(AUDIT, "DB", str(db_path))
    monkeypatch.setattr(AUDIT, "REPO", str(tmp_path))
    monkeypatch.setattr(AUDIT, "_classify", lambda *_: "PARTIAL_AGREE")

    AUDIT.cmd_debate(SimpleNamespace(date="2026-10-26", since=""))
    out = capsys.readouterr().out
    assert "analysis=3 proposed=2 gate_publishable=1 db_da_rows=1 filled_trades=1" in out
    assert "semantic=0.5 warn_adjustment=-0.15 final=0.35" in out


def test_confgate_preserves_publication_and_current_states(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _db(db_path)
    conn = sqlite3.connect(db_path)
    _instruction(conn)
    log = tmp_path / "run.log"
    log.write_text("[playbook] US.AMD SELL_SHORT confidence=35 < 门槛=55 → SKIP\n" * 2,
                   encoding="utf-8")
    monkeypatch.setattr(AUDIT, "DB", str(db_path))
    AUDIT.cmd_confgate(SimpleNamespace(date="2026-10-26", log=str(log)))
    out = capsys.readouterr().out
    assert "conf_gate_events=2" in out
    assert "published=SELL_SHORT current=SKIP" in out
    assert "log_join=symbol_unique filled=0" in out


def test_confgate_marks_same_symbol_rows_ambiguous(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _db(db_path)
    conn = sqlite3.connect(db_path)
    _instruction(conn)
    conn.execute("UPDATE playbook_instructions SET id=2")
    _instruction(conn)
    log = tmp_path / "run.log"
    log.write_text("[playbook] US.AMD SELL_SHORT confidence=35 < 门槛=55 → SKIP\n",
                   encoding="utf-8")
    monkeypatch.setattr(AUDIT, "DB", str(db_path))
    AUDIT.cmd_confgate(SimpleNamespace(date="2026-10-26", log=str(log)))
    assert "gate_events=ambiguous refused_events=ambiguous" in capsys.readouterr().out


def test_overlap_separates_retention_jaccard_and_window(tmp_path, monkeypatch, capsys):
    path = tmp_path / "funnel.jsonl"
    _write_batches(path, [_batch("b1", "2026-10-23", ["US.AAA", "US.BBB", "US.CCC"]),
                          _batch("b2", "2026-10-26", ["US.AAA", "US.BBB", "US.DDD"]),
                          _batch("b3", "2026-10-27", ["US.XXX"])])
    monkeypatch.setattr(AUDIT, "FUNNEL", str(path))
    monkeypatch.setattr(AUDIT, "CKPT", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    AUDIT.cmd_overlap(SimpleNamespace(since="2026-10-23", date="2026-10-26"))
    out = capsys.readouterr().out
    assert "0.67 | 0.50 | 2/3" in out
    assert "pairs=1 mean_retention=0.67 mean_jaccard=0.50" in out


def test_overlap_does_not_silently_merge_same_day_batches(tmp_path, monkeypatch, capsys):
    path = tmp_path / "funnel.jsonl"
    _write_batches(path, [_batch("b1", "2026-10-26", ["US.AAA"]),
                          _batch("b2", "2026-10-26", ["US.BBB"])])
    monkeypatch.setattr(AUDIT, "FUNNEL", str(path))
    monkeypatch.setattr(AUDIT, "CKPT", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    AUDIT.cmd_overlap(SimpleNamespace(since="", date=""))
    out = capsys.readouterr().out
    assert "2026-10-26 | 2 | unknown" in out
    assert "pairs=0" in out


def test_arbitration_reports_reason_and_distinct_kept_instruction(tmp_path, monkeypatch, capsys):
    shadow = tmp_path / "data/shadow"
    shadow.mkdir(parents=True)
    event = {
        "date": "2026-10-26", "symbol": "US.AMD", "reason": "empty_did_vs_sourced",
        "stage": "save_instruction", "write_outcome": "rejected_kept_existing",
        "attempted": {"direction": "SKIP", "confidence": 35,
                      "skip_reason": "SELL_SHORT confidence 35 < 55"},
        "kept": {"id": 42, "did": "DA-test", "direction": "SELL_SHORT"},
    }
    (shadow / "instruction_arbitration.jsonl").write_text(
        json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setattr(AUDIT, "REPO", str(tmp_path))
    AUDIT.cmd_arbitration(SimpleNamespace(date="2026-10-26"))
    out = capsys.readouterr().out
    assert "rows=2 unique_kept_instructions=1" in out
    assert "outcome=rejected_kept_existing" in out
    assert "skip=SELL_SHORT confidence 35 < 55" in out
