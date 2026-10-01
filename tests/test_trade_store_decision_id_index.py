import sqlite3

from app.storage.trade_store import TradeStore


def test_decision_id_index_created_and_used(tmp_path):
    db_path = str(tmp_path / "trade-store.db")
    TradeStore(db_path)

    with sqlite3.connect(db_path) as connection:
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(trade_records)")
        }
        assert "idx_trade_records_decision_id" in indexes

        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM trade_records WHERE decision_id = ?",
            ("DA-test",),
        ).fetchall()
        assert any("idx_trade_records_decision_id" in row[3] for row in plan)
