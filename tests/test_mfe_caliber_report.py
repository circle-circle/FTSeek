import sqlite3

from scripts.mfe_caliber_report import report


def test_report_reads_all_three_forward_mfe_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER,"
        "trade_date TEXT, symbol TEXT, owner_strategy TEXT, source_type TEXT,"
        "exit_price REAL, pnl_net REAL, realized_pnl REAL, metadata_json TEXT,"
        "exit_reason TEXT, mfe_pct REAL, mfe_tick_pct REAL, mfe_t15_pct REAL,"
        "mfe_gate_pct REAL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1, "2026-11-05", "US.TEST", "DA", "PLAYBOOK_SIM", 101.0, 1.0, 2.0,
            "{}", "SL_BREAKEVEN", 0.5, 0.4, 0.2, 0.1,
        ),
    )

    result = report(conn, "2026-11-05", "2026-11-05", "PLAYBOOK_SIM")

    assert result["new_columns_present"] is True
    assert result["calibers"]["db_mfe_pct"]["dead_rate"] == 0.0
    assert result["calibers"]["tick_mfe_pct"]["dead_rate"] == 0.0
    assert result["calibers"]["t15_mfe_pct"]["dead_rate"] == 1.0
    assert result["calibers"]["gate_mfe_pct"]["dead_rate"] == 1.0
    assert result["divergence"]["compared"] == 1
    conn.close()
