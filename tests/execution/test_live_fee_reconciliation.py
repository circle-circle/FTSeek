import json
import sqlite3

from app.execution.live_fee_reconciliation import (
    fee_discrepancy_is_material,
    reconcile_live_order_fees,
)


class FakeBroker:
    def __init__(self, fees=None, error=False):
        self.fees = fees if fees is not None else {"ENTRY-1": 1.99, "EXIT-1": 2.0}
        self.error = error
        self.calls = []

    def query_order_fees(self, order_ids, target_env="real"):
        self.calls.append((list(order_ids), target_env))
        if self.error:
            return {"success": False, "error": "broker unavailable"}
        return {"success": True, "fees": self.fees, "rows": []}


def _connection():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            source_type TEXT,
            trade_mode TEXT,
            symbol TEXT,
            futu_order_id TEXT,
            close_futu_order_id TEXT,
            realized_pnl REAL,
            fees_est REAL,
            pnl_net REAL,
            net_pnl_pct REAL,
            fees_source TEXT,
            metadata_json TEXT,
            entry_price REAL,
            quantity INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY,
            pnl REAL,
            pnl_pct REAL,
            outcome TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO playbook_instructions (id, pnl, pnl_pct, outcome) "
        "VALUES (83, -2.67, 0.0046, 'loss')"
    )
    return conn


def _insert_trade(conn, **overrides):
    metadata = {
        "playbook_instruction_id": 83,
        "fee_breakdown": {
            "entry": {"total": 1.993},
            "exit": {"total": 2.013},
            "total": 4.01,
            "source": "est",
        }
    }
    row = {
        "source_type": "PLAYBOOK_LIVE",
        "trade_mode": "live",
        "symbol": "US.TEST",
        "futu_order_id": "ENTRY-1",
        "close_futu_order_id": "EXIT-1",
        "realized_pnl": 1.34,
        "fees_est": 4.01,
        "pnl_net": -2.67,
        "net_pnl_pct": -0.0092,
        "fees_source": "est",
        "metadata_json": json.dumps(metadata),
        "entry_price": 289.9999,
        "quantity": 1,
    }
    row.update(overrides)
    conn.execute(
        "INSERT INTO trade_records (source_type, trade_mode, symbol, "
        "futu_order_id, close_futu_order_id, realized_pnl, fees_est, pnl_net, net_pnl_pct, "
        "fees_source, metadata_json, entry_price, quantity) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(row.values()),
    )
    conn.commit()


def test_reconcile_live_fees_upgrades_net_pnl_and_preserves_estimate():
    conn = _connection()
    _insert_trade(conn)
    broker = FakeBroker()

    result = reconcile_live_order_fees(broker, conn, 1)

    assert result["status"] == "ok"
    assert result["fees_total"] == 3.99
    assert result["estimated_total"] == 4.01
    assert result["difference"] == -0.02
    assert broker.calls == [(["ENTRY-1", "EXIT-1"], "real")]

    row = conn.execute(
        "SELECT fees_est, pnl_net, net_pnl_pct FROM trade_records WHERE id=1"
    ).fetchone()
    assert row == (3.99, -2.65, -0.0091)
    source = conn.execute(
        "SELECT fees_source FROM trade_records WHERE id=1"
    ).fetchone()[0]
    assert source == "broker"

    metadata = json.loads(
        conn.execute("SELECT metadata_json FROM trade_records WHERE id=1").fetchone()[0]
    )
    breakdown = metadata["fee_breakdown"]
    assert breakdown["entry"]["total"] == 1.993
    assert breakdown["broker"]["entry_fee"] == 1.99
    assert breakdown["broker"]["exit_fee"] == 2.0
    assert breakdown["broker"]["total"] == 3.99
    assert breakdown["estimated_total"] == 4.01
    assert breakdown["difference"] == -0.02
    instruction = conn.execute(
        "SELECT pnl, pnl_pct, outcome FROM playbook_instructions WHERE id=83"
    ).fetchone()
    assert instruction == (-2.65, -0.0091, "loss")
    conn.close()


def test_reconciliation_skips_sim_rows():
    conn = _connection()
    _insert_trade(conn, source_type="PLAYBOOK_SIM", trade_mode="simulate")
    broker = FakeBroker()

    result = reconcile_live_order_fees(broker, conn, 1)

    assert result == {"status": "skipped", "reason": "not a live playbook trade"}
    assert broker.calls == []
    conn.close()


def test_reconciliation_fails_closed_without_partial_update():
    conn = _connection()
    _insert_trade(conn)
    broker = FakeBroker({"ENTRY-1": 1.99})

    result = reconcile_live_order_fees(broker, conn, 1)

    assert result["status"] == "failed"
    assert "did not return both" in result["reason"]
    row = conn.execute(
        "SELECT fees_est, fees_source FROM trade_records WHERE id=1"
    ).fetchone()
    assert row == (4.01, "est")
    conn.close()


def test_material_fee_discrepancy_threshold():
    assert fee_discrepancy_is_material(4.01, 3.99) is False
    assert fee_discrepancy_is_material(4.01, 4.10) is True
    assert fee_discrepancy_is_material(None, 4.10) is False
