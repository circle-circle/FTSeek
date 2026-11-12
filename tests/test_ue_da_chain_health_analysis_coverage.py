from __future__ import annotations

import json
import sqlite3

from scripts.ue_da_chain_health import (
    _checkpoint_analysis_fields,
    _day_cluster_stats,
    _instruction_class,
    contribution_audit,
    effective_window_start,
    latest_complete_scan,
    load_closed_da_trades,
)


def test_analysis_ue_context_detected(tmp_path):
    day_dir = tmp_path / "2026-11-10"
    day_dir.mkdir()
    (day_dir / "US.TEST.json").write_text(
        json.dumps({"_ue_origin": "ue_scan", "_ue_batch_id": "batch-1"}),
        encoding="utf-8",
    )
    fields = _checkpoint_analysis_fields("2026-11-10", "US.TEST", tmp_path)
    assert fields["analysis_found"] is True
    assert fields["analysis_ue_context"] is True
    assert fields["analysis_ue_batch_id"] == "batch-1"


def test_missing_analysis_is_not_context(tmp_path):
    fields = _checkpoint_analysis_fields("2026-11-10", "US.MISS", tmp_path)
    assert fields["analysis_found"] is False
    assert fields["analysis_ue_context"] is False


def test_latest_complete_scan_filters_window_date_and_status(tmp_path):
    path = tmp_path / "scan.jsonl"
    rows = [
        {"type": "scan", "date": "2026-11-10", "scan_window": "da_feed",
         "batch_status": "failed", "batch_id": "bad"},
        {"type": "scan", "date": "2026-11-10", "scan_window": "da_feed",
         "batch_status": "complete", "batch_id": "good"},
        {"type": "scan", "date": "2026-11-10", "scan_window": "premarket",
         "batch_status": "complete", "batch_id": "other-window"},
        {"type": "scan", "date": "2026-11-11", "scan_window": "da_feed",
         "batch_status": "complete", "batch_id": "other-date"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    scan = latest_complete_scan(path, date="2026-11-10", window="da_feed")
    assert scan["batch_id"] == "good"


def test_prompt_eligible_requires_unique_origin_and_causal_chain(tmp_path):
    day_dir = tmp_path / "2026-11-10"
    day_dir.mkdir()
    (day_dir / "US.UE.json").write_text(json.dumps({
        "_ue_origin": True, "_ue_batch_id": "batch-1", "_ue_causal_chain": "cause",
    }), encoding="utf-8")
    (day_dir / "US.OVERLAP.json").write_text(json.dumps({
        "_ue_origin": False, "_ue_batch_id": "batch-1", "_ue_causal_chain": "cause",
    }), encoding="utf-8")
    unique = _checkpoint_analysis_fields("2026-11-10", "US.UE", tmp_path)
    overlap = _checkpoint_analysis_fields("2026-11-10", "US.OVERLAP", tmp_path)
    assert unique["analysis_prompt_eligible"] is True
    assert overlap["analysis_prompt_eligible"] is False


def test_instruction_class_separates_origin_overlap_and_non_ue():
    assert _instruction_class({"origin": "ue_candidate"}) == "ue_origin"
    assert _instruction_class({"origin": "ue_fresh_event"}) == "ue_origin"
    assert _instruction_class({"origin": "", "ue_batch_id": "batch-1"}) == "ue_overlap"
    assert _instruction_class({"origin": ""}) == "non_ue"


def test_day_cluster_stats_small_sample_is_not_distinguishable():
    assert _day_cluster_stats([])["n_days"] == 0
    single = _day_cluster_stats([10.0])
    assert single["n_days"] == 1
    assert single["mde_80pct"] is None
    assert single["distinguishable"] is False
    assert _day_cluster_stats([10.0, 20.0])["mde_80pct"] is not None


def _write_checkpoint(root, day, symbol, *, origin, batch, causal, prompt,
                      decision_id=None, action="BUY"):
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol,
        "action": action,
        "_ue_origin": origin,
        "_ue_batch_id": batch,
        "_ue_causal_chain": causal,
        "_ue_direction": "LONG",
        "_ic_decision_id": decision_id,
        "_supply_selection": {"stage": "da_ue_feed" if origin else "da_extra_watchlist"},
    }
    (day_dir / f"{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")
    (day_dir / f"{symbol}.raw.json").write_text(json.dumps({
        "user_prompt": "事件扫描器(UE)今晨浮现该标的" if prompt else "普通 DA 材料",
    }), encoding="utf-8")


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE playbook_instructions (
        id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
        skip_reason TEXT, triggered INTEGER, executed INTEGER, decision_id TEXT,
        execution_state TEXT, row_kind TEXT, raw_instruction_json TEXT)""")
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
        decision_id TEXT, entry_price REAL, quantity INTEGER, realized_pnl REAL,
        pnl_net REAL, fees_est REAL, fees_source TEXT, exit_reason TEXT,
        metadata_json TEXT, source_type TEXT, strategy_id TEXT, exit_price REAL,
        data_quality TEXT)""")
    conn.commit()
    return conn


