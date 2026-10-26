"""runtime/capabilities 与持仓保护口径的契约测试 (2026-10-25, RFC-165 Batch 2)."""
from __future__ import annotations

import asyncio
import pathlib
import sqlite3

from app.web_dashboard.routes.dashboard import _parse_positions
from app.web_dashboard.runtime_status import build_runtime_status, map_market_phase

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _runtime(**overrides):
    base = dict(
        connect_flag=True,
        account_known=True,
        account_scope="REAL",
        positions_raw=[{"code": "US.AAPL", "data_source": "broker"}],
        positions=[{"code": "US.AAPL", "price_basis": "live"}],
        positions_authoritative=True,
        orders_meta={"source": "broker", "broker_ok": True},
        gates={"blocks_now": []},
        market_phase="opening_drive",
        runtime_as_of="2026-10-25T10:00:00-04:00",
        data_as_of="2026-10-25T09:59:55-04:00",
    )
    base.update(overrides)
    return build_runtime_status(**base)


def test_healthy_runtime_allows_all_actions():
    rt = _runtime()
    assert rt["broker"] == "connected"
    assert rt["account"] == "real"
    assert rt["quotes"] == "live"
    assert rt["positions"] == "live"
    assert rt["orders"] == "live"
    assert rt["market"] == "regular"
    assert rt["as_of"] == "2026-10-25T09:59:55-04:00"
    assert rt["capabilities"] == {
        "place": True, "reduce": True, "cancel": True, "modify_stop": True,
    }
    assert rt["reasons"] == []


def test_disconnected_is_fully_read_only():
    rt = _runtime(connect_flag=False, account_known=False, positions_raw=[],
                  positions=[], positions_authoritative=False,
                  orders_meta={"source": "none", "broker_ok": False})
    assert rt["broker"] == "disconnected"
    assert rt["account"] == "unknown"
    assert not any(rt["capabilities"].values())
    assert any("只读" in r for r in rt["reasons"])


def test_db_fallback_positions_lock_risk_actions_but_not_stale_cancel():
    rt = _runtime(
        positions_raw=[{"code": "US.AAPL", "data_source": "db_fallback"}],
        positions=[{"code": "US.AAPL", "price_basis": "cost"}],
        positions_authoritative=False,
    )
    assert rt["broker"] == "degraded"
    assert rt["positions"] == "degraded"
    assert rt["quotes"] == "degraded"
    assert rt["capabilities"]["place"] is False
    assert rt["capabilities"]["reduce"] is False
    assert rt["capabilities"]["modify_stop"] is False
    # 订单仍是券商权威来源, 撤单不因持仓降级被无谓关闭。
    assert rt["capabilities"]["cancel"] is True


def test_stale_quotes_block_new_risk_but_allow_reduce():
    rt = _runtime(positions=[{"code": "US.AAPL", "price_basis": "delayed"}])
    assert rt["broker"] == "connected"
    assert rt["quotes"] == "degraded"
    assert rt["capabilities"]["place"] is False
    assert rt["capabilities"]["reduce"] is True
    assert rt["capabilities"]["modify_stop"] is True


def test_gate_block_blocks_place_only():
    rt = _runtime(gates={"blocks_now": ["回撤熔断: -3.2%"]})
    assert rt["capabilities"]["place"] is False
    assert rt["capabilities"]["reduce"] is True
    assert rt["capabilities"]["cancel"] is True
    assert "回撤熔断: -3.2%" in rt["reasons"]


def test_unknown_orders_lock_cancel_only():
    rt = _runtime(orders_meta={"source": "none", "broker_ok": False})
    assert rt["orders"] == "unknown"
    assert rt["broker"] == "degraded"
    assert rt["capabilities"]["cancel"] is False
    assert rt["capabilities"]["place"] is True
    assert rt["capabilities"]["reduce"] is True


def test_missing_account_blocks_place_but_not_exit():
    rt = _runtime(account_known=False)
    assert rt["account"] == "unknown"
    assert rt["broker"] == "degraded"
    assert rt["capabilities"]["place"] is False
    assert rt["capabilities"]["reduce"] is True
    assert rt["capabilities"]["modify_stop"] is True


def test_no_positions_does_not_claim_live_quotes_or_block_limit_orders():
    rt = _runtime(positions_raw=[], positions=[], positions_authoritative=True)
    assert rt["quotes"] == "unknown"
    assert rt["positions"] == "live"
    assert rt["capabilities"]["place"] is True


def test_market_phase_mapping():
    assert map_market_phase("TRADING") == "regular"
    assert map_market_phase("closing_drive") == "regular"
    assert map_market_phase("PRE_MARKET") == "pre"
    assert map_market_phase("AFTER_HOURS") == "after"
    assert map_market_phase("weekend") == "closed"
    assert map_market_phase("???") == "unknown"


