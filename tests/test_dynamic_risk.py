"""
测试 DynamicRiskExit 动态风控退出模块

覆盖场景:
- ATR 止损触发 (多头/空头)
- 固定百分比止损
- 止盈触发
- 时间止损
- 个股累计亏损限制
- 连续亏损熔断
- 黑名单检查
- 风控状态跟踪
"""
import pytest
from app.exit.dynamic_risk import DynamicRiskExit, StockRiskState
from app.exit.base import ExitProfile


@pytest.fixture
def risk_exit():
    """创建 DynamicRiskExit 实例"""
    return DynamicRiskExit()


@pytest.fixture
def long_position():
    """创建多头持仓"""
    class Position:
        symbol = "US.AAPL"
        avg_entry_price = 150.0
        direction = "LONG"
        quantity = 100
    return Position()


@pytest.fixture
def short_position():
    """创建空头持仓"""
    class Position:
        symbol = "US.TSLA"
        avg_entry_price = 200.0
        direction = "SHORT"
        quantity = 50
    return Position()


@pytest.fixture
def atr_profile():
    """创建 ATR 止损配置"""
    return ExitProfile(
        name="test_atr",
        stop_loss_type="atr_multi",
        stop_loss_params={"multiplier": 1.0, "max_loss_pct": -0.05},
        take_profit_type="fixed_pct",
        take_profit_params={"target_pct": 0.04},
        max_holding_minutes=4320,
    )


class TestATRStopLoss:
    """ATR 止损测试"""

    def test_long_atr_sl_triggered(self, risk_exit, long_position, atr_profile):
        """多头 ATR 止损触发"""
        ctx = {
            "current_price": 145.0,  # 从 150 跌到 145
            "atr": 3.0,
            "holding_minutes": 60,
        }
        # stop_price = 150 - 3.0 * 1.0 = 147.0
        # current 145 < 147 → 触发
        decision = risk_exit.evaluate(long_position, ctx, atr_profile)
        assert decision.action == "CLOSE"
        assert decision.reason == "ATR_SL_HIT"
        assert decision.qty_pct == 1.0

    def test_long_atr_sl_not_triggered(self, risk_exit, long_position, atr_profile):
        """多头 ATR 止损未触发"""
        ctx = {
            "current_price": 148.0,  # 从 150 跌到 148
            "atr": 3.0,
            "holding_minutes": 60,
        }
        # stop_price = 147.0, current 148 > 147 → 不触发
        decision = risk_exit.evaluate(long_position, ctx, atr_profile)
        assert decision.action != "CLOSE" or decision.reason != "ATR_SL_HIT"

    def test_short_atr_sl_triggered(self, risk_exit, short_position, atr_profile):
        """空头 ATR 止损触发"""
        ctx = {
            "current_price": 205.0,  # 从 200 涨到 205
            "atr": 4.0,
            "holding_minutes": 60,
        }
        # stop_price = 200 + 4.0 * 1.0 = 204.0
        # current 205 > 204 → 触发
        decision = risk_exit.evaluate(short_position, ctx, atr_profile)
        assert decision.action == "CLOSE"
        assert decision.reason == "ATR_SL_HIT"


class TestFixedStopLoss:
    """固定百分比止损测试"""

    def test_fixed_sl_triggered(self, risk_exit, long_position):
        """固定百分比止损触发"""
        profile = ExitProfile(
            name="test_fixed",
            stop_loss_type="fixed_pct",
            stop_loss_params={"pct": -0.03},
            take_profit_type="fixed_pct",
            take_profit_params={"target_pct": 0.04},
        )
        ctx = {
            "current_price": 144.0,  # (144-150)/150 = -4%
            "atr": 3.0,
            "holding_minutes": 60,
        }
        decision = risk_exit.evaluate(long_position, ctx, profile)
        assert decision.action == "CLOSE"
        assert decision.reason == "FIXED_SL_HIT"


class TestTakeProfit:
    """止盈测试"""

    def test_tp_triggered(self, risk_exit, long_position, atr_profile):
        """止盈触发"""
        ctx = {
            "current_price": 157.0,  # (157-150)/150 = +4.67%
            "atr": 3.0,
            "holding_minutes": 60,
        }
        decision = risk_exit.evaluate(long_position, ctx, atr_profile)
        assert decision.action == "CLOSE"
        assert decision.reason == "TP_HIT"


class TestTimeStop:
    """时间止损测试"""

    def test_time_stop_triggered(self, risk_exit, long_position, atr_profile):
        """时间止损触发"""
        ctx = {
            "current_price": 151.0,  # 价格中性
            "atr": 3.0,
            "holding_minutes": 5000,  # 超过 4320 分钟
        }
        decision = risk_exit.evaluate(long_position, ctx, atr_profile)
        assert decision.action == "CLOSE"
        assert decision.reason == "TIME_STOP"


