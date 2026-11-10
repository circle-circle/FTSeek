import json
import sqlite3

from app.analysis import signal_ic_tracker as tracker


def _use_temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "signal_snapshots.db"
    monkeypatch.setattr(tracker, "_DB_PATH", db_path)
    monkeypatch.setattr(tracker, "_INITIALIZED", False)
    tracker._ensure_table()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE signal_snapshots ADD COLUMN market_context_json TEXT"
        )
    return db_path


def _record(decision_id="DA-1", strategy_id="deep_analysis", factors=None,
            market_context=None):
    tracker.record_signal_snapshot(
        decision_id=decision_id,
        symbol="US.TEST",
        direction="LONG",
        strength=80,
        confidence=0.8,
        consensus_level=0.8,
        strategy_id=strategy_id,
        created_at="2026-11-05T08:00:00-05:00",
        trade_date="2026-11-05",
        factors=factors,
        market_context=market_context,
    )


def test_da_claims_survive_playbook_factor_upsert(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    claims = {
        "claims": [{
            "thesis": "EPS beat with raised guidance",
            "claim_type": "earnings_beat",
            "side": "bull",
        }],
        "conditions": ["earnings_beat_streak"],
        "debate_outcome": "PARTIAL_AGREE",
    }
    _record(factors=claims)
    _record(
        factors=["news_positive", "volume_expansion"],
        market_context={"news_positive": {"value": 1.0}},
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT factors_json, market_context_json FROM signal_snapshots "
            "WHERE decision_id = 'DA-1'"
        ).fetchone()
    factors = json.loads(row[0])
    assert factors["claims"] == claims["claims"]
    assert factors["conditions"] == [
        "earnings_beat_streak", "news_positive", "volume_expansion"
    ]
    assert factors["debate_outcome"] == "PARTIAL_AGREE"
    assert factors["playbook_factors"] == ["news_positive", "volume_expansion"]
    assert json.loads(row[1])["news_positive"]["value"] == 1.0


def test_non_da_factor_upsert_behavior_is_unchanged(monkeypatch, tmp_path):
    db_path = _use_temp_db(monkeypatch, tmp_path)
    _record(decision_id="MAIN-1", strategy_id="main", factors=["old"])
    _record(decision_id="MAIN-1", strategy_id="main", factors=["new"])

    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT factors_json FROM signal_snapshots WHERE decision_id = 'MAIN-1'"
        ).fetchone()[0]
    assert json.loads(raw) == ["new"]