class _FakePositionDS:
    def __init__(self, protection_rows, columns):
        self._protection_rows = protection_rows
        self._columns = columns

    async def get_batch_quotes_detailed(self, codes):
        return {"US.AAPL": {"cur_price": 100.0}}

    async def get_open_position_owners(self):
        return [{"symbol": "US.AAPL", "owner": "manual", "entry_time": "2026-10-25T09:30:00"}]

    async def db_query(self, sql, params=(), db_path=None):
        if sql.startswith("PRAGMA table_info"):
            return [{"name": c} for c in self._columns]
        if "FROM open_positions" in sql:
            return self._protection_rows
        return []


def _position_raw():
    return [{
        "code": "US.AAPL", "qty": 10, "cost_price": 90.0,
        "cur_price": 100.0, "pl_val": 100.0, "unrealized_pl": 100.0,
        "today_pl_val": 5.0, "position_side": "LONG",
        "account_type": "real",
    }]


def test_parse_positions_prefers_current_sl_and_marks_broker_protection():
    ds = _FakePositionDS(
        protection_rows=[{
            "symbol": "US.AAPL", "current_sl": 95.0, "stop_loss_price": 90.0,
            "initial_tp": 110.0, "sl_broker_order_id": "STOP-1",
            "tp_broker_order_id": "TP-1", "trailing_stop_price": None,
            "stop_loss_triggered": 0, "trailing_stop_triggered": 0,
        }],
        columns=[
            "symbol", "current_sl", "stop_loss_price", "initial_tp",
            "trailing_stop_price", "sl_broker_order_id", "tp_broker_order_id",
            "stop_loss_triggered", "trailing_stop_triggered",
        ],
    )
    rows = asyncio.run(_parse_positions(ds, _position_raw()))
    assert len(rows) == 1
    row = rows[0]
    assert row["stop_loss_price"] == 95.0
    assert row["current_sl"] == 95.0
    assert row["initial_stop_loss_price"] == 90.0
    assert row["take_profit_price"] == 110.0
    assert row["protection_state"] == "broker_attached"
    assert row["broker_sl_attached"] is True


def test_parse_positions_marks_db_only_and_unknown_protection():
    columns = ["symbol", "current_sl", "stop_loss_price", "initial_tp",
               "sl_broker_order_id", "tp_broker_order_id"]
    ds = _FakePositionDS(
        protection_rows=[{
            "symbol": "US.AAPL", "current_sl": 94.0, "stop_loss_price": 90.0,
            "initial_tp": 110.0, "sl_broker_order_id": None,
            "tp_broker_order_id": None,
        }],
        columns=columns,
    )
    row = asyncio.run(_parse_positions(ds, _position_raw()))[0]
    assert row["protection_state"] == "db_only"

    ds_unknown = _FakePositionDS(protection_rows=[], columns=[])
    row_unknown = asyncio.run(_parse_positions(ds_unknown, _position_raw()))[0]
    assert row_unknown["protection_state"] == "unknown"


def test_parse_positions_unrelated_columns_do_not_claim_protection_known():
    """只有触发标志、没有止损来源列时，必须保持 unknown，不能误报“无保护”。"""
    ds = _FakePositionDS(
        protection_rows=[],
        columns=["symbol", "stop_loss_triggered", "trailing_stop_triggered"],
    )
    row = asyncio.run(_parse_positions(ds, _position_raw()))[0]
    assert row["protection_state"] == "unknown"


def test_today_orders_with_status_keeps_source_on_disconnect(tmp_path, monkeypatch):
    from app.web_dashboard.order import executor

    db_path = tmp_path / "orders.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE manual_order_audit (timestamp TEXT, symbol TEXT, quantity INTEGER, "
        "price REAL, order_type TEXT, status TEXT, action TEXT, order_id TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.core.db_utils.get_dashboard_db", lambda: db_path)

    class _Svc:
        def connect(self):
            return False

    meta = executor.get_today_orders_with_status(_Svc())
    assert meta["orders"] == []
    assert meta["broker_ok"] is False
    assert meta["source"] == "none"
    assert meta["error"] == "broker_not_connected"
    assert meta["as_of"]


def test_trade_ui_consumes_runtime_and_keeps_action_guards():
    html = (ROOT / "app/web_dashboard/templates/trade.html").read_text(encoding="utf-8")
    js = (ROOT / "app/web_dashboard/static/js/trade.js").read_text(encoding="utf-8")
    assert 'id="runtimeBanner"' in html
    assert ".runtime-banner.danger" in html
    assert "trade.js?v=20261025b" in html
    for token in ("applyRuntimeState", "showRuntimeUnavailable", "_runtimeGuard",
                  "caps.place", "caps.reduce", "_runtimeCan('cancel')"):
        assert token in js, token
    assert "if (!_runtimeGuard('place', false)) return;" in js
    assert "if (!_runtimeGuard('reduce', true)) return;" in js
    assert "if (!_runtimeGuard('cancel', false)) return;" in js
    assert "brokerOffline" in js
    assert "freshEl.textContent = brokerOffline ? '离线'" in js
