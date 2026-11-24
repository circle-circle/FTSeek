"""RFC-216: VIX 20–22 连续计分与旧规则回滚契约。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.playbook.market_regime import (
    MarketRegime,
    MarketRegimeGate,
    vix_score,
)


@pytest.mark.parametrize("vix,expected", [
    (14.99, 3), (15.0, 1), (20.0, 1), (20.01, -2),
    (21.99, -2), (22.0, -2), (29.99, -2), (30.0, -5),
])
def test_legacy_vix_ladder_is_unchanged_when_disabled(vix, expected):
    assert vix_score(vix, smooth_20_22=False) == expected


@pytest.mark.parametrize("vix,expected", [
    (20.0, 1.0), (20.26, 0.61), (21.0, -0.5), (21.99, -1.985), (22.0, -2.0),
])
def test_smoothing_is_continuous_at_both_boundaries(vix, expected):
    assert vix_score(vix, smooth_20_22=True) == pytest.approx(expected)


@pytest.mark.parametrize("vix", [14.0, 15.0, 20.0, 22.0, 25.0, 30.0])
def test_outside_open_band_matches_legacy_exactly(vix):
    assert vix_score(vix, smooth_20_22=True) == vix_score(vix, smooth_20_22=False)


def _gate(vix):
    router = Mock()
    router.get_spy_context.return_value = {
        "spy_price": 716.20,
        "spy_ma20": 742.98,
        "spy_ma50": 758.51,
        "spy_ma200": 730.61,
        "spx_change_percent": -1.0,
    }
    router.get_vix_or_none.return_value = vix
    router.get_quote.return_value = {}
    av = Mock()
    av.get_spy_context.return_value = {}
    av.get_vix_or_none.return_value = None
    return MarketRegimeGate(router, av)


def _enable_smoothing(monkeypatch, enabled):
    monkeypatch.setattr(
        "app.playbook.config.get_playbook_config",
        lambda: SimpleNamespace(get=lambda key, default=None: enabled),
    )
    MarketRegimeGate._assess_cache = None


def test_current_boundary_case_moves_from_no_trade_to_risk_off(monkeypatch):
    _enable_smoothing(monkeypatch, True)
    ctx = _gate(20.26).assess()
    # Other components: -2 (SPY<MA20) -2 (MA20<MA50) -1 (SPY<MA200) = -5.
    # VIX component: 1 - 1.5×0.26 = +0.61 ⇒ total -4.39.
    assert ctx.score == pytest.approx(-4.39)
    assert ctx.regime == MarketRegime.RISK_OFF
    assert ctx.position_multiplier == 0.3


def test_rollback_restores_legacy_no_trade_and_integer_score(monkeypatch):
    _enable_smoothing(monkeypatch, False)
    ctx = _gate(20.26).assess()
    assert ctx.score == -7
    assert isinstance(ctx.score, int)
    assert ctx.regime == MarketRegime.NO_TRADE
    assert ctx.position_multiplier == 0.0


def test_vix_22_keeps_legacy_no_trade_even_with_smoothing(monkeypatch):
    _enable_smoothing(monkeypatch, True)
    ctx = _gate(22.0).assess()
    assert ctx.score == -7
    assert ctx.regime == MarketRegime.NO_TRADE
