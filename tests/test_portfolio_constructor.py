"""
Unit tests for PortfolioConstructor — Collect-Rank-Allocate funnel.

Tests: candidate collection, composite scoring, strategy inference,
direction balance scoring, risk budget allocation, edge cases.
"""

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from typing import Optional

from app.core.models import SignalScore, Direction, MarketRegime


# ── Helpers ──

def _make_signal(symbol="AAPL", direction=Direction.LONG, strength=70,
                 confidence=0.70, strategy_id="voting_llm"):
    return SignalScore(
        symbol=symbol, direction=direction, strength=strength,
        confidence=confidence, strategy_id=strategy_id,
    )


def _make_context(price=150.0, rsi=55.0, macd=0.5, bb_position=0.5,
                  atr=3.0, vix=20.0):
    return {
        "price": price, "rsi": rsi, "macd": macd,
        "bb_position": bb_position, "atr": atr, "vix": vix,
    }


def _make_mock_capital_safety(equity=100000.0, water_level="green",
                               risk_multiplier=1.0, position_multiplier=1.0):
    cs = MagicMock()
    state = MagicMock()
    state.current_equity = equity
    state.water_level.value = water_level
    cs._state = state
    cs.config.max_risk_per_trade_pct = 0.035
    cs.config.max_total_risk_pct = 0.12
    cs.get_risk_multiplier.return_value = risk_multiplier
    cs.get_position_multiplier.return_value = position_multiplier

    def calc_position_size(entry_price, stop_loss_price, max_total_position_value=None):
        risk_per_share = abs(entry_price - stop_loss_price)
        risk_base = equity
        risk_amount = risk_base * 0.035 * risk_multiplier
        size = max(0, int(risk_amount / risk_per_share))
        return {"size": size, "risk_amount": risk_amount, "risk_per_share": risk_per_share,
                "reason": f"test size={size}"}

    cs.calc_position_size = calc_position_size
    return cs


@dataclass
class MockPosition:
    symbol: str = "AAPL"
    direction: Direction = Direction.LONG
    size: float = 10.0
    avg_entry_price: float = 150.0
    current_sl: float = 135.0


# ═══════════════════════════════════════════════
# Test 1: Candidate Collection
# ═══════════════════════════════════════════════

