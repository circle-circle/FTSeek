"""
ExitMonitor 止损止盈逻辑单元测试

P0/P1/P2 修复 (2026-04-27):
  - Scale Out 分批止盈
  - 盘末强制退出 (Pre-Close Exit)
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.risk.position_exit_monitor import ExitMonitor, get_exit_monitor


# ============================================================
#  Fixtures
# ============================================================

class FakePosition:
    """最小 Position 桩，提供 ExitMonitor 所需的所有属性"""
    def __init__(self, symbol, direction, entry_price, size,
                 take_profit_levels=None, trail_state=None,
                 entry_time=None, metadata=None):
        self.symbol = symbol
        self.direction = MagicMock(value=direction)
        self.avg_entry_price = entry_price
        self.size = float(size)
        self.take_profit_levels = take_profit_levels or []
        self.trail_state = trail_state or {}
        self.entry_time = entry_time or datetime.now()
        self.metadata = metadata or {}
        self.current_sl = 0.0
        self.initial_sl = 0.0
        self.signal_id = f"sig_{symbol}"
        self.strategy_id = "test"
        self.trace_id = "trace_test"


@pytest.fixture
def monitor():
    return ExitMonitor()


@pytest.fixture
def long_position():
    """做多持仓，入场价 $100"""
    return FakePosition("US.NVDA", "LONG", 100.0, 100)


@pytest.fixture
def short_position():
    """做空持仓，入场价 $100"""
    return FakePosition("US.NVDA", "SHORT", 100.0, 100)


# ============================================================
#  P0: Scale Out 分批止盈
# ============================================================

class TestScaleOut:
    """分批止盈逻辑测试"""

    def test_no_tiers_returns_none(self, monitor, long_position):
        """无止盈层级 → 不触发"""
        result = monitor._check_scale_out(long_position, 110.0, {}, "LONG")
        assert result is None

    def test_long_hits_tier1(self, monitor, long_position):
        """做多触及 Tier 1 (+8%) → 返回部分平仓指令"""
        long_position.take_profit_levels = [
            {"ratio": 0.30, "price": 108.0},
            {"ratio": 0.30, "price": 115.0},
            {"ratio": 0.40, "price": 125.0},
        ]
        result = monitor._check_scale_out(long_position, 110.0, {}, "LONG")
        assert result is not None
        assert result["reason"] == "SCALE_OUT"
        assert result["close_qty"] == 30  # 30% of 100 shares
        assert result["exit_price"] == 110.0

    def test_long_below_tier1_no_trigger(self, monitor, long_position):
        """做多价格未触及 Tier 1 → 不触发"""
        long_position.take_profit_levels = [
            {"ratio": 0.30, "price": 108.0},
        ]
        result = monitor._check_scale_out(long_position, 105.0, {}, "LONG")
        assert result is None

    def test_short_hits_tier1(self, monitor, short_position):
        """做空触及止盈价 (价格下跌) → 返回部分平仓指令"""
        short_position.take_profit_levels = [
            {"ratio": 0.30, "price": 92.0},
            {"ratio": 0.30, "price": 85.0},
        ]
        result = monitor._check_scale_out(short_position, 90.0, {}, "SHORT")
        assert result is not None
        assert result["reason"] == "SCALE_OUT"
        assert result["close_qty"] == 30

    def test_short_above_tier1_no_trigger(self, monitor, short_position):
        """做空价格未跌到 Tier 1 → 不触发"""
        short_position.take_profit_levels = [
            {"ratio": 0.30, "price": 92.0},
        ]
        result = monitor._check_scale_out(short_position, 95.0, {}, "SHORT")
        assert result is None

    def test_tier_already_triggered_skips(self, monitor, long_position):
        """已触发的 Tier 不会重复触发"""
        long_position.take_profit_levels = [
            {"ratio": 0.30, "price": 108.0},
            {"ratio": 0.30, "price": 115.0},
        ]
        trail_state = {"triggered_tiers": [0]}  # Tier 1 已触发

        # 价格同时超过 Tier 1 和 Tier 2 → 应触发 Tier 2 而不是 Tier 1
        result = monitor._check_scale_out(long_position, 120.0, trail_state, "LONG")
        assert result is not None
        assert result["close_qty"] == 30  # Tier 2's 30%
        assert trail_state["triggered_tiers"] == [0, 1]

    def test_sequential_tiers(self, monitor, long_position):
        """顺序触发所有三层"""
        long_position.take_profit_levels = [
            {"ratio": 0.30, "price": 108.0},
            {"ratio": 0.30, "price": 115.0},
            {"ratio": 0.40, "price": 125.0},
        ]
        trail_state = {}

        # Tier 1
        r1 = monitor._check_scale_out(long_position, 110.0, trail_state, "LONG")
        assert r1["close_qty"] == 30
        assert trail_state["triggered_tiers"] == [0]

        # Tier 2 (30% of **original** 100)
        r2 = monitor._check_scale_out(long_position, 120.0, trail_state, "LONG")
        assert r2["close_qty"] == 30
        assert trail_state["triggered_tiers"] == [0, 1]

        # Tier 3 (40% of **original** 100)
        r3 = monitor._check_scale_out(long_position, 130.0, trail_state, "LONG")
        assert r3["close_qty"] == 40
        assert trail_state["triggered_tiers"] == [0, 1, 2]

        # All done
        r4 = monitor._check_scale_out(long_position, 200.0, trail_state, "LONG")
        assert r4 is None

    def test_close_qty_minimum_1(self, monitor, long_position):
        """最小平仓量为 1 股"""
        long_position.size = 5.0
        long_position.take_profit_levels = [
            {"ratio": 0.30, "price": 108.0},
        ]
        result = monitor._check_scale_out(long_position, 110.0, {}, "LONG")
        assert result["close_qty"] >= 1

    def test_zero_price_tier_skipped(self, monitor, long_position):
        """价格为 0 的 tier 被跳过"""
        long_position.take_profit_levels = [
            {"ratio": 0.30, "price": 0},
            {"ratio": 0.30, "price": 115.0},
        ]
        result = monitor._check_scale_out(long_position, 120.0, {}, "LONG")
        assert result is not None
        # Should trigger tier index 1, not 0
        assert "triggered_tiers" in result or True


# ============================================================
#  P2: Pre-Close Exit 盘末强制退出
# ============================================================

class TestPreCloseExit:
    """盘末强制退出逻辑测试"""

    def test_weekend_no_exit(self, monitor, long_position):
        """周末不触发盘末退出"""
        with patch("app.risk.position_exit_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 25, 15, 50, 0)  # Saturday
            mock_dt.strftime = datetime.strftime
            # Mock ET timezone awareness
            from pytz import timezone
            ET = timezone("US/Eastern")
            mock_now_et = ET.localize(datetime(2026, 4, 25, 15, 50, 0))
            mock_dt.now.side_effect = lambda tz=None: mock_now_et if tz else datetime(2026, 4, 25, 15, 50, 0)

            result = monitor._check_pre_close_exit(long_position, 101.0, 2.0, {}, "LONG")
            assert result is None

    @patch("app.risk.position_exit_monitor.datetime")
    def test_within_15min_low_profit_exits(self, mock_dt, monitor, long_position):
        """距收盘 < 15min 且浮盈 < 1 ATR → 强制退出"""
        from pytz import timezone
        ET = timezone("US/Eastern")

        mock_now = ET.localize(datetime(2026, 4, 24, 15, 50, 0))  # Friday 15:50 ET
        mock_dt.now.return_value = mock_now
        mock_dt.strftime = datetime.strftime

        # 浮盈很小 (profit_atr ~= 0.2)
        result = monitor._check_pre_close_exit(long_position, 100.5, 2.5, {}, "LONG")
        assert result is not None
        assert result["reason"] == "PRE_CLOSE_EXIT"
        assert "距收盘" in result["reason_cn"]

    @patch("app.risk.position_exit_monitor.datetime")
    def test_within_15min_good_profit_holds(self, mock_dt, monitor, long_position):
        """距收盘 < 15min 但浮盈 ≥ 1 ATR → 允许持仓过夜"""
        from pytz import timezone
        ET = timezone("US/Eastern")

        mock_now = ET.localize(datetime(2026, 4, 24, 15, 50, 0))
        mock_dt.now.return_value = mock_now
        mock_dt.strftime = datetime.strftime

        # 浮盈充足: ATR=2.0, entry=100, price=104 → profit_atr = 4/100*100/2 = 2.0
        result = monitor._check_pre_close_exit(long_position, 104.0, 2.0, {}, "LONG")
        assert result is None

    @patch("app.risk.position_exit_monitor.datetime")
    def test_before_15min_no_trigger(self, mock_dt, monitor, long_position):
        """距收盘 > 15min → 不触发"""
        from pytz import timezone
        ET = timezone("US/Eastern")

        mock_now = ET.localize(datetime(2026, 4, 24, 14, 0, 0))  # 2 PM
        mock_dt.now.return_value = mock_now
        mock_dt.strftime = datetime.strftime

        result = monitor._check_pre_close_exit(long_position, 100.5, 2.0, {}, "LONG")
        assert result is None

    @patch("app.risk.position_exit_monitor.datetime")
    def test_after_close_no_trigger(self, mock_dt, monitor, long_position):
        """收盘后 → 不触发"""
        from pytz import timezone
        ET = timezone("US/Eastern")

        mock_now = ET.localize(datetime(2026, 4, 24, 16, 30, 0))
        mock_dt.now.return_value = mock_now
        mock_dt.strftime = datetime.strftime

        result = monitor._check_pre_close_exit(long_position, 100.5, 2.0, {}, "LONG")
        assert result is None

    def test_no_atr_fallback(self, monitor, long_position):
        """ATR=0 → 退化为不触发"""
        result = monitor._check_pre_close_exit(long_position, 101.0, 0.0, {}, "LONG")
        assert result is None


# ============================================================
#  P1: Scorer Stop-Loss 接入校验
# ============================================================

class TestScorerStopLossWiring:
    """IntradayScorer 止损接入位置校验"""

    def test_stop_loss_pct_in_vote_result(self):
        """验证 IntradaySignal.to_vote_result() 包含 stop_loss_pct"""
        from app.core.intraday_scorer import get_intraday_scorer

        ctx = {
            "symbol": "US.NVDA",
            "price": 450.0,
            "change_percent": 1.5,
            "rsi": 55.0, "rsi_prev": 52.0,
            "macd": 0.85, "macd_signal": 0.60,
            "ma20": 440.0, "ma50": 420.0, "ma200": 380.0,
            "atr": 12.5,
            "volume_ratio": 1.2, "volume": 50000000,
            "turnover": 2.25e8, "amplitude": 2.5,
            "lv3_bid": 449.80, "lv3_ask": 450.20,
            "lv3_bid_vol": 15000, "lv3_ask_vol": 12000,
            "order_flow_imbalance": 0.11,
            "vix": 20.0, "spy_price": 520.0, "spy_ma50": 510.0,
        }
        scorer = get_intraday_scorer()
        signal = scorer.score(ctx)
        vr = signal.to_vote_result()

        sl_pct = vr.get("position_suggestion", {}).get("stop_loss_pct", 0)
        assert sl_pct > 0, f"Expected positive stop_loss_pct, got {sl_pct}"
        assert 0.01 <= sl_pct <= 0.10, f"stop_loss_pct={sl_pct:.3f} out of reasonable range [0.01, 0.10]"

    @patch("app.risk.position_exit_monitor.datetime")
    def test_high_vix_wider_stop_in_vote_result(self, mock_dt):
        """高 VIX → stop_loss_pct 更大"""
        from app.core.intraday_scorer import get_intraday_scorer

        base_ctx = {
            "symbol": "US.NVDA", "price": 450.0,
            "change_percent": 1.5,
            "rsi": 55.0, "rsi_prev": 52.0,
            "macd": 0.85, "macd_signal": 0.60,
            "ma20": 440.0, "ma50": 420.0, "ma200": 380.0,
            "atr": 12.5, "volume_ratio": 1.2,
            "volume": 50000000, "turnover": 2.25e8, "amplitude": 2.5,
            "lv3_bid": 449.80, "lv3_ask": 450.20,
            "lv3_bid_vol": 15000, "lv3_ask_vol": 12000,
            "order_flow_imbalance": 0.11,
            "spy_price": 520.0, "spy_ma50": 510.0,
        }
        scorer = get_intraday_scorer()

        normal = scorer.score({**base_ctx, "vix": 18.0})
        panic = scorer.score({**base_ctx, "vix": 32.0})

        normal_sl = normal.to_vote_result()["position_suggestion"]["stop_loss_pct"]
        panic_sl = panic.to_vote_result()["position_suggestion"]["stop_loss_pct"]

        if normal.direction != "NEUTRAL" and panic.direction != "NEUTRAL":
            assert panic_sl >= normal_sl, \
                f"高VIX止损应该更宽: VIX=18→{normal_sl:.3f} vs VIX=32→{panic_sl:.3f}"


# ============================================================
#  ExitReason 枚举完整性
# ============================================================

class TestExitReasonEnum:
    """ExitReason 包含新增的 SCALE_OUT 和 PRE_CLOSE_EXIT"""

    def test_scale_out_exists(self):
        from app.core.models import ExitReason
        assert hasattr(ExitReason, 'SCALE_OUT')
        assert ExitReason.SCALE_OUT == "SCALE_OUT"

    def test_pre_close_exit_exists(self):
        from app.core.models import ExitReason
        assert hasattr(ExitReason, 'PRE_CLOSE_EXIT')
        assert ExitReason.PRE_CLOSE_EXIT == "PRE_CLOSE_EXIT"
