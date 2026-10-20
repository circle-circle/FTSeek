"""BE 宽限窗重放核心: 宽限内只认初始 SL, 宽限后才认净保本线。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.be_grace_replay import simulate_grace  # noqa: E402
from scripts.breakeven_shadow_settle import scale_out_delta  # noqa: E402


def _bars(points):
    base = datetime(2026, 10, 19, 10, 0)
    return [(base + timedelta(minutes=minute), price) for minute, price in points]


def test_long_grace_holds_through_pullback_and_captures_continuation():
    cf_net, exit_px, _ = simulate_grace(
        _bars([(1, 100.5), (5, 100.2), (11, 101.0), (359, 102.0)]),
        datetime(2026, 10, 19, 10, 0), 10, "LONG", 100.0, 10, 4.0, 96.0, 100.1,
    )
    assert exit_px == 102.0
    assert cf_net == 16.0


def test_long_grace_still_respects_initial_sl():
    cf_net, exit_px, _ = simulate_grace(
        _bars([(1, 100.5), (5, 95.5), (11, 101.0)]),
        datetime(2026, 10, 19, 10, 0), 10, "LONG", 100.0, 10, 4.0, 96.0, 100.1,
    )
    assert exit_px == 95.5
    assert cf_net == -49.0


def test_short_grace_still_respects_initial_sl():
    cf_net, exit_px, _ = simulate_grace(
        _bars([(1, 99.5), (5, 104.5), (11, 98.0)]),
        datetime(2026, 10, 19, 10, 0), 10, "SHORT", 100.0, 10, 4.0, 104.0, 99.9,
    )
    assert exit_px == 104.5
    assert cf_net == -49.0


def test_scale_out_delta_is_direction_aware():
    assert scale_out_delta(100.0, 10, "LONG", 101.0, 100.5) == 2.5
    assert scale_out_delta(100.0, 10, "SHORT", 99.0, 99.5) == 2.5
    assert scale_out_delta(100.0, 10, "LONG", 100.5, 101.0) == -2.5
