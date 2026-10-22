# -*- coding: utf-8 -*-
"""quality_shadow event_reaction 落库验收测试 (2026-10-21)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.playbook.quality_shadow import build_quality_shadow
from scripts.quality_shadow_event_reaction_audit import (
    apply_feed_gate,
    audit_rows,
    load_da_feed_scan,
    load_rows,
)


def _row(*, qs_present=True, with_reaction=True, mismatch=False, updated_at=None):
    reaction = {
        "age_h": 23.2,
        "post_event_move_pct": 3.77,
        "reaction_basis": "same_day_close",
        "source": "futu_daily_kline",
        "reason": None,
    }
    qs = build_quality_shadow({
        "date": "2026-10-22", "symbol": "US.HIMS", "direction": "BUY",
        "confidence": 55, "decision_id": "DA-2026-10-22-US.HIMS",
        "raw_instruction": {
            "source": "deep_analysis", "score_band": "MEDIUM",
            "ue_event_reaction": reaction,
        },
    })
    if mismatch:
        qs["post_event_move_pct"] = -9.99
    if not qs_present:
        qs = {}
    return {
        "id": 1, "date": "2026-10-22", "symbol": "US.HIMS",
        "direction": "BUY", "decision_id": "DA-2026-10-22-US.HIMS",
        "metadata_json": json.dumps({"quality_shadow": qs}) if qs else "{}",
        "raw_instruction_json": json.dumps({
            "source": "deep_analysis",
            "ue_event_reaction": reaction if with_reaction else {},
        }),
        "updated_at": updated_at,
    }


def test_audit_pass_with_matching_reaction():
    out = audit_rows([_row()])
    assert out["status"] == "PASS"
    assert out["key_full_coverage"] is True
    assert out["raw_reaction_rows"] == 1
    assert out["raw_reaction_matches"] == 1
    assert out["mismatches"] == []


def test_audit_fail_when_keys_missing():
    out = audit_rows([_row(qs_present=False)])
    assert out["status"] == "FAIL"
    assert out["quality_shadow_rows"] == 0


def test_audit_fail_on_value_mismatch():
    out = audit_rows([_row(mismatch=True)])
    assert out["status"] == "FAIL"
    assert out["mismatches"]


def test_audit_pass_without_ue_reaction_but_keys_present():
    out = audit_rows([_row(with_reaction=False)])
    assert out["status"] == "PASS_NO_REACTION"
    assert out["key_full_coverage"] is True


def test_audit_not_ready_when_no_rows():
    out = audit_rows([])
    assert out["status"] == "NOT_READY"
    assert out["total_da_rows"] == 0


def test_skip_rows_do_not_fail_audit():
    skip = {
        "id": 2, "date": "2026-10-22", "symbol": "US.SKIP",
        "direction": "SKIP", "decision_id": "DA-2026-10-22-US.SKIP",
        "metadata_json": "{}", "raw_instruction_json": "{}",
    }
    out = audit_rows([_row(), skip])
    assert out["status"] == "PASS"
    assert out["total_da_rows"] == 2
    assert out["actionable_rows"] == 1
    assert out["skip_rows"] == 1
    assert out["key_full_coverage"] is True


def test_only_skip_rows_is_no_actionable():
    skip = {
        "id": 2, "date": "2026-10-22", "symbol": "US.SKIP",
        "direction": "SKIP", "decision_id": "DA-2026-10-22-US.SKIP",
        "metadata_json": "{}", "raw_instruction_json": "{}",
    }
    out = audit_rows([skip])
    assert out["status"] == "NO_ACTIONABLE"
    assert out["actionable_rows"] == 0
    assert out["skip_rows"] == 1


def test_batch_in_progress_when_latest_row_is_fresh():
    now = datetime.now(timezone.utc)
    out = audit_rows(
        [_row(updated_at=(now - timedelta(seconds=30)).isoformat())],
        now=now, maturity_min=5)
    assert out["status"] == "BATCH_IN_PROGRESS"
    assert out["batch_mature"] is False


def test_batch_mature_after_quiet_window():
    now = datetime.now(timezone.utc)
    out = audit_rows(
        [_row(updated_at=(now - timedelta(minutes=10)).isoformat())],
        now=now, maturity_min=5)
    assert out["status"] == "PASS"
    assert out["batch_mature"] is True


def test_feed_gate_fails_when_feed_had_reaction_but_raw_did_not():
    result = audit_rows([_row(with_reaction=False)])
    assert result["status"] == "PASS_NO_REACTION"
    gated = apply_feed_gate(result, {"event_reaction_coverage": 2})
    assert gated["status"] == "FAIL"
    assert gated["reason"] == "feed_reaction_not_persisted"


def test_feed_gate_allows_no_reaction_when_feed_also_empty():
    result = audit_rows([_row(with_reaction=False)])
    gated = apply_feed_gate(result, {"event_reaction_coverage": 0})
    assert gated["status"] == "PASS_NO_REACTION"


def test_load_da_feed_scan_reports_coverage(tmp_path):
    path = tmp_path / "scan.jsonl"
    path.write_text(json.dumps({
        "type": "scan", "date": "2026-10-22", "scan_window": "da_feed",
        "scanned_at": "2026-10-22T08:02:00-04:00",
        "brief_template_version": "ue_brief_v8",
        "surfaced": [{
            "symbol": "HIMS", "direction": "SHORT",
            "evidence_matched_event_reaction": {
                "age_h": 23.2, "post_event_move_pct": 3.77,
                "reaction_basis": "same_day_close"},
        }],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    out = load_da_feed_scan("2026-10-22", path)
    assert out["found"] is True
    assert out["surfaced_n"] == 1
    assert out["event_reaction_coverage"] == 1
    assert out["reaction_basis"] == {"same_day_close": 1}


def test_load_rows_by_date(tmp_path):
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE playbook_instructions (
               id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
               decision_id TEXT, metadata_json TEXT, raw_instruction_json TEXT,
               created_at TEXT, updated_at TEXT)"""
    )
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (1,'2026-10-22','US.HIMS','BUY',"
        "'DA-1','{}','{}','2026-10-22T12:00:00+00:00','2026-10-22T12:00:00+00:00')")
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (2,'2026-10-21','US.BAX','SHORT',"
        "'DA-0','{}','{}','2026-10-21T12:00:00+00:00','2026-10-21T12:00:00+00:00')")
    conn.commit()
    conn.close()
    rows = load_rows(path, date="2026-10-22")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "US.HIMS"