class TestStockRiskControl:
    """个股风险控制测试"""

    def test_blacklist_blocks_entry(self, risk_exit):
        """黑名单阻止入场"""
        risk_params = {
            "blacklist": ["NVDA", "META"],
            "max_stock_loss_pct": 0.01,
            "capital": 100000.0,
        }
        allowed, reason = risk_exit.check_entry_allowed("US.NVDA", risk_params)
        assert not allowed
        assert "blacklist" in reason.lower()

    def test_cumulative_loss_blocks_entry(self, risk_exit):
        """累计亏损阻止入场"""
        risk_params = {
            "blacklist": [],
            "max_stock_loss_pct": 0.01,  # 1% = $1000
            "capital": 100000.0,
        }
        # 记录亏损交易
        risk_exit.record_trade_result("US.AAPL", -600.0, risk_params)
        risk_exit.record_trade_result("US.AAPL", -500.0, risk_params)
        # 累计 -$1100 > -$1000 → 应该被阻止
        
        allowed, reason = risk_exit.check_entry_allowed("US.AAPL", risk_params)
        assert not allowed
        assert "cumulative loss" in reason.lower()

    def test_win_resets_loss_streak(self, risk_exit):
        """盈利重置连续亏损"""
        risk_params = {"blacklist": [], "max_stock_loss_pct": 0.01}
        risk_exit.record_trade_result("US.AAPL", -100.0, risk_params)
        risk_exit.record_trade_result("US.AAPL", -100.0, risk_params)
        risk_exit.record_trade_result("US.AAPL", +200.0, risk_params)
        
        state = risk_exit._get_stock_state("US.AAPL")
        assert state.loss_streak == 0
        assert state.cumulative_pnl == 0.0


class TestConsecutiveLossPause:
    """连续亏损熔断测试"""

    def test_consecutive_loss_triggers_pause(self, risk_exit):
        """连续亏损触发暂停"""
        risk_params = {
            "blacklist": [],
            "max_stock_loss_pct": 0.01,
            "max_consecutive_losses": 3,
            "capital": 100000.0,
        }
        # 连续 3 笔亏损
        risk_exit.record_trade_result("US.AAPL", -100.0, risk_params)
        risk_exit.record_trade_result("US.TSLA", -150.0, risk_params)
        risk_exit.record_trade_result("US.MSFT", -200.0, risk_params)
        
        # 应该触发全局暂停
        allowed, reason = risk_exit.check_entry_allowed("US.GOOGL", risk_params)
        assert not allowed
        assert "paused" in reason.lower() or "连续" in reason

    def test_win_resets_pause(self, risk_exit):
        """盈利重置暂停"""
        risk_params = {
            "blacklist": [],
            "max_stock_loss_pct": 0.01,
            "max_consecutive_losses": 3,
            "capital": 100000.0,
        }
        # 连续 2 笔亏损 (未达到 3)
        risk_exit.record_trade_result("US.AAPL", -100.0, risk_params)
        risk_exit.record_trade_result("US.TSLA", -150.0, risk_params)
        # 一笔盈利
        risk_exit.record_trade_result("US.MSFT", +300.0, risk_params)
        
        # 不应该暂停
        allowed, reason = risk_exit.check_entry_allowed("US.GOOGL", risk_params)
        assert allowed
        assert reason == "OK"


class TestRiskSummary:
    """风控状态摘要测试"""

    def test_get_risk_summary(self, risk_exit):
        """获取风控状态摘要"""
        risk_params = {"blacklist": [], "max_stock_loss_pct": 0.01}
        risk_exit.record_trade_result("US.AAPL", +500.0, risk_params)
        risk_exit.record_trade_result("US.TSLA", -200.0, risk_params)
        risk_exit.record_trade_result("US.TSLA", -300.0, risk_params)
        
        summary = risk_exit.get_risk_summary()
        
        assert "stocks" in summary
        assert "US.AAPL" in summary["stocks"]
        assert "US.TSLA" in summary["stocks"]
        
        aapl_state = summary["stocks"]["US.AAPL"]
        assert aapl_state["trades"] == 1
        assert aapl_state["win_rate"] == 1.0
        assert aapl_state["cumulative_pnl"] == 500.0
        
        tsla_state = summary["stocks"]["US.TSLA"]
        assert tsla_state["trades"] == 2
        assert tsla_state["win_rate"] == 0.0
        assert tsla_state["cumulative_pnl"] == -500.0
        assert tsla_state["loss_streak"] == 2


class TestReset:
    """重置功能测试"""

    def test_reset_clears_all_state(self, risk_exit):
        """重置清除所有状态"""
        risk_params = {"blacklist": [], "max_stock_loss_pct": 0.01}
        risk_exit.record_trade_result("US.AAPL", -100.0, risk_params)
        risk_exit.record_trade_result("US.TSLA", -200.0, risk_params)
        
        risk_exit.reset()
        
        summary = risk_exit.get_risk_summary()
        assert len(summary["stocks"]) == 0
        assert summary["global_loss_streak"] == 0
        assert not summary["is_paused"]