def _insert_instruction(conn, *, date, symbol, direction, decision_id, origin="",
                        batch="", ue_direction="LONG", triggered=0, executed=0,
                        row_kind="instruction"):
    raw = {"origin": origin, "ue_batch_id": batch, "ue_direction": ue_direction}
    conn.execute(
        """INSERT INTO playbook_instructions
           (date, symbol, direction, skip_reason, triggered, executed, decision_id,
            execution_state, row_kind, raw_instruction_json)
           VALUES (?, ?, ?, '', ?, ?, ?, 'PENDING', ?, ?)""",
        (date, symbol, direction, triggered, executed, decision_id, row_kind,
         json.dumps(raw)),
    )
    conn.commit()


def _insert_trade(conn, *, trade_date, symbol, decision_id, net, excluded=False):
    metadata = {"exclusion_reason": "first_run_test_2026-09-17"} if excluded else {}
    conn.execute(
        """INSERT INTO trade_records
           (trade_date, symbol, direction, decision_id, entry_price, quantity,
            realized_pnl, pnl_net, fees_est, fees_source, exit_reason, metadata_json,
            source_type, strategy_id, exit_price, data_quality)
           VALUES (?, ?, 'LONG', ?, 100.0, 1, ?, ?, 1.0, 'est', 'TP', ?,
                   'PLAYBOOK_SIM', 'playbook_sim', 101.0, 'real')""",
        (trade_date, symbol, decision_id, net + 1.0, net, json.dumps(metadata)),
    )
    conn.commit()


def test_contribution_audit_funnel_and_a9_dual(tmp_path):
    scan_path = tmp_path / "scan.jsonl"
    scan_path.write_text(json.dumps({
        "type": "scan", "date": "2026-11-10", "scan_window": "da_feed",
        "batch_status": "complete", "batch_id": "batch-1",
        "surfaced": [
            {"symbol": "UNIQUE", "direction": "LONG"},
            {"symbol": "OVERLAP", "direction": "LONG"},
            {"symbol": "PLACEHOLDER", "direction": "LONG"},
            {"symbol": "MISS", "direction": "LONG"},
        ],
    }), encoding="utf-8")
    checkpoint_dir = tmp_path / "checkpoint"
    _write_checkpoint(checkpoint_dir, "2026-11-10", "US.UNIQUE", origin=True,
                      batch="batch-1", causal="cause", prompt=True, decision_id="D1")
    _write_checkpoint(checkpoint_dir, "2026-11-10", "US.OVERLAP", origin=False,
                      batch="batch-1", causal="cause", prompt=False, decision_id="D2")
    _write_checkpoint(checkpoint_dir, "2026-11-10", "US.PLACEHOLDER", origin=True,
                      batch="batch-1", causal="cause", prompt=True, decision_id="D3")
    db_path = tmp_path / "test.db"
    conn = _make_db(db_path)
    _insert_instruction(conn, date="2026-11-10", symbol="US.UNIQUE", direction="BUY",
                        decision_id="D1", origin="ue_candidate", triggered=1)
    _insert_instruction(conn, date="2026-11-10", symbol="US.OVERLAP", direction="BUY",
                        decision_id="D2", batch="batch-1", triggered=1, executed=1)
    _insert_instruction(conn, date="2026-11-10", symbol="US.PLACEHOLDER", direction="SKIP",
                        decision_id="", row_kind="supply_placeholder")
    _insert_trade(conn, trade_date="2026-11-10", symbol="US.OVERLAP", decision_id="D2", net=5.0)
    _insert_trade(conn, trade_date="2026-11-10", symbol="US.OVERLAP", decision_id="D2",
                  net=100.0, excluded=True)
    conn.close()

    result = contribution_audit(scan_path, db_path, checkpoint_dir, "2026-11-10", "2026-11-10")
    origin = result["funnel"]["ue_origin"]
    overlap = result["funnel"]["ue_overlap"]
    assert origin["analyses_n"] == 2
    assert origin["prompt_eligible_n"] == 2
    assert origin["prompt_context_n"] == 2
    assert origin["prompt_missing_n"] == 0
    assert origin["instructions_n"] == 1
    assert origin["actionable_n"] == 1
    assert origin["executed_n"] == 0
    assert origin["supply_placeholder_n"] == 1
    assert overlap["analyses_n"] == 1
    assert overlap["prompt_eligible_n"] == 0
    assert overlap["executed_n"] == 1
    assert result["scan_funnel"]["scan_actionable_n"] == 4
    assert result["scan_funnel"]["scan_analyzed_n"] == 3
    assert result["scan_funnel"]["scan_not_analyzed_symbols"] == ["MISS"]
    assert result["performance"]["ue_overlap"]["a9_clean"]["n"] == 1
    assert result["performance"]["ue_overlap"]["a9_clean"]["net"] == 5.0
    assert result["performance"]["ue_overlap"]["a9_included"]["n"] == 2
    assert result["performance"]["ue_overlap"]["a9_excluded_n"] == 1
    assert result["data_quality"]["origin_mismatch_n"] == 0
    assert {"code": "UE_ORIGIN_SAMPLE_LT_30", "n": 0} in result["warnings"]


