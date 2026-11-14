import json
import sqlite3

from scripts.entry_geometry_review import build_review


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
            entry_price REAL, decision_id TEXT
        );
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
            entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL,
            quantity REAL, realized_pnl REAL, fees_est REAL, pnl_net REAL,
            holding_hours REAL, mfe_pct REAL, mae_pct REAL, metadata_json TEXT,
            decision_id TEXT, source_type TEXT
        );
    """)
    conn.execute("INSERT INTO playbook_instructions VALUES (1,'2026-11-12','US.DPC','BUY',33.0,'DA-DPC')")
    conn.execute("INSERT INTO playbook_instructions VALUES (2,'2026-11-12','US.MRK','BUY',147.0,'DA-MRK')")
    conn.execute("""INSERT INTO trade_records VALUES (
        1,'2026-11-12','US.DPC','LONG','2026-11-12T09:59:09-05:00',
        '2026-11-12T10:04:21-05:00',34.16,33.095,174,-185.31,5.18,-190.49,
        0.1,0.0,3.308,?,'DA-DPC','PLAYBOOK_SIM')""", (json.dumps({
            "reprice_audit": {"reprice_to": 34.55}, "stop_loss": 32.82,
            "atr": 0.64, "entry_price_source": "sync_recovery_backfill",
            "peak_price": 34.16,
        }),))
    conn.execute("""INSERT INTO trade_records VALUES (
        2,'2026-11-12','US.MRK','LONG','2026-11-12T09:48:48-05:00',
        '2026-11-12T10:13:09-05:00',147.344,147.665,40,12.84,4.35,8.49,
        0.4,0.64,0.2,?,'DA-MRK','PLAYBOOK_SIM')""", (json.dumps({
            "stop_loss": 140.76, "entry_price_source": "broker_deal",
        }),))
    conn.commit()
    conn.close()


def _arms(path):
    rows = [
        {"type": "entry_confirm", "date": "2026-11-12", "inst_id": 1,
         "arm": "treatment", "ts": "2026-11-12T09:57:18-05:00",
         "delay_actual_sec": 125, "outcome": "CONFIRMED"},
        {"type": "entry_confirm", "date": "2026-11-12", "inst_id": 2,
         "arm": "holdout", "ts": "2026-11-12T09:48:09-05:00"},
        {"type": "entry_confirm_post", "date": "2026-11-12", "inst_id": 1,
         "ts": "2026-11-12T10:10:00-05:00"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_entry_geometry_review_tracks_anchor_latency_grace_and_recovery(tmp_path):
    db = tmp_path / "trades.db"
    _db(db)
    arms = tmp_path / "arms.jsonl"
    _arms(arms)
    result = build_review("2026-11-12", db, arms, anchor_cap_pct=2.0)
    assert result["summary"]["n_closed"] == 2
    assert result["summary"]["wins"] == 1
    dpc = result["reprice"]["rows"][0]
    assert dpc["planned_entry"] == 33.0 and dpc["effective_entry"] == 34.55
    assert dpc["fill_entry"] == 34.16
    assert dpc["adverse_fill_vs_planned_pct"] > 3.5
    assert dpc["would_anchor_skip"] is True
    assert result["reprice"]["would_skip_symbols"] == ["US.DPC"]
    treatment = result["dispatch"]["treatment"]
    assert treatment["median_total_delay_sec"] == 236.0
    assert treatment["median_post_confirm_queue_sec"] == 111.0
    holdout = result["dispatch"]["holdout"]
    assert holdout["median_total_delay_sec"] == 39.0
    assert holdout["median_post_confirm_queue_sec"] is None
    assert result["grace"]["n_mae_ge_2pct"] == 1
    recovery = result["recovery"]["rows"][0]
    assert recovery["atr_over_sl_distance"] == 0.4776
    assert result["recovery"]["median_atr_over_sl_distance"] == 0.478
    assert "不计算反事实 PnL" in " ".join(result["caveats"])
