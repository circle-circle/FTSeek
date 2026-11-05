import sqlite3

import pytest

from scripts.da_expectancy_report import build_report, daily_cluster_bps, summarize


def _row(day, gross, fee, net, notional=5000.0):
    return {"trade_date": day, "realized_pnl": gross, "fees_est": fee,
            "net": net, "notional": notional}


def test_summarize_reports_payoff_aware_breakeven_rate():
    result = summarize([
        _row("2026-11-02", 30.0, 5.0, 25.0),
        _row("2026-11-02", -105.0, 5.0, -110.0),
        _row("2026-11-03", 10.0, 5.0, 5.0),
    ])
    assert result["gross_bps"] == pytest.approx(-43.3333, abs=0.0001)
    assert result["fee_bps"] == 10.0
    assert result["net_bps"] == pytest.approx(-53.3333, abs=0.0001)
    assert result["net_win_rate"] == pytest.approx(2 / 3, abs=0.0001)
    assert result["breakeven_win_rate"] == pytest.approx(110 / 125)
    assert result["profit_factor"] == pytest.approx(30 / 110, abs=0.0001)


def test_daily_cluster_uses_daily_means_and_mde80():
    result = daily_cluster_bps([
        {"trade_date": "2026-11-02", "net_bps": 10.0},
        {"trade_date": "2026-11-02", "net_bps": 20.0},
        {"trade_date": "2026-11-03", "net_bps": -5.0},
    ])
    assert result["days"] == 2
    assert result["mean_bps"] == 5.0
    assert result["t"] == 0.5
    assert result["mde80_bps"] == pytest.approx(28.01585, abs=0.0001)


def test_build_report_emits_a9_dual_calibers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER, trade_date TEXT, decision_id TEXT,"
        " entry_price REAL, quantity INTEGER, realized_pnl REAL, fees_est REAL,"
        " pnl_net REAL, exit_price REAL, source_type TEXT, data_quality TEXT,"
        " metadata_json TEXT)"
    )
    base = (1, "2026-11-02", "DA-TEST", 100.0, 50, 20.0, 5.0, 15.0,
            101.0, "PLAYBOOK_SIM", "real", "{}")
    conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", base)
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, "2026-11-02", "DA-TEST-2", 100.0, 50, -30.0, 5.0, -35.0,
         99.0, "PLAYBOOK_SIM", "real", '{"exclusion_reason":"first_run_test"}'),
    )
    result = build_report(conn, "2026-11-01", "2026-11-03", "DA")
    assert result["clean"]["n"] == 1
    assert result["clean"]["net_usd"] == 15.0
    assert result["including_excluded"]["n"] == 2
    assert result["including_excluded"]["net_usd"] == -20.0
    conn.close()
