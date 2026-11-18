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
        CREATE TABLE trade_records (decision_id TEXT, trade_date TEXT, symbol TEXT);
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
    conn.execute("INSERT INTO trade_records VALUES ('DA-test-US.AAA','2026-10-26','US.AAA')")
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


def test_confgate_shared_decision_id_is_scoped_by_symbol(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _db(db_path)
    conn = sqlite3.connect(db_path)
    _instruction(conn, "US.AAA")
    conn.execute("UPDATE playbook_instructions SET id=2")
    _instruction(conn, "US.BBB")
    conn.execute("UPDATE playbook_instructions SET decision_id='DA-shared'")
    conn.execute("INSERT INTO trade_records VALUES ('DA-shared','2026-10-26','US.AAA')")
    conn.execute("INSERT INTO trade_records VALUES ('DA-shared','2026-10-26','US.BBB')")
    conn.commit()
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(AUDIT, "DB", str(db_path))
    AUDIT.cmd_confgate(SimpleNamespace(date="2026-10-26", log=str(log)))
    out = capsys.readouterr().out
    assert out.count("filled=1") == 2
    assert "filled=2" not in out


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


def _first_night_case(tmp_path, *, model="qwen4-max", status="complete",
                      finished="2026-11-17T08:31:00-05:00", contract=None,
                      degraded=False, log_text="ok\n"):
    day = "2026-11-17"
    checkpoint = tmp_path / "checkpoints" / day
    checkpoint.mkdir(parents=True, exist_ok=True)
    llm = {
        "direction": "BUY", "executive_summary": "正常分析", "risks": [],
        "_model_reported": model, "_llm_latency_ms": 400000,
        "_finish_reason": "stop",
        "_usage": {"prompt_tokens": 19000,
                   "completion_tokens_details": {"reasoning_tokens": 15000}},
        "_contract": contract or {"violations": []},
        "_debate": {"challenger_tier": "primary", "challenger_primary_error": None},
    }
    if degraded:
        llm["executive_summary"] = "LLM分析降级，数据已采集但合成失败"
    (checkpoint / "US.AMD.json").write_text(json.dumps({
        "symbol": "US.AMD", "_ckpt_model": model, "_llm_output": llm,
    }), encoding="utf-8")
    schedule = tmp_path / "schedule.jsonl"
    schedule.write_text(json.dumps({
        "date": day, "status": status, "finished_at": finished,
        "analyses_count": 1,
        "batch_observation": {"state": "complete", "analysed_count": 1,
                              "missing_symbols": [], "unreadable_checkpoints": [],
                              "unexpected_symbols": [], "gate_unknown_symbols": []},
        "artifacts": {"ok": True},
    }) + "\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True, exist_ok=True)
    (repo / "data" / f"opportunity_scan_{day}.json").write_text(json.dumps({
        "model_used": model, "candidates": [{"symbol": "US.AMD"}],
    }), encoding="utf-8")
    db = tmp_path / "first.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE playbook_instructions (date TEXT, decision_id TEXT, model_used TEXT)")
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?)", (day, "DA-x", model))
    conn.commit()
    conn.close()
    log = tmp_path / "da.log"
    log.write_text(log_text, encoding="utf-8")
    return {
        "date": day, "expected_model": "qwen4-max", "thinking_budget": 0,
        "log_path": log, "ckpt_root": tmp_path / "checkpoints",
        "schedule_path": schedule, "db_path": db, "repo": repo,
    }


def test_first_night_passes_uncapped_complete_batch(tmp_path):
    report = AUDIT.first_night_report(**_first_night_case(tmp_path))
    assert report["status"] == "PASS"
    assert report["cap_check"] == "not_applicable"
    assert report["model_counts"] == {"qwen4-max": 1}
    assert report["latency_ms"]["max"] == 400000.0
    assert report["quality_or_edge_proven"] is False


def test_first_night_429_and_cap_hit_are_warnings_not_hard_failures(tmp_path):
    case = _first_night_case(tmp_path, log_text="HTTP 429 retry\nTimeoutError recovered\n")
    case["thinking_budget"] = 16384
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "WARN"
    assert report["cap_hits"] == []
    assert report["log"]["http_429"] == 1
    assert report["log"]["timeout"] == 1
    case["thinking_budget"] = 8192
    report = AUDIT.first_night_report(**case)
    assert report["cap_hits"] == ["US.AMD"]
    assert "reasoning_cap_hits:1" in report["warnings"]


def test_first_night_hard_fails_model_degradation_contract_and_deadline(tmp_path):
    case = _first_night_case(
        tmp_path, model="qwen3.7-max", status="pipeline_timeout_finalized",
        finished="2026-11-17T08:58:00-05:00", degraded=True,
        contract={"violations": ["generic_claim"]},
    )
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert "model_mismatch:2" in report["failures"]
    assert "main_analysis_degraded:1" in report["failures"]
    assert "unresolved_contract:1" in report["failures"]
    assert "terminal_status:pipeline_timeout_finalized" in report["failures"]
    assert "deadline_missed" in report["failures"]
    assert "downstream_model_mismatch:bridge:qwen3.7-max" in report["failures"]
    assert "downstream_model_mismatch:instruction:qwen3.7-max" in report["failures"]


