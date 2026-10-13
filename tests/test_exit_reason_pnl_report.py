"""Exit reason reporting must use net PnL and canonical reason labels."""
from scripts.exit_reason_pnl_report import _aggregate, _compute_net_pnl_pct


def _trade(**overrides):
    trade = {
        "exit_reason": "breakeven_lock",
        "direction": "LONG",
        "realized_pnl": 136.99,
        "fees_est": 451.26,
        "pnl_net": -314.27,
        "pnl_pct": 0.0278,
        "net_pnl_pct": -0.0637,
        "entry_price": 0.072,
        "quantity": 68493,
        "entry_time": "2026-09-17T08:00:00-04:00",
        "exit_time": "2026-09-17T08:06:00-04:00",
        "holding_hours": 0.1,
        "symbol": "US.CTNT",
        "source_type": "PLAYBOOK_SIM",
        "trade_date": "2026-09-17",
    }
    trade.update(overrides)
    return trade


def test_report_aggregates_net_pnl_and_canonicalizes_legacy_reason():
    stats = _aggregate([_trade()])

    assert set(stats) == {"SL_BREAKEVEN"}
    result = stats["SL_BREAKEVEN"]
    assert result["n"] == 1
    assert result["win_rate"] == 0.0
    assert result["avg_pnl_pct"] == -0.0637
    assert result["avg_gross_pnl_pct"] == 0.0278
    assert result["gross_pnl_usd"] == 136.99
    assert result["fees_usd"] == 451.26
    assert result["net_pnl_usd"] == -314.27


def test_net_pct_falls_back_to_pnl_net_over_notional():
    trade = _trade()
    trade.pop("net_pnl_pct")

    assert _compute_net_pnl_pct(trade) == -314.27 / (68493 * 0.072)
