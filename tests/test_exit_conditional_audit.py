"""RFC-154 P0 出场条件审计的纯函数/口径契约。

钉住:
  ① trail 模拟方向对称, 且先判止损再更新峰值 (同 bar 不用未来信息);
  ② delta 方向归一 (正=反事实更好);
  ③ 止损类出场 (SL_INITIAL/OVERNIGHT_GAP_STOP/SL_FIXED_PCT) 必须从 trail 结论剔除,
     单列 stop_like_reversal — 否则止损后反转会把均值抬到 +30bps 级别;
  ④ 分桶/日聚类边界。
"""
from __future__ import annotations

from datetime import datetime

import scripts.exit_conditional_audit as audit


def _bar(ts, o, h, l, c):
    return [ts, o, h, l, c]


def test_simulate_trail_long_stops_on_retrace():
    bars = [
        _bar("2026-10-20 09:48:00", 100, 102, 100.5, 101),
        _bar("2026-10-20 09:49:00", 101, 102.5, 100.5, 101),
        _bar("2026-10-20 09:50:00", 101, 103, 99, 100),
    ]
    # peak=102 (pre-exit), 0.8% trail => 101.184; 第二根 low 100.5 触发
    price, reason = audit._simulate_trail(bars, "LONG", 100.0, 102.0, 0.8)
    assert reason == "trail"
    assert price == 102.0 * (1 - 0.008)


def test_simulate_trail_short_symmetric():
    bars = [
        _bar("2026-10-20 09:48:00", 100, 99.5, 98, 99),
        _bar("2026-10-20 09:49:00", 99, 99.5, 97.5, 98),
    ]
    price, reason = audit._simulate_trail(bars, "SHORT", 100.0, 98.0, 0.8)
    assert reason == "trail"
    assert price == 98.0 * (1 + 0.008)


def test_simulate_trail_holds_to_close_when_never_stopped():
    bars = [_bar("2026-10-20 09:48:00", 100, 100.5, 99.8, 100.2)]
    price, reason = audit._simulate_trail(bars, "LONG", 100.0, 100.0, 0.8)
    assert reason == "close" and price == 100.2


def test_delta_bps_direction_normalized():
    assert audit._delta_bps("LONG", 100.0, 101.0) == 100.0
    assert audit._delta_bps("SHORT", 100.0, 99.0) == 100.0
    assert audit._delta_bps("LONG", 100.0, 99.0) == -100.0
    assert audit._delta_bps("SHORT", 100.0, 101.0) == -100.0


def test_buckets():
    assert audit._mfe_bucket(0.2) == "<0.3%"
    assert audit._mfe_bucket(0.4) == "0.3-0.6%"
    assert audit._mfe_bucket(0.8) == "0.6-1.0%"
    assert audit._mfe_bucket(1.2) == ">=1.0%"
    assert audit._atr_bucket(2.9) == "<3%"
    assert audit._atr_bucket(3.0) == "3-5%"
    assert audit._atr_bucket(5.0) == "5-8%"
    assert audit._atr_bucket(8.0) == ">=8%"


def test_day_clustered_requires_three_days():
    assert audit._day_clustered([("2026-10-01", 1.0), ("2026-10-02", 2.0)]) is None
    out = audit._day_clustered([("2026-10-01", 1.0), ("2026-10-02", 2.0),
                                ("2026-10-03", 3.0)])
    assert out and out["n_days"] == 3


def test_stop_like_excluded_from_trail_and_reported_separately(monkeypatch):
    bars = [_bar("2026-10-20 09:48:00", 100, 101, 99.8, 100.5)]
    monkeypatch.setattr(audit, "_load_bars", lambda symbol, date: bars)
    rows = [
        {"type": "post_exit", "status": "ok", "trade_id": 1, "symbol": "US.TEST1",
         "date": "2026-10-20", "exit_min": "09:47", "exit_px": 100.0,
         "exit_reason": "SL_BREAKEVEN", "close_gap_pct": 2.0},
        {"type": "post_exit", "status": "ok", "trade_id": 2, "symbol": "US.TEST2",
         "date": "2026-10-20", "exit_min": "09:47", "exit_px": 100.0,
         "exit_reason": "SL_INITIAL", "close_gap_pct": 2.0},
    ]
    context = {
        1: {"symbol": "US.TEST1", "direction": "LONG", "entry": 100.0, "exit": 100.0,
            "mfe_pct": 0.8, "atr_pct": 4.0, "peak_price": 101.0, "archetype": "PULLBACK_LIMIT"},
        2: {"symbol": "US.TEST2", "direction": "LONG", "entry": 100.0, "exit": 100.0,
            "mfe_pct": 0.0, "atr_pct": 4.0, "peak_price": 100.0, "archetype": "BREAKOUT"},
    }
    report = audit.compute(rows, context)
    assert report["coverage"]["valid_trail_rows"] == 1
    assert report["coverage"]["stop_like_rows"] == 1
    assert report["variants"]["trail_0.8"]["n"] == 1
    assert report["stop_like_reversal"]["trail_0.8"]["n"] == 1
    assert report["premature_exit_subset"]["trail_0.8"]["n"] == 1
