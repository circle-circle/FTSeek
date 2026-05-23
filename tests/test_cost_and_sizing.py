"""
CostModel + PositionSizing 测试
"""
import pytest
from app.exit.cost_model import CostModel, CostEstimate
from app.exit.position_sizing import PositionSizing, SizingResult


class TestCostModel:
    def test_ibkr_defaults(self):
        m = CostModel.ibkr()
        assert m.commission_per_share == 0.005
        assert m.min_commission == 1.0
        assert m.spread_bps == 3.0
        assert m.slippage_atr_frac == 0.03

    def test_zero_cost(self):
        m = CostModel.zero()
        est = m.estimate(price=150.0, atr=3.0, shares=100)
        assert est.round_trip == 0.0
        assert est.commission == 0.0

    def test_estimate_round_trip(self):
        m = CostModel.ibkr()
        est = m.estimate(price=150.0, atr=3.0, shares=100)
        # commission: max(1, 0.005*100)=1.0 per side × 2 = 2.0
        # slippage: 3.0 * 0.03 * 100 = 9.0 per side × 2 = 18.0
        # spread: 150 * 3/10000 = 0.045/share × 100 = 4.5 per side × 2 = 9.0
        assert est.commission == pytest.approx(2.0, abs=0.1)
        assert est.slippage == pytest.approx(18.0, abs=0.1)
        assert est.spread == pytest.approx(9.0, abs=0.1)
        assert est.round_trip > 20.0
        assert est.shares == 100

    def test_min_commission(self):
        m = CostModel.ibkr()
        est = m.estimate(price=150.0, atr=3.0, shares=10)
        # commission: max(1, 0.005*10)=1.0 per side × 2 = 2.0
        assert est.commission == pytest.approx(2.0, abs=0.01)

    def test_estimate_edge_positive(self):
        m = CostModel.ibkr()
        result = m.estimate_edge(
            price=150.0, atr=3.0, shares=100,
            tp_pct=0.04, sl_atr_mult=1.0, win_rate=0.55,
        )
        assert result["edge_per_share"] > 0
        assert result["should_trade"]  # edge should be > 1.5x cost for 100 shares

    def test_estimate_edge_small_position(self):
        m = CostModel.ibkr()
        result = m.estimate_edge(
            price=150.0, atr=3.0, shares=5,
            tp_pct=0.04, sl_atr_mult=1.0, win_rate=0.55,
        )
        # edge_per_share is the same regardless of shares
        # With 5 shares, cost is low (min commission dominates)
        # But edge is still positive for good WR — just check it's calculated
        assert "edge_cost_ratio" in result
        assert "should_trade" in result

    def test_to_dict(self):
        m = CostModel.ibkr()
        est = m.estimate(price=150.0, atr=3.0, shares=50)
        d = est.to_dict()
        assert "round_trip" in d
        assert "cost_per_share" in d
        assert d["shares"] == 50


class TestPositionSizing:
    def test_basic_sizing(self):
        sizer = PositionSizing(capital=100000, risk_per_trade_pct=0.02)
        result = sizer.calculate(price=150.0, atr=3.0)
        # risk = $2000, sl = $3.0, shares = 2000/3 = 666
        # max = 100000*0.15/150 = 100
        # final = min(666, 100) = 100
        assert result.shares == 100
        assert result.position_value == 15000.0
        assert result.should_trade

    def test_small_cap_limits_shares(self):
        sizer = PositionSizing(capital=10000, risk_per_trade_pct=0.02)
        result = sizer.calculate(price=500.0, atr=10.0)
        # risk = $200, sl = $10, shares = 20
        # max = 10000*0.15/500 = 3
        # final = max(10, 3) = 10 (min_shares)
        assert result.shares == 10

    def test_high_atr_reduces_shares(self):
        # Use lower max_position_pct so the cap doesn't bind for both
        sizer = PositionSizing(capital=100000, risk_per_trade_pct=0.02, max_position_pct=0.50)
        result_low = sizer.calculate(price=150.0, atr=2.0)
        result_high = sizer.calculate(price=150.0, atr=10.0)
        # risk=$2000, low: shares=2000/2=1000, high: shares=2000/10=200
        assert result_high.shares < result_low.shares

    def test_invalid_inputs(self):
        sizer = PositionSizing()
        result = sizer.calculate(price=0, atr=3.0)
        assert not result.should_trade
        assert result.shares == 0

    def test_edge_filter_blocks_bad_trades(self):
        sizer = PositionSizing(
            capital=100000,
            min_edge_ratio=1.5,
            expected_win_rate=0.40,  # Low WR → low edge
        )
        result = sizer.calculate(price=150.0, atr=8.0)
        # With 40% WR: edge = 0.4*6 - 0.6*8 = 2.4-4.8 = -2.4 per share
        # Negative edge → should not trade
        assert not result.should_trade

    def test_cost_model_integration(self):
        sizer = PositionSizing(capital=100000)
        cost_model = CostModel.zero()
        result = sizer.calculate(price=150.0, atr=3.0, cost_model=cost_model)
        assert result.cost_estimate == 0.0
        assert result.should_trade

    def test_to_dict(self):
        sizer = PositionSizing(capital=100000)
        result = sizer.calculate(price=150.0, atr=3.0)
        d = result.to_dict()
        assert "shares" in d
        assert "should_trade" in d
        assert "reason" in d
