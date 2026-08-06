"""intraday_reentry.evaluate_candidate 纯函数单测 (2026-08-06)."""
from app.playbook.intraday_reentry import _DEFAULTS, evaluate_candidate


def _cfg(**over):
    c = dict(_DEFAULTS)
    c.update(over)
    return c


VWAP = 100.0
# LONG 回踩带: dev ∈ [-0.3%, +1.0%] → price ∈ [99.7, 101.0]
# SHORT 回踩带: dev ∈ [-1.0%, +0.3%] → price ∈ [99.0, 100.3]
GOOD = dict(slope_5m=0.2, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0)


def test_long_pass_in_band_turned_up():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **GOOD)
    assert ok, reasons


def test_long_pass_at_band_edges():
    for price in (99.7, 101.0):
        ok, reasons = evaluate_candidate("BUY", price, VWAP, cfg=_cfg(), **GOOD)
        assert ok, (price, reasons)


def test_long_reject_extended_above_band():
    ok, reasons = evaluate_candidate("BUY", 101.5, VWAP, cfg=_cfg(), **GOOD)
    assert not ok and any("vwap_dev" in r for r in reasons)


def test_long_reject_broken_below_band():
    ok, reasons = evaluate_candidate("BUY", 99.5, VWAP, cfg=_cfg(), **GOOD)
    assert not ok and any("vwap_dev" in r for r in reasons)


def test_long_reject_slope_not_turned():
    for slope in (0.0, -0.3):
        ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": slope})
        assert not ok and any("slope_5m" in r for r in reasons)


def test_short_pass_symmetric():
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 99.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": -0.2})
    assert ok, reasons


def test_short_reject_slope_up():
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 99.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": 0.1})
    assert not ok and any("slope_5m" in r for r in reasons)


def test_short_reject_above_band():
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": -0.2})
    assert not ok and any("vwap_dev" in r for r in reasons)


def test_missing_indicators_reject_no_failopen():
    for kwargs in (dict(slope_5m=None), dict(atr_pct=None), dict(vol_ratio=None), dict(spread_bps=None)):
        merged = {**GOOD, **kwargs}
        ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **merged)
        assert not ok, kwargs
        assert reasons


def test_wide_spread_reject():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "spread_bps": 40.0})
    assert not ok and any("spread_bps" in r for r in reasons)


def test_low_atr_reject():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "atr_pct": 1.0})
    assert not ok and any("atr_pct" in r for r in reasons)


def test_low_vol_ratio_reject():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "vol_ratio": 0.5})
    assert not ok and any("vol_ratio" in r for r in reasons)


def test_zero_price_or_vwap_reject():
    for price, vwap in ((0, VWAP), (100.0, 0)):
        ok, reasons = evaluate_candidate("BUY", price, vwap, cfg=_cfg(), **GOOD)
        assert not ok and reasons == ["no_price_or_vwap"]


# ── overnight_loser_exempt (2026-08-06 用户裁决: 再入场水下仓首夜豁免 EOD) ──

from datetime import timedelta

import app.playbook.intraday_reentry as ir


def _cfg_on(**over):
    return _cfg(overnight_hold_loser_enabled=True, **over)


def _today_entry():
    from app.utils.timezone import now_et
    return now_et().replace(hour=11, minute=30).isoformat()


def _yesterday_entry():
    from app.utils.timezone import now_et
    return (now_et() - timedelta(days=1)).isoformat()


YES = lambda sym, date: True   # noqa: E731
NO = lambda sym, date: False   # noqa: E731


def test_overnight_exempt_positive(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    assert ir.overnight_loser_exempt("AVGO", _today_entry(), -0.8, is_reentry=YES)


def test_overnight_flag_disabled(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg)  # _DEFAULTS: False
    assert not ir.overnight_loser_exempt("AVGO", _today_entry(), -0.8, is_reentry=YES)


def test_overnight_profitable_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    for pnl in (0.0, 0.5, 2.3):
        assert not ir.overnight_loser_exempt("AVGO", _today_entry(), pnl, is_reentry=YES)


def test_overnight_second_night_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    assert not ir.overnight_loser_exempt("AVGO", _yesterday_entry(), -0.8, is_reentry=YES)


def test_overnight_non_reentry_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    assert not ir.overnight_loser_exempt("AVGO", _today_entry(), -0.8, is_reentry=NO)


def test_overnight_bad_timestamp_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    for bad in ("", "not-a-date", None):
        assert not ir.overnight_loser_exempt("AVGO", bad, -0.8, is_reentry=YES)


def test_overnight_naive_timestamp_treated_as_et(monkeypatch):
    # trade_records 历史行为: entry_time 可能无时区 — 按 ET 当日处理不豁免错位
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    from app.utils.timezone import now_et
    naive_today = now_et().replace(hour=11, minute=30, tzinfo=None).isoformat()
    assert ir.overnight_loser_exempt("AVGO", naive_today, -0.8, is_reentry=YES)
