import pytest

from app.core import trade_identity
from app.core.trade_identity import (
    ensure_test_symbol_not_in_production_db,
    is_test_symbol,
    summarize_trade_identity,
    trade_identity as classify,
)
from app.storage.trade_store import TradeStore


def test_trade_identity_distinguishes_test_sim_real_and_phase():
    assert classify("US.TEST", "PLAYBOOK_SIM") == {"trade_env": "test"}
    assert classify("US.AAPL", "PLAYBOOK_SIM", session="premarket") == {
        "trade_env": "sim",
        "trade_phase": "premarket",
    }
    assert classify("US.AAPL", "PLAYBOOK_LIVE", session="after-hours") == {
        "trade_env": "real",
        "trade_phase": "afterhours",
    }
    assert classify("US.AAPL", "MANUAL") == {"trade_env": "unknown"}


def test_identity_summary_groups_existing_fields_without_schema_change():
    rows = [
        {"symbol": "US.AAPL", "source_type": "PLAYBOOK_SIM", "trade_mode": "simulate",
         "entry_session": "premarket", "pnl_net": 10.0},
        {"symbol": "US.MSFT", "source_type": "PLAYBOOK_SIM", "trade_mode": "simulate",
         "entry_session": "premarket", "pnl_net": -2.0},
        {"symbol": "US.NVDA", "source_type": "PLAYBOOK_LIVE", "trade_mode": "live",
         "metadata_json": '{"session":"afterhours"}', "realized_pnl": 5.0},
    ]
    summary = summarize_trade_identity(rows)
    assert summary[0] == {
        "trade_env": "sim", "source_type": "PLAYBOOK_SIM", "trade_phase": "premarket",
        "n": 2, "wins": 1, "pnl": 8.0,
    }
    assert summary[1]["trade_env"] == "real"
    assert summary[1]["trade_phase"] == "afterhours"


@pytest.mark.parametrize("symbol", ["TEST", "US.TEST", "US.FAKE.TEST"])
def test_test_symbol_detection(symbol):
    assert is_test_symbol(symbol)


def test_test_symbol_is_allowed_in_test_db(tmp_path):
    ensure_test_symbol_not_in_production_db("US.TEST", tmp_path / "test.db")


def test_trade_store_rejects_test_symbol_in_production_db(tmp_path, monkeypatch):
    production_dir = tmp_path / "data"
    production_dir.mkdir()
    monkeypatch.setattr(trade_identity, "_PROJECT_DATA_DIR", production_dir)
    store = TradeStore(str(production_dir / "ftseek_us.db"))

    with pytest.raises(ValueError, match="cannot be written to production DB"):
        store.save_trade({"symbol": "US.TEST"})