def test_save_instruction_writes_reaction_quality_shadow(monkeypatch):
    import app.playbook.admission as admission
    from app.playbook.db import PlaybookDB

    monkeypatch.setattr(admission, "apply", lambda inst: (inst, {}))
    monkeypatch.setattr(
        admission, "evaluate",
        lambda inst: {"verdict": "PASS", "reasons": [], "metrics": {}})
    conn = sqlite3.connect(":memory:")
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    inst = {
        "date": "2026-10-22", "symbol": "US.TEST", "direction": "BUY",
        "confidence": 60, "decision_id": "DA-TEST-RFC156",
        "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "position_size": 100, "risk_amount": 500, "risk_reward": 2.0,
        "raw_instruction": {
            "source": "deep_analysis", "score_band": "MEDIUM",
            "entry_archetype": "PULLBACK_LIMIT",
            "ue_event_reaction": {
                "age_h": 23.2, "post_event_move_pct": 3.77,
                "reaction_basis": "same_day_close",
                "source": "futu_daily_kline", "reason": None,
            },
        },
    }
    row_id = db.save_instruction(inst)
    row = conn.execute(
        "SELECT metadata_json FROM playbook_instructions WHERE id=?",
        (row_id,)).fetchone()
    qs = json.loads(row[0] or "{}").get("quality_shadow") or {}
    assert qs["event_age_h"] == 23.2
    assert qs["post_event_move_pct"] == 3.77
    assert qs["reaction_basis"] == "same_day_close"
    assert qs["reaction_source"] == "futu_daily_kline"
