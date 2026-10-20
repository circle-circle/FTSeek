"""RFC-155 P2 风险尺寸分层影子契约: 旗标/乘数/回撤/美元口径聚合。"""
from __future__ import annotations

import scripts.risk_size_tier_shadow as rs


def _row(**overrides):
    row = {
        "id": 1, "symbol": "US.TEST", "direction": "LONG",
        "trade_date": "2026-10-01", "source": "DA",
        "atr_pct": 6.0, "session_min": 5, "pnl": -100.0,
        "notional": 5000.0, "left_tail": True,
    }
    row.update(overrides)
    return row


def test_flags_and_multipliers():
    flags = rs.flags_for(_row())
    assert flags == {"atr5": True, "open15": True, "ue": False}
    assert rs.multiplier("atr5", flags) == 0.5
    assert rs.multiplier("open15", flags) == 0.5
    assert rs.multiplier("ue", flags) == 1.0
    assert rs.multiplier("ge2", flags) == 0.5
    assert rs.multiplier("any", flags) == 0.5
    assert rs.multiplier("product", flags) == 0.25
    quiet = rs.flags_for(_row(atr_pct=2.0, session_min=60, source="DA"))
    assert rs.multiplier("any", quiet) == 1.0
    assert rs.multiplier("ge2", quiet) == 1.0


def test_max_drawdown_daily_equity():
    daily = {"2026-10-01": 100.0, "2026-10-02": -250.0, "2026-10-03": 50.0}
    # 100 -> -150 -> -100; peak 100; worst dd 250
    assert rs.max_drawdown(daily) == 250.0


def test_metrics_scales_loss_and_reports_tradeoff():
    rows = [
        _row(id=1, pnl=-100.0, left_tail=True, atr_pct=6.0, session_min=5),
        _row(id=2, pnl=80.0, left_tail=False, atr_pct=6.0, session_min=60),
        _row(id=3, pnl=40.0, left_tail=False, atr_pct=2.0, session_min=60),
        _row(id=4, pnl=-40.0, left_tail=False, atr_pct=2.0, session_min=60),
    ]
    out = rs.metrics(rows, "ge2")
    # rows 1 (2 flags) and 2 (1 flag) -> only row 1 qualifies for ge2 (open+atr? row2 atr only)
    assert out["affected_n"] == 1
    assert out["delta_net"] == 50.0            # -100 * 0.5 saved
    assert out["loss_avoided"] == 50.0
    assert out["upside_given_up"] == 0.0
    assert out["left_tail_actual_net"] == -100.0
    assert out["left_tail_cf_net"] == -50.0
    assert out["actual_net"] == -20.0 and out["cf_net"] == 30.0


def test_metrics_product_gives_up_winner_upside():
    rows = [
        _row(id=1, pnl=100.0, atr_pct=6.0, session_min=5, source="UE"),
        _row(id=2, pnl=-100.0, atr_pct=2.0, session_min=60, source="DA"),
    ]
    out = rs.metrics(rows, "product")
    assert out["affected_n"] == 1
    assert out["upside_given_up"] == 75.0     # 3 flags -> floor 0.25x
    assert out["loss_avoided"] == 0.0
    assert out["delta_net"] == -75.0


def test_half_split():
    rows = [_row(id=i, trade_date=f"2026-10-{i:02d}") for i in range(1, 5)]
    first, second = rs._half_split(rows)
    assert len(first) == 2 and len(second) == 2
    assert {r["trade_date"] for r in first} == {"2026-10-01", "2026-10-02"}
