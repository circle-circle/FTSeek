"""session_attribution 的盈亏结构与入场时段口径契约。"""
from __future__ import annotations

import scripts.session_attribution as sa


def _row(**overrides):
    row = {
        "id": 1, "symbol": "US.AAPL", "trade_date": "2026-11-10",
        "strategy_id": "playbook_sim", "entry_time": "2026-11-10T09:40:00-05:00",
        "entry_price": 100.0, "quantity": 100, "realized_pnl": 22.0,
        "pnl_net": 18.0, "fees_est": 4.0, "exit_reason": "SL_BREAKEVEN",
        "mfe_pct": 0.5, "metadata_json": "{}",
    }
    row.update(overrides)
    return row


def test_entry_session_bucket_boundaries():
    assert sa.entry_session_bucket("2026-11-10T08:30:00-05:00") == "pre_open"
    assert sa.entry_session_bucket("2026-11-10T09:35:00-05:00") == "open_0_15"
    assert sa.entry_session_bucket("2026-11-10T09:44:59-05:00") == "open_0_15"
    assert sa.entry_session_bucket("2026-11-10T09:45:00-05:00") == "open_15_60"
    assert sa.entry_session_bucket("2026-11-10T10:29:59-05:00") == "open_15_60"
    assert sa.entry_session_bucket("2026-11-10T10:30:00-05:00") == "after_60"
    assert sa.entry_session_bucket(None) == "unknown"


def test_pnl_structure_reports_payoff_and_breakeven_win_rate():
    rows = [
        _row(),
        _row(realized_pnl=-66.0, pnl_net=-70.0),
        _row(realized_pnl=86.0, pnl_net=82.0),
    ]
    report = sa.pnl_structure(rows)
    assert report["n"] == 3
    assert report["win_rate_pct"] == 66.67
    assert report["gross"] == 42.0
    assert report["fees"] == 12.0
    assert report["net"] == 30.0
    assert report["net_bps"] == 10.0
    assert report["expectancy"] == 10.0
    assert report["avg_win"] == 50.0
    assert report["avg_loss"] == -70.0
    assert report["payoff"] == 0.714
    assert report["breakeven_win_rate_pct"] == 58.33
    assert report["fee_to_gross_pct"] == 28.57


def test_pnl_structure_null_net_falls_back_to_gross_minus_fees():
    report = sa.pnl_structure([_row(pnl_net=None, realized_pnl=10.0, fees_est=2.0)])
    assert report["net"] == 8.0 and report["expectancy"] == 8.0


def test_segment_i_separates_da_ext_time_and_stop_family(monkeypatch):
    captured = {}

    def fake_rows(sql, args=()):
        captured["sql"] = sql
        captured["args"] = args
        return [
            _row(id=1, symbol="US.RAW", pnl_net=-100.0, realized_pnl=-96.0,
                 exit_reason="SL_INITIAL"),
            _row(id=2, symbol="US.TIGHT", pnl_net=-50.0, realized_pnl=-46.0,
                 exit_reason="SL_INITIAL", metadata_json='{"_tight_sl_killed": true}'),
            _row(id=3, symbol="US.LATE", entry_time="2026-11-10T09:45:00-05:00",
                 pnl_net=30.0, realized_pnl=34.0),
            _row(id=4, symbol="US.EXT", strategy_id="extended_hours_entry",
                 entry_time="2026-11-10T08:30:00-05:00", pnl_net=20.0, realized_pnl=24.0),
        ]

    monkeypatch.setattr(sa, "_rows", fake_rows)
    report = sa.segment_i("2026-11-10")
    assert captured["args"] == ("2026-11-01", "2026-11-10")
    assert "exclusion_reason" in captured["sql"]
    assert "data_quality IN ('clean','real')" in captured["sql"]
    assert report["overall"]["playbook_sim"]["net"] == -120.0
    assert report["overall"]["extended_hours_entry"]["net"] == 20.0
    assert report["da_entry_buckets"][0]["bucket"] == "open_0_15"
    assert report["da_entry_buckets"][0]["sl_initial_n"] == 2
    assert report["da_sl_initial"]["raw_initial"]["net"] == -100.0
    assert report["da_sl_initial"]["early_tight"]["net"] == -50.0
    assert report["da_sl_initial"]["early_tight"]["open_0_15_n"] == 1
    assert report["decision_use"] == "display_only_not_a_trading_gate"