def test_contribution_audit_matches_checkpoint_decision_id(tmp_path):
    scan_path = tmp_path / "scan.jsonl"
    scan_path.write_text(json.dumps({
        "type": "scan", "date": "2026-11-10", "scan_window": "da_feed",
        "batch_status": "complete", "batch_id": "batch-1", "surfaced": [],
    }), encoding="utf-8")
    checkpoint_dir = tmp_path / "checkpoint"
    _write_checkpoint(checkpoint_dir, "2026-11-10", "US.SYM", origin=False,
                      batch="batch-1", causal="cause", prompt=False, decision_id="D_NEW")
    db_path = tmp_path / "test.db"
    conn = _make_db(db_path)
    _insert_instruction(conn, date="2026-11-10", symbol="US.SYM", direction="BUY",
                        decision_id="D_OLD", origin="ue_candidate")
    _insert_instruction(conn, date="2026-11-10", symbol="US.SYM", direction="BUY",
                        decision_id="D_NEW", batch="batch-1")
    conn.close()
    result = contribution_audit(scan_path, db_path, checkpoint_dir, "2026-11-10", "2026-11-10")
    assert result["data_quality"]["origin_mismatch_n"] == 0
    assert result["funnel"]["ue_overlap"]["analyzed_to_instruction_n"] == 1


def test_effective_window_start_clamps_to_current_era():
    assert effective_window_start("2026-06-30") == "2026-07-01"
    assert effective_window_start("2026-07-01") == "2026-07-01"
    assert effective_window_start("2026-11-10") == "2026-11-10"


def test_load_closed_da_trades_excludes_pre_era_rows(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = _make_db(db_path)
    _insert_trade(conn, trade_date="2026-06-30", symbol="US.PRE", decision_id="DA-PRE", net=5.0)
    _insert_trade(conn, trade_date="2026-07-01", symbol="US.ERA", decision_id="DA-ERA", net=6.0)
    conn.close()

    rows = load_closed_da_trades(db_path, "2026-06-30", "2026-07-01")

    assert [row["symbol"] for row in rows] == ["US.ERA"]


def test_contribution_audit_window_reports_effective_start_and_clamp(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = _make_db(db_path)
    _insert_trade(conn, trade_date="2026-06-30", symbol="US.PRE", decision_id="DA-PRE", net=5.0)
    _insert_trade(conn, trade_date="2026-07-01", symbol="US.ERA", decision_id="DA-ERA", net=6.0)
    conn.close()

    result = contribution_audit(
        tmp_path / "missing_scan.jsonl", db_path, tmp_path / "missing_checkpoints",
        "2026-06-30", "2026-07-01",
    )

    assert result["window"] == {
        "start": "2026-07-01",
        "end": "2026-07-01",
        "requested_start": "2026-06-30",
        "clamped": True,
        "basis": "entry_cohort_closed_A9_A10",
    }


def test_contribution_audit_window_not_clamped_inside_era(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = _make_db(db_path)
    conn.close()

    result = contribution_audit(
        tmp_path / "missing_scan.jsonl", db_path, tmp_path / "missing_checkpoints",
        "2026-07-01", "2026-07-01",
    )

    assert result["window"]["start"] == "2026-07-01"
    assert result["window"]["requested_start"] == "2026-07-01"
    assert result["window"]["clamped"] is False