def test_first_night_repaired_contract_passes_but_missing_log_is_unknown(tmp_path):
    case = _first_night_case(
        tmp_path, contract={"violations": ["tripwires_lt2"], "violations_after": [],
                            "repair": {"applied": ["tripwires"]}}, log_text="",
    )
    report = AUDIT.first_night_report(**case)
    assert report["contract_repaired"] == ["US.AMD"]
    assert report["contract_unresolved"] == []
    assert report["log"]["status"] == "unknown"
    assert report["log"]["http_429"] is None
    assert report["status"] == "WARN"


def test_first_night_malformed_checkpoint_and_missing_terminal_fail_closed(tmp_path):
    case = _first_night_case(tmp_path)
    Path(case["ckpt_root"], case["date"], "US.BAD.json").write_text("{}", encoding="utf-8")
    Path(case["schedule_path"]).write_text(json.dumps({
        "date": case["date"], "status": "heartbeat", "analyses_done": 1,
    }) + "\n", encoding="utf-8")
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert "malformed_checkpoints:1" in report["failures"]
    assert "terminal_status:missing_terminal" in report["failures"]
    assert "artifacts_not_ok" in report["failures"]


def test_first_night_missing_downstream_and_schema_cannot_pass(tmp_path):
    case = _first_night_case(tmp_path)
    bridge = Path(case["repo"], "data", "opportunity_scan_2026-11-17.json")
    bridge.unlink()
    conn = sqlite3.connect(case["db_path"])
    conn.execute("ALTER TABLE playbook_instructions DROP COLUMN model_used")
    conn.commit()
    conn.close()
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert "bridge_error:missing" in report["failures"]
    assert any(item.startswith("instruction_error:") for item in report["failures"])


def test_first_night_missing_bridge_candidates_is_unknown_not_zero(tmp_path):
    case = _first_night_case(tmp_path)
    Path(case["repo"], "data", "opportunity_scan_2026-11-17.json").write_text(
        json.dumps({"model_used": "qwen4-max"}), encoding="utf-8")
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert report["downstream_models"]["bridge_candidates"] is None
    assert "bridge_error:candidates_missing_or_invalid" in report["failures"]


def test_first_night_partial_batch_and_missing_symbols_fail_closed(tmp_path):
    case = _first_night_case(tmp_path)
    terminal = json.loads(Path(case["schedule_path"]).read_text())
    terminal["analyses_count"] = 32
    terminal["batch_observation"]["analysed_count"] = 32
    terminal["batch_observation"]["missing_symbols"] = ["US.NVDA"]
    Path(case["schedule_path"]).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert "analysis_count_mismatch:32!=1" in report["failures"]
    assert "missing_symbols:1" in report["failures"]


def test_first_night_missing_usage_finish_and_instruction_row_are_visible(tmp_path):
    case = _first_night_case(tmp_path)
    checkpoint = Path(case["ckpt_root"], case["date"], "US.AMD.json")
    doc = json.loads(checkpoint.read_text())
    doc["_llm_output"].pop("_usage")
    doc["_llm_output"].pop("_finish_reason")
    checkpoint.write_text(json.dumps(doc), encoding="utf-8")
    conn = sqlite3.connect(case["db_path"])
    conn.execute("DELETE FROM playbook_instructions")
    conn.commit()
    conn.close()
    case["thinking_budget"] = 8192
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert "missing_usage:1" in report["warnings"]
    assert "missing_prompt_tokens:1" in report["warnings"]
    assert "missing_reasoning_tokens:1" in report["warnings"]
    assert "missing_finish_reason:1" in report["warnings"]
    assert report["cap_check"] == "unknown"
    assert "instruction_count_mismatch:0!=1" in report["failures"]


def test_first_night_unknown_expected_count_and_non_stop_finish_fail(tmp_path):
    case = _first_night_case(tmp_path)
    terminal = json.loads(Path(case["schedule_path"]).read_text())
    terminal.pop("analyses_count")
    terminal["batch_observation"].pop("analysed_count")
    terminal["batch_observation"].pop("selected_count", None)
    Path(case["schedule_path"]).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    checkpoint = Path(case["ckpt_root"], case["date"], "US.AMD.json")
    doc = json.loads(checkpoint.read_text())
    doc["_llm_output"]["_finish_reason"] = "length"
    checkpoint.write_text(json.dumps(doc), encoding="utf-8")
    report = AUDIT.first_night_report(**case)
    assert report["status"] == "FAIL"
    assert "expected_analyses_unknown" in report["failures"]
    assert "finish_not_stop:1" in report["failures"]


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