class TestCandidateCollection:
    def test_add_single_candidate(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        signal = _make_signal()
        ctx = _make_context()
        pc.add_candidate("AAPL", signal, ctx)
        assert pc.candidate_count == 1

    def test_add_multiple_candidates(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        for sym in ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]:
            pc.add_candidate(sym, _make_signal(symbol=sym), _make_context())
        assert pc.candidate_count == 5

    def test_reset_clears_candidates(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("AAPL", _make_signal(), _make_context())
        pc.reset()
        assert pc.candidate_count == 0

    def test_candidates_have_unique_symbols(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("AAPL", _make_signal(symbol="AAPL"), _make_context())
        pc.add_candidate("GOOGL", _make_signal(symbol="GOOGL"), _make_context())
        symbols = [c.symbol for c in pc.get_all_candidates()]
        assert symbols == ["AAPL", "GOOGL"]


# ═══════════════════════════════════════════════
# Test 2: Strategy Type Inference
# ═══════════════════════════════════════════════

class TestStrategyInference:
    def test_infer_breakout(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        c = Candidate(symbol="TSLA", signal=_make_signal(),
                      context=_make_context(rsi=85, bb_position=0.95))
        assert PortfolioConstructor._infer_strategy_type(c) == "breakout"

    def test_infer_breakout_oversold(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        c = Candidate(symbol="TSLA", signal=_make_signal(),
                      context=_make_context(rsi=15, bb_position=0.05))
        assert PortfolioConstructor._infer_strategy_type(c) == "breakout"

    def test_infer_mean_reversion(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        c = Candidate(symbol="AAPL", signal=_make_signal(),
                      context=_make_context(rsi=70, bb_position=0.5))
        assert PortfolioConstructor._infer_strategy_type(c) == "mean_reversion"

    def test_infer_trend_following(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        c = Candidate(symbol="MSFT", signal=_make_signal(),
                      context=_make_context(rsi=55, macd=0.5))
        assert PortfolioConstructor._infer_strategy_type(c) == "trend_following"

    def test_infer_default_to_trend(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        c = Candidate(symbol="DEFAULT", signal=_make_signal(),
                      context={})
        assert PortfolioConstructor._infer_strategy_type(c) == "trend_following"


# ═══════════════════════════════════════════════
# Test 3: Direction Balance Scoring
# ═══════════════════════════════════════════════

class TestDirectionBalance:
    def test_long_signal_when_need_long(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        # exposure_gap > 0: need more LONG
        score = PortfolioConstructor._direction_balance_score(
            Direction.LONG, exposure_gap=0.3, total_exposure=0.3
        )
        assert score > 0.7  # strong bonus

    def test_long_signal_when_need_short(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        score = PortfolioConstructor._direction_balance_score(
            Direction.LONG, exposure_gap=-0.3, total_exposure=0.3
        )
        assert score < 0.3  # penalty

    def test_short_signal_when_need_short(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        score = PortfolioConstructor._direction_balance_score(
            Direction.SHORT, exposure_gap=-0.3, total_exposure=0.3
        )
        assert score > 0.7  # strong bonus

    def test_short_signal_when_need_long(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        score = PortfolioConstructor._direction_balance_score(
            Direction.SHORT, exposure_gap=0.3, total_exposure=0.3
        )
        assert score < 0.3  # penalty

    def test_neutral_gap_gives_mid_score(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        score = PortfolioConstructor._direction_balance_score(
            Direction.LONG, exposure_gap=0.0, total_exposure=0.0
        )
        assert 0.45 <= score <= 0.55

    def test_score_bounded_0_to_1(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        for gap in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            for d in [Direction.LONG, Direction.SHORT]:
                score = PortfolioConstructor._direction_balance_score(d, gap, 0.5)
                assert 0.0 <= score <= 1.0, f"gap={gap} dir={d} score={score}"


# ═══════════════════════════════════════════════
# Test 4: Composite Scoring
# ═══════════════════════════════════════════════

class TestCompositeScoring:
    def test_score_range_zero_to_one(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        pc = PortfolioConstructor()
        c = Candidate(symbol="AAPL", signal=_make_signal(strength=70, confidence=0.7),
                      context=_make_context())
        score = pc._compute_composite(c, "morning", exposure_gap=0.1, total_exposure=0.3)
        assert 0.0 <= score <= 1.0

    def test_high_strength_scores_higher(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        pc = PortfolioConstructor()
        c_high = Candidate(symbol="HIGH", signal=_make_signal(strength=90, confidence=0.8),
                           context=_make_context())
        c_low = Candidate(symbol="LOW", signal=_make_signal(strength=30, confidence=0.3),
                          context=_make_context())
        score_high = pc._compute_composite(c_high, "morning", 0.0, 0.3)
        score_low = pc._compute_composite(c_low, "morning", 0.0, 0.3)
        assert score_high > score_low

    def test_session_alignment_affects_score(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        pc = PortfolioConstructor()
        # mean_reversion candle in opening_drive (alignment=1.0) vs midday (alignment=0.7)
        c = Candidate(symbol="AAPL", signal=_make_signal(strength=70, confidence=0.7),
                      context=_make_context(rsi=70, bb_position=0.5))  # mean_reversion
        score_open = pc._compute_composite(c, "opening_drive", 0.0, 0.3)
        score_midday = pc._compute_composite(c, "midday", 0.0, 0.3)
        assert score_open > score_midday

    def test_scoring_is_ranked_descending(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        # Mix of strong and weak signals
        pc.add_candidate("STRONG", _make_signal(symbol="STRONG", strength=90, confidence=0.9),
                         _make_context(rsi=55, macd=1.0))
        pc.add_candidate("WEAK", _make_signal(symbol="WEAK", strength=30, confidence=0.3),
                         _make_context(rsi=50, macd=0.0))
        pc.add_candidate("MEDIUM", _make_signal(symbol="MEDIUM", strength=60, confidence=0.6),
                         _make_context(rsi=55, macd=0.5))

        pc.score_candidates("morning", "normal", current_net_exposure=0.0, current_total_exposure=0.2)
        scores = [c.composite_score for c in pc.get_all_candidates()]
        assert scores == sorted(scores, reverse=True), f"Not sorted descending: {scores}"

    def test_rank_numbers_are_sequential(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        for sym in ["A", "B", "C"]:
            pc.add_candidate(sym, _make_signal(symbol=sym, strength=50 + 10 * len(pc.get_all_candidates())),
                             _make_context())
        pc.score_candidates("morning", "normal", 0.0, 0.0)
        ranks = [c.rank for c in pc.get_all_candidates()]
        assert ranks == [1, 2, 3]


# ═══════════════════════════════════════════════
# Test 5: Risk Budget Allocation
# ═══════════════════════════════════════════════

class TestRiskBudgetAllocation:
    def test_allocate_within_budget(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        for sym in ["AAPL", "GOOGL", "MSFT"]:
            pc.add_candidate(sym, _make_signal(symbol=sym, strength=70, confidence=0.7),
                             _make_context(price=150.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)

        cs = _make_mock_capital_safety(equity=100000.0)
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=[])
        # All 3 should fit within budget and slots
        assert len(plan) == 3

    def test_respects_max_positions(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        for sym in ["A", "B", "C", "D", "E"]:
            pc.add_candidate(sym, _make_signal(symbol=sym, strength=70, confidence=0.7),
                             _make_context(price=150.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)

        cs = _make_mock_capital_safety(equity=100000.0)
        plan = pc.allocate_risk_budget(cs, max_positions=3, existing_positions=[])
        assert len(plan) <= 3

    def test_respects_existing_positions(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        for sym in ["AAPL", "GOOGL", "MSFT"]:
            pc.add_candidate(sym, _make_signal(symbol=sym, strength=70, confidence=0.7),
                             _make_context(price=150.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)

        cs = _make_mock_capital_safety(equity=100000.0)
        existing = [MockPosition(symbol="NVDA"), MockPosition(symbol="TSLA"),
                     MockPosition(symbol="META"), MockPosition(symbol="NFLX")]
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=existing)
        assert len(plan) <= 1  # only 1 slot left

    def test_no_slots_returns_empty(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("AAPL", _make_signal(), _make_context(price=150.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)

        cs = _make_mock_capital_safety(equity=100000.0)
        existing = [MockPosition() for _ in range(5)]
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=existing)
        assert len(plan) == 0

    def test_higher_scored_allocated_first(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("BEST", _make_signal(symbol="BEST", strength=95, confidence=0.95),
                         _make_context(price=150.0))
        pc.add_candidate("WORST", _make_signal(symbol="WORST", strength=30, confidence=0.3),
                         _make_context(price=150.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)

        cs = _make_mock_capital_safety(equity=100000.0)
        plan = pc.allocate_risk_budget(cs, max_positions=1, existing_positions=[])
        assert len(plan) == 1
        assert plan[0].symbol == "BEST"

    def test_short_signals_allocated_correctly(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("SHORT1", _make_signal(symbol="SHORT1", direction=Direction.SHORT,
                                                 strength=80, confidence=0.8),
                         _make_context(price=150.0))
        pc.score_candidates("afternoon", "trending_down", 0.3, 0.5)

        cs = _make_mock_capital_safety(equity=100000.0)
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=[])
        assert len(plan) == 1
        assert plan[0].signal.direction == Direction.SHORT


# ═══════════════════════════════════════════════
# Test 6: Regime Net Exposure Targets
# ═══════════════════════════════════════════════

class TestRegimeExposureTargets:
    def test_trending_up_prefers_long(self):
        from app.pipeline.portfolio_constructor import REGIME_NET_EXPOSURE_TARGET
        assert REGIME_NET_EXPOSURE_TARGET["trending_up"] > 0

    def test_trending_down_prefers_short(self):
        from app.pipeline.portfolio_constructor import REGIME_NET_EXPOSURE_TARGET
        assert REGIME_NET_EXPOSURE_TARGET["trending_down"] < 0

    def test_range_is_neutral(self):
        from app.pipeline.portfolio_constructor import REGIME_NET_EXPOSURE_TARGET
        assert REGIME_NET_EXPOSURE_TARGET["range"] == 0.0

    def test_all_regimes_have_targets(self):
        from app.pipeline.portfolio_constructor import REGIME_NET_EXPOSURE_TARGET
        for regime in ["trending_up", "trending_down", "range", "high_vol", "crisis", "normal"]:
            assert regime in REGIME_NET_EXPOSURE_TARGET

    def test_targets_in_range(self):
        from app.pipeline.portfolio_constructor import REGIME_NET_EXPOSURE_TARGET
        for target in REGIME_NET_EXPOSURE_TARGET.values():
            assert -1.0 <= target <= 1.0


# ═══════════════════════════════════════════════
# Test 7: Session Strategy Alignment Table
# ═══════════════════════════════════════════════

class TestSessionAlignmentTable:
    def test_all_sessions_defined(self):
        from app.pipeline.portfolio_constructor import SESSION_STRATEGY_ALIGNMENT
        for session in ["opening_drive", "morning", "midday", "afternoon", "closing_drive"]:
            assert session in SESSION_STRATEGY_ALIGNMENT, f"Missing {session}"

    def test_all_strategies_per_session(self):
        from app.pipeline.portfolio_constructor import SESSION_STRATEGY_ALIGNMENT
        for session, strategies in SESSION_STRATEGY_ALIGNMENT.items():
            for stype in ["trend_following", "mean_reversion", "breakout"]:
                assert stype in strategies, f"Missing {stype} in {session}"

    def test_alignment_values_in_range(self):
        from app.pipeline.portfolio_constructor import SESSION_STRATEGY_ALIGNMENT
        for strategies in SESSION_STRATEGY_ALIGNMENT.values():
            for val in strategies.values():
                assert 0.0 <= val <= 1.0

    def test_midday_lowest_overall(self):
        from app.pipeline.portfolio_constructor import SESSION_STRATEGY_ALIGNMENT
        # midday should have lowest average (low conviction in chop)
        averages = {}
        for session, strategies in SESSION_STRATEGY_ALIGNMENT.items():
            averages[session] = sum(strategies.values()) / len(strategies)
        assert averages["midday"] == min(averages.values())


# ═══════════════════════════════════════════════
# Test 8: Edge Cases
# ═══════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_candidates_allocation(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        cs = _make_mock_capital_safety()
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=[])
        assert plan == []

    def test_zero_price_skipped(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("ZERO", _make_signal(), _make_context(price=0.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)
        cs = _make_mock_capital_safety()
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=[])
        assert len(plan) == 0

    def test_negative_price_skipped(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.add_candidate("NEG", _make_signal(), _make_context(price=-10.0))
        pc.score_candidates("morning", "normal", 0.0, 0.2)
        cs = _make_mock_capital_safety()
        plan = pc.allocate_risk_budget(cs, max_positions=5, existing_positions=[])
        assert len(plan) == 0

    def test_extreme_rsi_handled(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        # RSI above 100 shouldn't crash
        c = Candidate(symbol="EXTREME", signal=_make_signal(),
                      context=_make_context(rsi=120, bb_position=5.0))
        stype = PortfolioConstructor._infer_strategy_type(c)
        assert stype in ("breakout", "mean_reversion", "trend_following")

    def test_score_with_unknown_session(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        pc = PortfolioConstructor()
        c = Candidate(symbol="AAPL", signal=_make_signal(strength=70, confidence=0.7),
                      context=_make_context())
        score = pc._compute_composite(c, "nonexistent_session", 0.0, 0.2)
        assert 0.0 <= score <= 1.0  # should not crash

    def test_score_with_extreme_exposure(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor, Candidate
        pc = PortfolioConstructor()
        c = Candidate(symbol="AAPL", signal=_make_signal(strength=70, confidence=0.7),
                      context=_make_context())
        score = pc._compute_composite(c, "morning", exposure_gap=2.0, total_exposure=2.0)
        assert 0.0 <= score <= 1.0  # should be clamped

    def test_allocated_risk_estimate_with_empty_positions(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        risk = PortfolioConstructor._estimate_allocated_risk([], 100000.0)
        assert risk == 0.0

    def test_allocated_risk_estimate_with_positions(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        positions = [MockPosition(size=10, avg_entry_price=150, current_sl=135)]
        risk = PortfolioConstructor._estimate_allocated_risk(positions, 100000.0)
        assert risk == 150.0  # 10 * (150 - 135)

    def test_direction_balance_with_full_exposure(self):
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        # Already at 80% total exposure; gap should still produce valid score
        score = PortfolioConstructor._direction_balance_score(
            Direction.LONG, exposure_gap=0.5, total_exposure=0.8
        )
        assert 0.0 <= score <= 1.0

    def test_multiple_resets_idempotent(self):
        from app.pipeline.portfolio_constructor import get_portfolio_constructor
        pc = get_portfolio_constructor()
        pc.reset()
        pc.reset()
        pc.reset()
        pc.add_candidate("AAPL", _make_signal(), _make_context())
        assert pc.candidate_count == 1
