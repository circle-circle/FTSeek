# -*- coding: utf-8 -*-
"""CLI 端到端验收: 下一批 DA 行 + da_feed scan → audit 脚本 (2026-10-21)."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.playbook.quality_shadow import build_quality_shadow

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "quality_shadow_event_reaction_audit.py"
SCAN = {
    "type": "scan", "date": "2026-10-22", "scan_window": "da_feed",
    "scanned_at": "2026-10-22T08:02:00-04:00",
    "brief_template_version": "ue_brief_v8",
    "surfaced": [{
        "symbol": "US.HIMS", "direction": "SHORT",
        "evidence_matched_event_reaction": {
            "age_h": 23.2, "post_event_move_pct": 3.77,
            "reaction_basis": "same_day_close",
        },
    }],
}


def _make_db(path, *, with_raw_reaction=True):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE playbook_instructions (
               id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
               decision_id TEXT, metadata_json TEXT, raw_instruction_json TEXT,
               created_at TEXT, updated_at TEXT)"""
    )
    reaction = {
        "age_h": 23.2, "post_event_move_pct": 3.77,
        "reaction_basis": "same_day_close",
        "source": "futu_daily_kline", "reason": None,
    }
    raw = {
        "source": "deep_analysis", "score_band": "MEDIUM",
        "ue_event_reaction": reaction if with_raw_reaction else {},
    }
    qs = build_quality_shadow({
        "date": "2026-10-22", "symbol": "US.HIMS", "direction": "BUY",
        "confidence": 55, "decision_id": "DA-2026-10-22-US.HIMS",
        "raw_instruction": raw,
    })
    created = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (1,'2026-10-22','US.HIMS','BUY',"
        "'DA-2026-10-22-US.HIMS',? ,?,?,?)",
        (json.dumps({"quality_shadow": qs}), json.dumps(raw), created, created))
    conn.commit()
    conn.close()


def _run(tmp_path, db, scan):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--since", "2026-10-22",
         "--scan-path", str(scan), "--json"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
    )
    return proc, json.loads(proc.stdout)


def test_cli_pass_with_matching_raw_and_scan(tmp_path):
    db = tmp_path / "audit.db"
    scan = tmp_path / "scan.jsonl"
    _make_db(db)
    scan.write_text(json.dumps(SCAN, ensure_ascii=False) + "\n", encoding="utf-8")
    proc, out = _run(tmp_path, db, scan)
    assert proc.returncode == 0, proc.stderr
    assert out["status"] == "PASS"
    assert out["key_full_coverage"] is True
    assert out["raw_reaction_matches"] == 1
    assert out["da_feed_scan"]["event_reaction_coverage"] == 1


def test_cli_fail_when_feed_had_reaction_but_raw_did_not(tmp_path):
    db = tmp_path / "audit.db"
    scan = tmp_path / "scan.jsonl"
    _make_db(db, with_raw_reaction=False)
    scan.write_text(json.dumps(SCAN, ensure_ascii=False) + "\n", encoding="utf-8")
    proc, out = _run(tmp_path, db, scan)
    assert proc.returncode == 1
    assert out["status"] == "FAIL"
    assert out["reason"] == "feed_reaction_not_persisted"
