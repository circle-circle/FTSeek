import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from app.playbook.pipeline import _include_observed_prices_in_extremes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location(
    "repair_trade_exit_invariants",
    PROJECT_ROOT / "scripts" / "repair_trade_exit_invariants.py",
)
repair = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = repair
spec.loader.exec_module(repair)


def create_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
            source_type TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL,
            exit_price REAL, quantity INTEGER, realized_pnl REAL, pnl_pct REAL,
            fees_est REAL, pnl_net REAL, fees_source TEXT, mfe_pct REAL, mae_pct REAL,
            max_drawdown REAL, data_quality TEXT, exit_reason TEXT, metadata_json TEXT
        )
    """
    )
    return conn


def insert_trade(conn, trade_id, symbol, direction, entry, exit_price, metadata, **overrides):
    values = {
        "trade_date": "2026-09-16",
        "source_type": "PLAYBOOK_SIM",
        "entry_time": "2026-09-16T09:45:00-04:00",
        "exit_time": "2026-09-17T04:00:00-04:00",
        "quantity": 10,
        "realized_pnl": (exit_price - entry) * 10,
        "pnl_pct": (exit_price - entry) / entry,
        "fees_est": 1.0,
        "pnl_net": (exit_price - entry) * 10 - 1.0,
        "fees_source": "est",
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "max_drawdown": 0.0,
        "data_quality": "real",
        "exit_reason": "lock_in",
        **overrides,
    }
    conn.execute(
        """
        INSERT INTO trade_records (
            id, trade_date, symbol, direction, source_type, entry_time, exit_time,
            entry_price, exit_price, quantity, realized_pnl, pnl_pct, fees_est,
            pnl_net, fees_source, mfe_pct, mae_pct, max_drawdown, data_quality,
            exit_reason, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id, symbol, direction, values["trade_date"], values["source_type"],
            values["entry_time"], values["exit_time"], entry, exit_price,
            values["quantity"], values["realized_pnl"], values["pnl_pct"],
            values["fees_est"], values["pnl_net"], values["fees_source"],
            values["mfe_pct"], values["mae_pct"], values["max_drawdown"],
            values["data_quality"], values["exit_reason"], json.dumps(metadata),
        ),
    )


def test_duplicate_cleanup_rows_are_neutralized(tmp_path):
    conn = create_db(tmp_path / "db.sqlite")
    metadata = {"peak_price": 11.0, "trough_price": 10.0}
    insert_trade(conn, 1, "US.TEST", "LONG", 10.0, 11.0, metadata,
                 exit_reason="P3_DUP_CLEANUP", realized_pnl=0.0, pnl_pct=0.0)
    insert_trade(conn, 2, "US.TEST", "LONG", 10.0, 11.0, metadata)

    repaired = repair.repair_duplicate_cleanup_rows(conn)

    row = conn.execute(
        "SELECT data_quality, realized_pnl, fees_est, pnl_net, metadata_json "
        "FROM trade_records WHERE id=1"
    ).fetchone()
    assert repaired == [1]
    assert row[:4] == ("duplicate_external_close", None, None, None)
    assert json.loads(row[4])["duplicate_cleanup"]["primary_trade_id"] == 2
    conn.close()


def test_exit_extremes_include_actual_fill_price(tmp_path):
    conn = create_db(tmp_path / "db.sqlite")
    metadata = {"peak_price": 10.335, "peak_price_bar": 10.35, "trough_price": 9.925}
    insert_trade(conn, 1, "US.NOK", "LONG", 10.335, 10.55, metadata,
                 realized_pnl=123.25, pnl_pct=0.0208)

    repaired = repair.repair_exit_extremes(conn)
    row = conn.execute("SELECT metadata_json, mfe_pct, mae_pct FROM trade_records WHERE id=1").fetchone()
    updated = json.loads(row[0])

    assert repaired == [1]
    assert updated["peak_price"] == 10.55
    assert updated["peak_price_bar"] == 10.55
    assert row[1] == 2.0803
    assert row[2] == 3.9671
    conn.close()


def test_pipeline_extreme_helper_handles_short_and_missing_values():
    metadata = {"peak_price": 39.655, "trough_price": 39.835}
    changed = _include_observed_prices_in_extremes(metadata, "SHORT", 39.835, 39.49)

    assert changed is True
    assert metadata == {
        "peak_price": 39.49,
        "trough_price": 39.835,
        "peak_price_bar": 39.49,
        "trough_price_bar": 39.835,
    }


def test_pipeline_extreme_helper_rejects_invalid_price():
    metadata = {}
    assert _include_observed_prices_in_extremes(metadata, "LONG", 10.0, None) is False
    assert metadata == {}
