# -*- coding: utf-8 -*-
"""VWAP 门评估器: DA archetype/plan-gap 切片 (2026-10-21)."""
import json
import sqlite3

import scripts.eval_vwap_gate as ev


def _db(tmp_path):
    path = tmp_path / "eval.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE playbook_instructions (
               symbol TEXT, date TEXT, direction TEXT, entry_price REAL,
               raw_instruction_json TEXT)"""
    )
    # SKIP 行: DB direction 已被改写; 必须从 raw archetype 还原 SHORT.
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?)",
        ("US.BAX", "2026-10-21", "SKIP", 24.41, json.dumps({
            "entry_archetype": "FADE_SHORT",
            "planned_entry": 24.7,
            "confirmation": {"price_level": 24.7, "side": "above"},
        })))
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?)",
        ("US.BWXT", "2026-10-21", "BUY", 135.62, json.dumps({
            "entry_archetype": "PULLBACK_LIMIT",
            "planned_entry": 132.5,
        })))
    conn.commit()
    conn.close()
    return path


def test_meta_infers_short_from_archetype_when_db_is_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_DB", str(_db(tmp_path)))
    meta = ev._load_instruction_meta()
    row = meta[("US.BAX", "2026-10-21", "SHORT")]
    assert row["archetype"] == "FADE_SHORT"
    assert row["planned_entry"] == 24.7
    assert row["instruction_entry"] == 24.41
    assert row["plan_gap_pct"] == -1.174


def test_meta_plan_gap_for_pullback(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_DB", str(_db(tmp_path)))
    meta = ev._load_instruction_meta()
    row = meta[("US.BWXT", "2026-10-21", "BUY")]
    assert row["archetype"] == "PULLBACK_LIMIT"
    assert row["plan_gap_pct"] == 2.355


def test_plan_gap_bucket_boundaries():
    assert ev._plan_gap_bucket(None) == "UNKNOWN"
    assert ev._plan_gap_bucket(-1.2) == "<=-1%"
    assert ev._plan_gap_bucket(-0.2) == "-1..1%"
    assert ev._plan_gap_bucket(1.0) == ">=1%"
