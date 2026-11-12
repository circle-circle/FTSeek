import sqlite3

from scripts.entry_blackout_window_stats import bucket_for, load_rows, summarize


def _row(day, symbol, entry, exit_time, net, source="PLAYBOOK_SIM"):
    return {
        "trade_date": day, "symbol": symbol, "entry_time": entry,
        "exit_time": exit_time, "net_pnl": net, "source_type": source,
    }


def test_bucket_boundaries_are_left_closed_right_open():
    assert bucket_for("2026-11-12T09:34:59-05:00") == "open_0930_0935"
    assert bucket_for("2026-11-12T09:35:00-05:00") == "open_0935_0945"
    assert bucket_for("2026-11-12T09:44:59-05:00") == "open_0935_0945"
    assert bucket_for("2026-11-12T09:45:00-05:00") == "open_0945_1000"


def test_summarize_excludes_open_and_reports_daily_uncertainty():
    rows = [
        _row("2026-11-10", "US.A", "2026-11-10T09:35:00-05:00", "2026-11-10T10:00:00-05:00", -100),
        _row("2026-11-10", "US.B", "2026-11-10T09:40:00-05:00", "2026-11-10T10:00:00-05:00", 20),
        _row("2026-11-11", "US.A", "2026-11-11T09:40:00-05:00", "2026-11-11T10:00:00-05:00", 40),
        _row("2026-11-11", "US.OPEN", "2026-11-11T09:41:00-05:00", None, None),
        _row("2026-11-11", "US.C", "2026-11-11T09:45:00-05:00", "2026-11-11T10:00:00-05:00", 10),
    ]
    result = summarize(rows)
    target = result["buckets"]["open_0935_0945"]
    assert target["n"] == 3 and target["days"] == 2
    assert target["net_pnl"] == -40 and target["excluded_open_or_no_pnl"] == 1
    cluster = result["target_0935_0945_daily_cluster"]
    assert cluster["active_days"] == 2 and cluster["mean_net_per_active_day"] == -20
    assert cluster["normal95_mean_net"][0] < -20 < cluster["normal95_mean_net"][1]
    assert result["buckets"]["open_0945_1000"]["n"] == 1
    assert "not a causal counterfactual" in result["caveat"]


def test_load_rows_is_read_only_and_uses_net_fallback(tmp_path):
    db = tmp_path / "trades.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE trade_records (
        trade_date TEXT, symbol TEXT, entry_time TEXT, exit_time TEXT,
        source_type TEXT, realized_pnl REAL, fees_est REAL, pnl_net REAL)""")
    conn.executemany(
        "INSERT INTO trade_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-11-10", "US.A", "2026-11-10T09:40:00-05:00", "2026-11-10T10:00:00-05:00",
             "PLAYBOOK_SIM", 10.0, 2.0, None),
            ("2026-11-10", "US.B", "2026-11-10T09:40:00-05:00", "2026-11-10T10:00:00-05:00",
             "PLAYBOOK_SIM", 20.0, 2.0, 17.0),
            ("2026-11-10", "US.C", "2026-11-10T09:40:00-05:00", "2026-11-10T10:00:00-05:00",
             "OTHER", 99.0, 0.0, 99.0),
            ("2026-10-01", "US.D", "2026-10-01T09:40:00-04:00", "2026-10-01T10:00:00-04:00",
             "PLAYBOOK_SIM", 99.0, 0.0, 99.0),
        ])
    conn.commit()
    conn.close()
    rows = load_rows(db, "2026-11-01")
    assert [row["net_pnl"] for row in rows] == [8.0, 17.0]
