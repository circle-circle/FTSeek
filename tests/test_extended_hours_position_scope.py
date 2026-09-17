import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_position_scope import load_non_manual_positions


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL, "
        "avg_entry_price REAL, current_sl REAL, entry_time TEXT, signal_id TEXT, "
        "owner TEXT, trader_type TEXT, owner_strategy TEXT, source TEXT)"
    )
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, exit_time TEXT, "
        "exit_price REAL, source_type TEXT, owner_strategy TEXT, metadata_json TEXT)"
    )
    return conn


def test_unknown_and_manual_positions_are_excluded(tmp_path):
    path = tmp_path / "positions.db"
    conn = _create_db(path)
    rows = [
        ("US.AUTO", "LONG", 10, 100, 95, "2026-09-17T10:00:00-04:00", "signal-auto", "auto", "AUTO", "playbook_sim", "scheduler"),
        ("US.MANUAL", "LONG", 10, 100, 95, "2026-09-17T10:00:00-04:00", "signal-manual", "manual", "MANUAL", "", "web"),
        ("US.UNKNOWN", "LONG", 10, 100, 95, "2026-09-17T10:00:00-04:00", "", "auto", "MANUAL", "", "unknown"),
    ]
    conn.executemany("INSERT INTO open_positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute(
        "INSERT INTO trade_records (id,symbol,exit_time,exit_price,source_type,owner_strategy) "
        "VALUES (1,'US.AUTO','',0,'PLAYBOOK_SIM','playbook_sim')")
    conn.execute(
        "INSERT INTO trade_records (id,symbol,exit_time,exit_price,source_type,owner_strategy) "
        "VALUES (2,'US.MANUAL','',0,'MANUAL','')")
    conn.execute(
        "INSERT INTO trade_records (id,symbol,exit_time,exit_price,source_type,owner_strategy) "
        "VALUES (3,'US.UNKNOWN','',0,NULL,NULL)")
    conn.commit()
    conn.close()

    assert load_non_manual_positions(path) == [
        ("US.AUTO", "LONG", 10, 100, 95, "2026-09-17T10:00:00-04:00",
         "signal-auto", None, 1, "PLAYBOOK_SIM")
    ]


def test_collectors_use_positive_evidence_scope(tmp_path, monkeypatch):
    path = tmp_path / "positions.db"
    conn = _create_db(path)
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.AUTO','LONG',10,100,95,"
        "'2026-09-17T10:00:00-04:00','signal-auto','auto','AUTO','playbook_sim','scheduler')"
    )
    conn.execute(
        "INSERT INTO trade_records (id,symbol,exit_time,exit_price,source_type,owner_strategy,metadata_json) "
        "VALUES (1,'US.AUTO','',0,'PLAYBOOK_SIM','playbook_sim','{\"atr\":0.5}')")
    conn.commit()
    conn.close()

    for name, relative in (
        ("pm_scope_test", "scripts/premarket_exit_shadow.py"),
        ("ah_scope_test", "scripts/afterhours_exit_shadow.py"),
    ):
        module = _load(name, relative)
        monkeypatch.setattr(module, "DB", path)
        assert module._load_open_positions() == [
            ("US.AUTO", "LONG", 10, 100, 95, "2026-09-17T10:00:00-04:00",
             "signal-auto", '{"atr":0.5}', 1, "PLAYBOOK_SIM")
        ]
