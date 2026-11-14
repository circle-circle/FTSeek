"""signal_snapshots 实时写入时间口径契约。"""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from app.analysis import signal_ic_tracker as tracker


def _init_production_schema(path):
    tracker._ensure_table()
    conn = sqlite3.connect(tracker._get_db_path())
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_snapshots)")}
    if "market_context_json" not in columns:
        conn.execute("ALTER TABLE signal_snapshots ADD COLUMN market_context_json TEXT")
    conn.commit()
    conn.close()


def test_default_snapshot_timestamps_are_et_aware(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_INITIALIZED", False)
    monkeypatch.setattr(tracker, "_DB_PATH", tmp_path / "signal_snapshots.db")
    _init_production_schema(tmp_path / "signal_snapshots.db")
    tracker.record_signal_snapshot(
        decision_id="universe_expansion:2026-11-13:MU",
        symbol="US.MU", direction="LONG", strength=50,
        strategy_id="universe_expansion", trade_date="2026-11-13",
    )

    conn = sqlite3.connect(tracker._get_db_path())
    created_at, created_at_snap = conn.execute(
        "SELECT created_at,created_at_snap FROM signal_snapshots WHERE decision_id=?",
        ("universe_expansion:2026-11-13:MU",),
    ).fetchone()
    conn.close()
    for value in (created_at, created_at_snap):
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.now(ZoneInfo("America/New_York")).utcoffset()


def test_explicit_da_timestamp_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_INITIALIZED", False)
    monkeypatch.setattr(tracker, "_DB_PATH", tmp_path / "signal_snapshots.db")
    da_ts = "2026-11-13T08:11:10.748717-05:00"
    tracker.record_signal_snapshot(
        decision_id="DA-20261113211110-US.MU", symbol="US.MU",
        direction="LONG", strength=70, strategy_id="deep_analysis",
        created_at=da_ts, trade_date="2026-11-13",
    )
    conn = sqlite3.connect(tracker._get_db_path())
    row = conn.execute(
        "SELECT created_at,created_at_snap FROM signal_snapshots WHERE decision_id=?",
        ("DA-20261113211110-US.MU",),
    ).fetchone()
    conn.close()
    assert row[0] == da_ts
    assert datetime.fromisoformat(row[1]).tzinfo is not None
