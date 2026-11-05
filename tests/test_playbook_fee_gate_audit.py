import sqlite3

from scripts.playbook_fee_gate_audit import (
    build_report,
    evaluate_r06,
    find_playbook_consumers,
)


def test_r06_uses_actual_size_and_stop_distance():
    result = evaluate_r06(100.0, 99.9, 50, 5.0, rr_mult=2.0, max_ratio=0.25)
    assert result["eligible"] is True
    assert result["expected_gross_usd"] == 10.0
    assert result["fee_ratio"] == 0.5
    assert result["would_block"] is True
    passing = evaluate_r06(100.0, 95.0, 50, 5.0, rr_mult=2.0, max_ratio=0.25)
    assert passing["would_block"] is False


def test_missing_stop_is_not_counted_as_pass():
    result = evaluate_r06(100.0, None, 50, 5.0, rr_mult=2.0, max_ratio=0.25)
    assert result["eligible"] is False
    assert result["would_block"] is False
    assert result["fee_ratio"] is None


def test_report_separates_blocked_kept_and_missing_stop():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER, trade_date TEXT, symbol TEXT,"
        " decision_id TEXT, entry_price REAL, quantity INTEGER, fees_est REAL,"
        " pnl_net REAL, realized_pnl REAL, exit_price REAL, source_type TEXT,"
        " data_quality TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE playbook_instructions (id INTEGER, decision_id TEXT, stop_loss REAL)"
    )
    rows = [
        (1, "DA-1", 95.0, 10.0, 5.0),
        (2, "DA-2", 99.9, -50.0, -55.0),
        (3, "DA-3", 95.0, 100.0, 95.0),
    ]
    for trade_id, decision_id, stop_loss, gross, net in rows:
        conn.execute(
            "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "2026-11-02", "US.TEST", decision_id, 100.0, 50, 5.0,
             net, gross, 101.0, "PLAYBOOK_SIM", "real", "{}"),
        )
        if trade_id < 3:
            conn.execute(
                "INSERT INTO playbook_instructions VALUES (?,?,?)",
                (trade_id, decision_id, stop_loss),
            )
    gate = {"enabled": True, "rr_mult": 2.0, "max_fee_to_gross_ratio": 0.25}
    result = build_report(conn, "2026-11-01", "2026-11-03", "DA", gate)
    assert result["baseline"]["n"] == 3
    assert result["r06_eligible"]["n"] == 2
    assert result["blocked"]["n"] == 1
    assert result["kept"]["n"] == 1
    assert result["missing_or_invalid_stop"]["n"] == 1
    conn.close()


def test_current_playbook_path_has_no_r06_config_consumer():
    assert find_playbook_consumers() == []
