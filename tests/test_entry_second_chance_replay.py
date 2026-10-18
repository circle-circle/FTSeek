"""二次限价挂单回放的纯函数契约 (只读)."""
from datetime import datetime, timedelta

import scripts.entry_second_chance_replay as m

T0 = datetime(2026, 10, 16, 9, 40)


def _bar(minute, o, h, l, c):
    return (T0 + timedelta(minutes=minute), o, h, l, c)


def test_limit_price_direction():
    assert abs(m._limit_price("LONG", 100.0, 0.003) - 99.7) < 1e-9
    assert abs(m._limit_price("SHORT", 100.0, 0.003) - 100.3) < 1e-9


def test_retrace_frac_atr_and_clamp():
    assert abs(m._retrace_frac("atr0.2", 0.04) - 0.008) < 1e-12
    assert m._retrace_frac("atr0.2", 0.0001) == 0.002  # 下限
    assert m._retrace_frac("atr0.3", 0.50) == 0.03     # 上限


def test_fill_long_uses_limit_or_better_open():
    bars = [_bar(1, 100.0, 100.2, 99.8, 100.0),
            _bar(2, 99.5, 99.6, 99.2, 99.4)]
    filled = m._simulate_fill(bars, T0, T0 + timedelta(minutes=5), "LONG", 99.5)
    assert filled == (99.5, T0 + timedelta(minutes=2))


def test_fill_short_uses_limit_or_better_open():
    bars = [_bar(1, 100.0, 100.2, 99.8, 100.0),
            _bar(2, 101.0, 101.4, 100.8, 101.2)]
    filled = m._simulate_fill(bars, T0, T0 + timedelta(minutes=5), "SHORT", 100.5)
    assert filled == (101.0, T0 + timedelta(minutes=2))


def test_no_fill_when_limit_untouched():
    bars = [_bar(1, 100.0, 100.2, 99.8, 100.0)]
    assert m._simulate_fill(bars, T0, T0 + timedelta(minutes=5), "LONG", 99.0) is None


def test_exit_hits_stop_loss_first():
    bars = [_bar(1, 100.0, 100.2, 98.0, 100.0)]
    out = m._simulate_exit(bars, T0, "LONG", 100.0, 99.0, 105.0, 60)
    assert out["reason"] in ("SL", "SL_GAP")
    assert out["exit"] <= 99.0


def test_exit_hits_take_profit():
    bars = [_bar(1, 100.0, 106.0, 99.5, 105.0)]
    out = m._simulate_exit(bars, T0, "LONG", 100.0, 90.0, 105.0, 60)
    assert out["reason"] == "TP"
    assert out["exit"] == 105.0
