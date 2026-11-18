"""DA 链契约只读审计的纯函数与 SQLite 契约测试 (2026-10-23)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "da_chain_contract_audit", ROOT / "scripts" / "da_chain_contract_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


def test_debate_state_priority_analyst_skip_wins_even_if_unavailable():
    r = M.classify_debate("SKIP", None, None, challenger_available=False)
    assert r["state"] == "ANALYST_SKIP"
    assert r["priority"] == 1


def test_debate_state_unavailable_is_not_abstain():
    assert M.classify_debate("BUY", None, None, False)["state"] == "UNAVAILABLE"
    assert M.classify_debate("BUY", "SKIP", "DISAGREE", True)["state"] == "ABSTAIN"


def test_debate_partial_is_not_upgraded_by_agreed_boolean():
    r = M.classify_debate("BUY", "BUY", "PARTIAL_AGREE", True)
    assert r["state"] == "PARTIAL_AGREE"
    assert r["priority"] == 6


def test_debate_verdict_direction_mismatch_is_conflict():
    assert M.classify_debate("BUY", "BUY", "DISAGREE", True)["state"] == "CONFLICT"
    assert M.classify_debate("BUY", "SELL_SHORT", "AGREE", True)["state"] == "CONFLICT"


def test_debate_invalid_enum_is_conflict_not_abstain():
    assert M.classify_debate("BUY", None, "AGREE", True)["state"] == "CONFLICT"
    assert M.classify_debate("INVALID", "BUY", "AGREE", True)["state"] == "CONFLICT"


def _kline(*rows):
    return [{"time": d + " 00:00:00", "open": c, "high": c, "low": c,
             "close": c, "volume": 1} for d, c in rows]


def test_price_basis_detects_prior_day_close_mislabeled_as_prev_close():
    # TSCO: 快照 prev_close=32.91 实为 10/20 收盘; 真昨收 32.69.
    snap = {"session": "premarket", "prev_close_price": 32.91,
            "pre_price": 30.82, "pre_change_rate": -5.72}
    r = M.classify_price_basis(snap, _kline(("2026-10-20", 32.91),
                                            ("2026-10-21", 32.69)), "2026-10-22")
    assert r["status"] == "prev_close_mismatch"
    assert r["bar_date"] == "2026-10-21"
    assert r["bar_close"] == 32.69
    assert abs(r["mismatch_pct"] - 0.673) < 1e-6
    assert r["implied_vs_bar_pct"] is not None and abs(r["implied_vs_bar_pct"]) < 0.01


def test_price_basis_correct_close_is_ok():
    snap = {"session": "premarket", "prev_close_price": 135.11,
            "pre_price": 132.34, "pre_change_rate": -2.05}
    r = M.classify_price_basis(snap, _kline(("2026-10-21", 135.11)), "2026-10-22")
    assert r["status"] == "ok"


def test_price_basis_snapshot_error_is_not_silently_ok():
    r = M.classify_price_basis({"_error": "timeout"}, _kline(("2026-10-21", 1.0)),
                               "2026-10-22")
    assert r["status"] == "snapshot_error"


def test_price_basis_afterhours_not_checked():
    r = M.classify_price_basis({"session": "afterhours", "prev_close_price": 1.0},
                               _kline(("2026-10-21", 1.0)), "2026-10-22")
    assert r["status"] == "not_checked"


def test_finnhub_url_ids_do_not_collapse_into_one_duplicate():
    a = {"section": "news_v2", "url": "https://finnhub.io/api/news?id=aaa"}
    b = {"section": "news_v2", "url": "https://finnhub.io/api/news?id=bbb"}
    assert M._dedupe_key(a) != M._dedupe_key(b)


def test_source_stats_detects_cross_section_same_story_and_research_time():
    collected = {
        "news": [{"news_id": "1", "title": "same", "publish_time": "1792669312"}],
        "research": [{"news_id": "1", "title": "same", "publish_time": "1792669312"}],
        "news_v2": [],
        "sentiment": [],
    }
    s = M.source_stats(collected, "2026-10-22")
    assert s["duplicate_extra_items"] == 1
    assert s["cross_section_extra_items"] == 1
    assert s["render_time_missing"].get("research", 0) == 0
    collected["research"][0].pop("publish_time")
    assert M.source_stats(collected, "2026-10-22")["render_time_missing"].get("research") == 1


def test_compute_rr_direction_aware_and_invalid_geometry():
    assert M.compute_rr("BUY", 100, 90, 120) == 2.0
    assert M.compute_rr("SELL_SHORT", 100, 110, 80) == 2.0
    assert M.compute_rr("BUY", 100, 110, 120) is None
    assert M.compute_rr("SKIP", 100, 90, 120) is None


def test_compare_memory_flags_pseudo_window_gross_net_and_old_era():
    patterns = [{
        "direction": "LONG", "exit_reason": "SL_TRAIL_LOCK",
        "strength_bucket": "70-80", "total_trades": 60, "total_pnl": 1000.0,
        "last_trade_date": "2026-10-21",
    }]
    trades = [
        # 全史 3 笔; 窗口内仅 1 笔; 净额与毛额不同
        {"trade_date": "2026-06-25", "direction": "LONG",
         "exit_reason": "SL_TRAIL_LOCK", "signal_strength": 75,
         "realized_pnl": 500.0, "pnl_net": 480.0},
        {"trade_date": "2026-06-26", "direction": "LONG",
         "exit_reason": "SL_TRAIL_LOCK", "signal_strength": 75,
         "realized_pnl": 400.0, "pnl_net": 390.0},
        {"trade_date": "2026-10-20", "direction": "LONG",
         "exit_reason": "SL_TRAIL_LOCK", "signal_strength": 75,
         "realized_pnl": 100.0, "pnl_net": 90.0},
    ]
    r = M.compare_memory_patterns(patterns, trades, "2026-09-22")
    assert r["displayed_groups"] == 1
    assert r["window_n_mismatch_groups"] == 1
    assert r["gross_vs_net_mismatch_groups"] == 1
    assert r["old_era_rows"] == 2
    assert r["true_window_n_sum"] == 1


def test_checkpoint_audit_preserves_model_prompt_and_as_of_lineage(tmp_path):
    (tmp_path / "US.AMD.raw.json").write_text(json.dumps({
        "symbol": "US.AMD", "user_prompt": "frozen prompt",
        "saved_at": "2026-11-17T08:00:00-05:00",
        "input_version": "full-v1",
        "event_context": {"as_of": "2026-11-17T12:30:00+00:00",
                          "injection_hash": "event123"},
        "collected": {"snapshot": {}, "kline": []},
    }), encoding="utf-8")
    (tmp_path / "US.AMD.json").write_text(json.dumps({
        "symbol": "US.AMD", "_ckpt_model": "qwen4-max",
        "_ic_decision_id": "DA-amd",
        "_llm_output": {"direction": "SKIP", "_model_reported": "qwen4-max",
                        "_prompt_hash": "prompt-v3"},
    }), encoding="utf-8")
    result = M.audit_checkpoints(tmp_path, "2026-11-17")
    row = result["analyses"]["US.AMD"]
    assert row["checkpoint_model"] == "qwen4-max"
    assert row["reported_model"] == "qwen4-max"
    assert row["prompt_hash"] == "prompt-v3"
    assert row["input_version"] == "full-v1"
    assert row["event_as_of"] == "2026-11-17T12:30:00+00:00"
    assert row["user_prompt_chars"] == len("frozen prompt")
    assert len(row["user_prompt_sha256"]) == 64


def _make_stage_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
            entry_price REAL, stop_loss REAL, take_profit REAL, risk_reward REAL,
            decision_id TEXT, execution_state TEXT, execution_price REAL,
            model_used TEXT, skip_reason TEXT)
    """)
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
            entry_price REAL, quantity REAL, realized_pnl REAL, pnl_net REAL,
            fees_est REAL, fees_source TEXT, exit_time TEXT, exit_price REAL,
            exit_reason TEXT, metadata_json TEXT, decision_id TEXT)
    """)
    conn.execute(
        "INSERT INTO playbook_instructions VALUES "
        "(1,'2026-10-22','US.SNDK','BUY',1330.64,1264.11,1430.44,2.45,"
        "'DA-x','BOOKED',1331.673,'qwen4-max',NULL)")
    meta = {"playbook_instruction_id": 1, "stop_loss": 1264.11,
            "take_profit": 1430.44,
            "_tp_rr_gate": {"initial_sl": 1264.11, "tp": 1430.44,
                            "rr_at_execution": 1.46185, "swallowed": True}}
    conn.execute(
        "INSERT INTO trade_records VALUES "
        "(2,'2026-10-22','US.SNDK','LONG',1331.673,2,15.07,11.01,4.06,'est',"
        "'2026-10-22T15:00:00-04:00',1430.0,'SL_TRAIL_LOCK',?,'DA-x')",
        (json.dumps(meta),))
    conn.commit()
    conn.close()


def test_stage_audit_keeps_plan_submitted_filled_distinct(tmp_path):
    db = tmp_path / "t.db"
    _make_stage_db(db)
    analyses = {"US.SNDK": {
        "decision_id": "DA-x", "direction": "BUY", "score": 8,
        "plan": {"entry_price": 1315.0, "stop_loss_price": 1260.0,
                 "take_profit_price": 1450.0},
        "gate": {"decision": "PASS", "reasons": ["test"]},
        "checkpoint_model": "qwen4-max", "reported_model": "qwen4-max",
        "prompt_hash": "prompt-v3", "user_prompt_sha256": "abc123",
        "raw_saved_at": "2026-10-22T08:00:00-04:00",
        "event_as_of": "2026-10-22T12:00:00+00:00",
    }}
    r = M.stage_audit(analyses, db, "2026-10-22")
    assert r["status"] == "ok"
    row = r["rows"][0]
    assert row["plan"]["rr"] == round(135 / 55, 6)
    assert row["submitted"]["rr"] == round(99.8 / 66.53, 6)
    assert row["filled"]["rr"] == round(98.767 / 67.563, 6)
    assert row["filled"]["tp_swallowed"] is True
    assert row["model_mismatch"] is False
    assert row["prompt"]["production_hash"] == "prompt-v3"
    assert row["as_of"]["event_as_of"] == "2026-10-22T12:00:00+00:00"
    assert row["gate"]["decision"] == "PASS"
    assert row["outcome"] == {
        "fill_count": 1, "closed_count": 1, "open_count": 0,
        "realized_pnl": 15.07, "pnl_net": 11.01, "fees_est": 4.06,
        "fees_sources": ["est"], "exit_reasons": ["SL_TRAIL_LOCK"],
        "attribution_sources": ["metadata"], "trade_ids": [2],
    }
    assert r["trade_attribution"]["metadata"] == 1
    assert r["plan_vs_filled_rr_mismatch_gt_0_05"] == 1
    assert r["tp_swallowed"] == 1


def test_stage_audit_marks_duplicate_decision_ambiguous_but_keeps_exact_fill_outcome(tmp_path):
    db = tmp_path / "t.db"
    _make_stage_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO playbook_instructions VALUES "
        "(2,'2026-10-22','US.SNDK','BUY',1330.64,1264.11,1430.44,2.45,"
        "'DA-x','BOOKED',1331.673,'qwen4-max',NULL)")
    conn.commit()
    conn.close()
    analyses = {"US.SNDK": {
        "decision_id": "DA-x", "direction": "BUY",
        "plan": {"entry_price": 1315.0, "stop_loss_price": 1260.0,
                 "take_profit_price": 1450.0},
        "checkpoint_model": "qwen4-max", "reported_model": "qwen3.7-max",
    }}
    result = M.stage_audit(analyses, db, "2026-10-22")
    row = result["rows"][0]
    assert row["match"] == "ambiguous"
    assert row["candidate_instruction_ids"] == [1, 2]
    assert row["model_mismatch"] is True
    assert row["outcome"]["pnl_net"] == 11.01
    assert row["outcome"]["attribution_sources"] == ["metadata"]
    assert result["ambiguous"] == 1
    assert result["model_mismatch"] == 1


def test_stage_audit_duplicate_decision_id_is_narrowed_by_symbol(tmp_path):
    db = tmp_path / "t.db"
    _make_stage_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO playbook_instructions VALUES "
        "(2,'2026-10-22','US.AMD','BUY',100,95,110,2.0,"
        "'DA-x','BOOKED',101,'qwen4-max',NULL)")
    meta = {"playbook_instruction_id": 2, "stop_loss": 95, "take_profit": 110}
    conn.execute(
        "INSERT INTO trade_records VALUES "
        "(3,'2026-10-22','US.AMD','LONG',101,1,3.0,2.0,1.0,'est',"
        "'2026-10-22T15:00:00-04:00',105,'SL_TRAIL_LOCK',?,'DA-x')",
        (json.dumps(meta),))
    conn.commit()
    conn.close()
    analyses = {
        "US.SNDK": {"decision_id": "DA-x", "direction": "BUY", "plan": {}},
        "US.AMD": {"decision_id": "DA-x", "direction": "BUY", "plan": {}},
    }
    result = M.stage_audit(analyses, db, "2026-10-22")
    rows = {row["symbol"]: row for row in result["rows"]}
    assert rows["US.SNDK"]["outcome"]["pnl_net"] == 11.01
    assert rows["US.AMD"]["outcome"]["pnl_net"] == 2.0
    assert rows["US.SNDK"]["outcome"]["trade_ids"] == [2]
    assert rows["US.AMD"]["outcome"]["trade_ids"] == [3]
    assert result["matched"] == 2
    assert result["ambiguous"] == 0


def test_stage_audit_multi_fill_summaries_cover_every_fill(tmp_path):
    db = tmp_path / "t.db"
    _make_stage_db(db)
    conn = sqlite3.connect(db)
    original = conn.execute("SELECT metadata_json FROM trade_records WHERE id=2").fetchone()[0]
    meta = json.loads(original)
    meta["_tp_rr_gate"]["swallowed"] = False
    conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=2", (json.dumps(meta),))
    second = {
        "playbook_instruction_id": 1, "stop_loss": 1264.11, "take_profit": 1430.44,
        "_tp_rr_gate": {"initial_sl": 1264.11, "tp": 1430.44,
                        "rr_at_execution": 1.1, "swallowed": True},
    }
    conn.execute(
        "INSERT INTO trade_records VALUES "
        "(3,'2026-10-22','US.SNDK','LONG',1340,1,3.0,2.0,1.0,'est',"
        "'2026-10-22T15:30:00-04:00',1430,'SL_TRAIL_LOCK',?,'DA-x')",
        (json.dumps(second),))
    conn.commit()
    conn.close()
    analyses = {"US.SNDK": {
        "decision_id": "DA-x", "direction": "BUY",
        "plan": {"entry_price": 1315.0, "stop_loss_price": 1260.0,
                 "take_profit_price": 1450.0},
    }}
    result = M.stage_audit(analyses, db, "2026-10-22")
    row = result["rows"][0]
    assert row["outcome"]["fill_count"] == 2
    assert row["outcome"]["pnl_net"] == 13.01
    assert len(row["fills"]) == 2
    assert row["fills"][1]["entry_gap_pct"] is not None
    assert result["fill_events"] == 2
    assert result["tp_swallowed"] == 1
    assert result["plan_vs_filled_rr_mismatch_gt_0_05"] == 2


def test_stage_audit_zero_exit_price_remains_open_not_closed(tmp_path):
    db = tmp_path / "t.db"
    _make_stage_db(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE trade_records SET exit_time=NULL,exit_price=0,"
                 "realized_pnl=NULL,pnl_net=NULL,fees_est=NULL,exit_reason=NULL WHERE id=2")
    conn.commit()
    conn.close()
    result = M.stage_audit({"US.SNDK": {
        "decision_id": "DA-x", "direction": "BUY", "plan": {},
    }}, db, "2026-10-22")
    outcome = result["rows"][0]["outcome"]
    assert outcome["open_count"] == 1
    assert outcome["closed_count"] == 0
    assert outcome["pnl_net"] is None


def test_stage_audit_symbol_unique_fallback_has_explicit_count(tmp_path):
    db = tmp_path / "t.db"
    _make_stage_db(db)
    result = M.stage_audit({"US.SNDK": {
        "decision_id": "DA-missing", "direction": "BUY", "plan": {},
    }}, db, "2026-10-22")
    assert result["matched"] == 0
    assert result["symbol_unique"] == 1
    assert result["ambiguous"] == 0
    assert result["no_instruction"] == 0


def _feedback_row(rid=1, **changes):
    row = {
        "id": rid, "trade_date": "2026-10-26", "exit_time": "2026-10-26T12:00:00-04:00",
        "exit_price": 10.0, "realized_pnl": 1.0, "pnl_net": 0.9,
        "fees_est": 0.1, "fees_source": "est", "data_quality": "real",
        "source_type": "PLAYBOOK_SIM", "decision_id": "DA-test", "metadata_json": "{}",
    }
    row.update(changes)
    return row


def test_feedback_clock_distinguishes_sqlite_utc_day_from_et_day():
    row = _feedback_row(trade_date="2026-07-02", exit_time="2026-07-02T23:27:40-04:00")
    result = M.compare_feedback_clocks([row], "2026-07-03")
    assert result["views"]["live_exit_day_ids"] == [1]
    assert result["views"]["exit_et_day_ids"] == []
    assert result["live_day_not_observed_et_ids"] == [1]
    assert result["views"]["exit_et_window_ids"] == [1]
    assert result["views"]["exit_utc_et_date_mismatch_ids"] == [1]


def test_feedback_clock_marks_naive_time_unknown_and_splits_source():
    row = _feedback_row(exit_time="2026-10-26T23:00:00", decision_id="EXT-test")
    result = M.compare_feedback_clocks([row], "2026-10-26")
    assert result["views"]["live_exit_day_ids"] == [1]
    assert result["views"]["exit_time_unknown_ids"] == [1]
    assert result["source_ids"] == {"EXT": [1]}


def test_feedback_a9_unknown_is_not_counted_as_clean():
    rows = [_feedback_row(1), _feedback_row(2, metadata_json="{invalid"),
            _feedback_row(3, metadata_json="")]
    result = M.compare_feedback_clocks(rows, "2026-10-26")
    assert result["a9_including_count"] == 3
    assert result["a9_excluding_count"] == 1
    assert result["a9_unknown_count"] == 2


def test_feedback_missing_metadata_column_is_unknown(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trade_records (id INTEGER, trade_date TEXT, exit_time TEXT, "
                 "realized_pnl REAL, strategy_id TEXT, source_type TEXT)")
    conn.execute("INSERT INTO trade_records VALUES "
                 "(1,'2026-10-26','2026-10-26T12:00:00-04:00',1,'deep_analysis','PLAYBOOK_SIM')")
    conn.commit()
    conn.close()
    result = M.feedback_clock_audit(db, "2026-10-26")
    assert result["status"] == "ok"
    assert result["a9_unknown_count"] == 1
    assert result["a9_excluding_count"] == 0
    assert "metadata_json" in result["missing_columns"]


def test_main_includes_feedback_clock_in_readonly_report(tmp_path, monkeypatch, capsys):
    day = "2026-10-26"
    (tmp_path / day).mkdir()
    base = {"analyses": {}, "price": {}, "source_aggregate": {}, "sources": {},
            "debate_states": {}, "partial_agreed_true": [], "debates": {}}
    monkeypatch.setattr(M, "audit_checkpoints", lambda *_: base)
    monkeypatch.setattr(M, "stage_audit", lambda *_: {"status": "ok"})
    monkeypatch.setattr(M, "memory_audit", lambda *_: {"status": "ok"})
    monkeypatch.setattr(M, "feedback_clock_audit", lambda *_: {"status": "sentinel"})
    monkeypatch.setattr(M, "summarize", lambda *_: [])
    assert M.main(["--date", day, "--checkpoint-root", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["feedback_clock"] == {"status": "sentinel"}
    assert report["read_only"] is True
