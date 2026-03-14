"""
单元测试：AutoExitManager (自动退出管理器)

测试场景:
1. 收益率 +6% → 触发止盈 (TARGET_PROFIT)
2. 收益率 +4% → 不触发
3. 收益率 -4% → 触发止损 (STOP_LOSS)
4. 收益率 -2% → 不触发
5. 持仓 25 小时 → 触发超时 (TIMEOUT)
6. 持仓 23 小时 → 不触发
7. 最高价$100, 现价$97 (回撤 3%) → 触发 Trailing
8. 最高价$100, 现价$98.5 (回撤 1.5%) → 不触发
9. Trailing Stop 禁用 → 不触发
"""
import pytest
from datetime import datetime, timedelta
from typing import Optional
import sys
import os

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.v4.core.exit_manager import AutoExitManager, ExitDecision
from app.v4.trader.paper_trader import PaperPosition


class TestExitDecision:
    """测试 ExitDecision 数据结构"""
    
    def test_exit_decision_creation(self):
        """测试 ExitDecision 创建"""
        decision = ExitDecision(
            should_exit=True,
            reason="TARGET_PROFIT",
            expected_pnl=600.0,
            expected_return=0.06,
            current_price=106.0,
            trigger_price=105.0
        )
        
        assert decision.should_exit is True
        assert decision.reason == "TARGET_PROFIT"
        assert decision.expected_pnl == 600.0
        assert decision.expected_return == 0.06
        assert decision.current_price == 106.0
        assert decision.trigger_price == 105.0


class TestTargetProfit:
    """测试目标止盈策略"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager(target_profit=0.05, stop_loss=0.03)
    
    def test_profit_6_percent_triggers_exit(self):
        """收益率 +6% → 触发止盈 (TARGET_PROFIT)"""
        # 创建持仓：成本价 100，现价 106 (收益率 6%)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=106.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TARGET_PROFIT"
        assert decision.expected_return == pytest.approx(0.06, rel=1e-6)
        assert decision.expected_pnl == pytest.approx(600.0, rel=1e-6)
    
    def test_profit_4_percent_no_exit(self):
        """收益率 +4% → 不触发"""
        # 创建持仓：成本价 100，现价 104 (收益率 4%)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_profit_exactly_5_percent_triggers_exit(self):
        """收益率恰好 5% → 触发止盈"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=105.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TARGET_PROFIT"


class TestStopLoss:
    """测试止损策略"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager(target_profit=0.05, stop_loss=0.03)
    
    def test_loss_4_percent_triggers_exit(self):
        """收益率 -4% → 触发止损 (STOP_LOSS)"""
        # 创建持仓：成本价 100，现价 96 (收益率 -4%)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=96.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "STOP_LOSS"
        assert decision.expected_return == pytest.approx(-0.04, rel=1e-6)
        assert decision.expected_pnl == pytest.approx(-400.0, rel=1e-6)
    
    def test_loss_2_percent_no_exit(self):
        """收益率 -2% → 不触发"""
        # 创建持仓：成本价 100，现价 98 (收益率 -2%)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=98.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_loss_exactly_3_percent_triggers_exit(self):
        """亏损恰好 3% → 触发止损"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=97.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "STOP_LOSS"


class TestTimeout:
    """测试时间退出策略"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager(target_profit=0.05, stop_loss=0.03, timeout_hours=24)
    
    def test_holding_25_hours_triggers_timeout(self):
        """持仓 25 小时 → 触发超时 (TIMEOUT)"""
        # 创建持仓：25 小时前建仓
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=101.0,  # 收益 1%，未触发止盈止损
            entry_time=entry_time
        )
        
        # 不提供 indicators，默认平仓
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_holding_23_hours_no_exit(self):
        """持仓 23 小时 → 不触发"""
        # 创建持仓：23 小时前建仓
        entry_time = datetime.now() - timedelta(hours=23)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=101.0,
            entry_time=entry_time
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_holding_exactly_24_hours_triggers_timeout(self):
        """持仓恰好 24 小时 → 触发超时"""
        entry_time = datetime.now() - timedelta(hours=24)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=101.0,
            entry_time=entry_time
        )
        
        # 不提供 indicators，默认平仓
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"


class TestTimeoutDynamic:
    """测试动态超时退出策略 (V2)"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager(
            target_profit=0.05,
            stop_loss=0.03,
            timeout_hours=24,
            strong_uptrend_threshold=0.03,
            rsi_overbought=70
        )
    
    def test_timeout_exit_strong_uptrend(self):
        """持仓 25 小时，收益 +4%，RSI=50，MACD 金叉 → 继续持仓 (HOLD_STRONG)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 50,
            'macd_histogram': 0.5  # 金叉
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is False
        assert decision.reason == "HOLD_STRONG"
    
    def test_timeout_exit_weak_uptrend(self):
        """持仓 25 小时，收益 +2%，RSI=50，MACD 金叉 → 平仓 (TIMEOUT，涨势不足)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=102.0,  # 收益 2%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 50,
            'macd_histogram': 0.5  # 金叉
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_timeout_exit_rsi_overbought(self):
        """持仓 25 小时，收益 +4%，RSI=75，MACD 金叉 → 平仓 (TIMEOUT，RSI 超买)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 75,  # 超买
            'macd_histogram': 0.5  # 金叉
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_timeout_exit_macd_dead_cross(self):
        """持仓 25 小时，收益 +4%，RSI=50，MACD 死叉 → 平仓 (TIMEOUT，指标不利)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 50,
            'macd_histogram': -0.5  # 死叉
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_timeout_exit_all_negative(self):
        """持仓 25 小时，收益 +1%，RSI=50，MACD 死叉 → 平仓 (TIMEOUT)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=101.0,  # 收益 1%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 50,
            'macd_histogram': -0.5  # 死叉
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_holding_23_hours_no_timeout(self):
        """持仓 23 小时，任意条件 → 不触发超时"""
        entry_time = datetime.now() - timedelta(hours=23)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 50,
            'macd_histogram': 0.5
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is None
    
    def test_timeout_without_indicators(self):
        """持仓 25 小时，无指标数据 → 默认平仓 (TIMEOUT)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        # 不提供 indicators 参数
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_timeout_edge_case_rsi_exactly_70(self):
        """持仓 25 小时，收益 +4%，RSI=70，MACD 金叉 → 平仓 (RSI=70 不满足<70 条件)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 70,  # 恰好 70
            'macd_histogram': 0.5
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        # RSI < 70 才是 favorable，RSI=70 不满足条件 → 平仓
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"
    
    def test_timeout_edge_case_macd_zero(self):
        """持仓 25 小时，收益 +4%，RSI=50，MACD=0 → 平仓 (MACD=0 不算金叉)"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=104.0,  # 收益 4%
            entry_time=entry_time
        )
        
        indicators = {
            'rsi': 50,
            'macd_histogram': 0  # 零轴
        }
        
        decision = self.manager.check_exit(position, indicators)
        
        # macd_histogram > 0 才是 favorable
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TIMEOUT"


class TestTrailingStop:
    """测试 Trailing Stop 策略"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager(
            target_profit=0.05,
            stop_loss=0.03,
            trailing_stop=True,
            trailing_pct=0.02
        )
    
    def test_drawdown_3_percent_triggers_trailing(self):
        """最高价$100, 现价$97 (回撤 3%) → 触发 Trailing"""
        # 创建持仓：成本价 95，现价 97
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=95.0,
            current_price=97.0,
            entry_time=datetime.now()
        )
        
        # 更新最高价为 100
        self.manager.update_peak_price("0700.HK", 100.0)
        
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TRAILING"
        # 回撤 = (100 - 97) / 100 = 3%
    
    def test_drawdown_1_5_percent_no_exit(self):
        """最高价$100, 现价$98.5 (回撤 1.5%) → 不触发"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=95.0,
            current_price=98.5,
            entry_time=datetime.now()
        )
        
        # 更新最高价为 100
        self.manager.update_peak_price("0700.HK", 100.0)
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_trailing_disabled_no_exit(self):
        """Trailing Stop 禁用 → 不触发"""
        manager = AutoExitManager(
            target_profit=0.05,
            stop_loss=0.03,
            trailing_stop=False,
            trailing_pct=0.02
        )
        
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=95.0,
            current_price=97.0,
            entry_time=datetime.now()
        )
        
        # 更新最高价为 100
        manager.update_peak_price("0700.HK", 100.0)
        
        decision = manager.check_exit(position)
        
        assert decision is None
    
    def test_trailing_exactly_2_percent_triggers_exit(self):
        """回撤恰好 2% → 触发 Trailing"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=95.0,
            current_price=98.0,
            entry_time=datetime.now()
        )
        
        self.manager.update_peak_price("0700.HK", 100.0)
        
        decision = self.manager.check_exit(position)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TRAILING"
    
    def test_trailing_without_peak_price_no_exit(self):
        """未设置最高价 → 不触发 Trailing"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=95.0,
            current_price=97.0,
            entry_time=datetime.now()
        )
        
        # 不更新最高价
        
        decision = self.manager.check_exit(position)
        
        assert decision is None


class TestPriority:
    """测试退出策略优先级"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager(
            target_profit=0.05,
            stop_loss=0.03,
            timeout_hours=24,
            trailing_stop=True,
            trailing_pct=0.02
        )
    
    def test_profit_priority_over_timeout(self):
        """同时触达止盈和超时 → 优先返回止盈"""
        # 25 小时前建仓，收益 6%
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=106.0,
            entry_time=entry_time
        )
        
        # 提供指标，但止盈优先级更高
        indicators = {'rsi': 50, 'macd_histogram': 0.5}
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "TARGET_PROFIT"  # 止盈优先级最高
    
    def test_loss_priority_over_timeout(self):
        """同时触达止损和超时 → 优先返回止损"""
        entry_time = datetime.now() - timedelta(hours=25)
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=96.0,
            entry_time=entry_time
        )
        
        # 提供指标，但止损优先级更高
        indicators = {'rsi': 50, 'macd_histogram': 0.5}
        decision = self.manager.check_exit(position, indicators)
        
        assert decision is not None
        assert decision.should_exit is True
        assert decision.reason == "STOP_LOSS"


class TestConfig:
    """测试配置管理"""
    
    def test_get_exit_config(self):
        """测试获取退出配置"""
        manager = AutoExitManager(
            target_profit=0.05,
            stop_loss=0.03,
            timeout_hours=24,
            trailing_stop=True,
            trailing_pct=0.02,
            strong_uptrend_threshold=0.03,
            rsi_overbought=70
        )
        
        config = manager.get_exit_config()
        
        assert config["target_profit"] == 0.05
        assert config["stop_loss"] == 0.03
        assert config["timeout_hours"] == 24
        assert config["trailing_stop"] is True
        assert config["trailing_pct"] == 0.02
        assert config["strong_uptrend_threshold"] == 0.03
        assert config["rsi_overbought"] == 70
    
    def test_default_config(self):
        """测试默认配置"""
        manager = AutoExitManager()
        
        config = manager.get_exit_config()
        
        assert config["target_profit"] == 0.05
        assert config["stop_loss"] == 0.03
        assert config["timeout_hours"] == 24
        assert config["trailing_stop"] is False
        assert config["trailing_pct"] == 0.02
        assert config["strong_uptrend_threshold"] == 0.03
        assert config["rsi_overbought"] == 70


class TestEdgeCases:
    """测试边界情况"""
    
    def setup_method(self):
        """每个测试前创建管理器"""
        self.manager = AutoExitManager()
    
    def test_zero_quantity_no_exit(self):
        """持仓数量为 0 → 不触发"""
        position = PaperPosition(
            code="0700.HK",
            quantity=0,
            avg_cost=100.0,
            current_price=106.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_zero_current_price_no_exit(self):
        """当前价格为 0 → 不触发"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=0.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_zero_avg_cost_no_exit(self):
        """平均成本为 0 → 不触发"""
        position = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=0.0,
            current_price=106.0,
            entry_time=datetime.now()
        )
        
        decision = self.manager.check_exit(position)
        
        assert decision is None
    
    def test_multiple_stocks_independent(self):
        """多只股票独立管理"""
        # 股票 A：收益 6%
        position_a = PaperPosition(
            code="0700.HK",
            quantity=100,
            avg_cost=100.0,
            current_price=106.0,
            entry_time=datetime.now()
        )
        
        # 股票 B：收益 2%
        position_b = PaperPosition(
            code="MU",
            quantity=50,
            avg_cost=80.0,
            current_price=81.6,
            entry_time=datetime.now()
        )
        
        decision_a = self.manager.check_exit(position_a)
        decision_b = self.manager.check_exit(position_b)
        
        assert decision_a is not None
        assert decision_a.should_exit is True
        assert decision_a.reason == "TARGET_PROFIT"
        
        assert decision_b is None
    
    def test_update_peak_price_multiple_stocks(self):
        """更新多只股票的最高价"""
        self.manager.update_peak_price("0700.HK", 100.0)
        self.manager.update_peak_price("MU", 85.0)
        
        config = self.manager.get_exit_config()
        # 验证最高价已存储 (通过内部状态)
        assert self.manager._peak_prices["0700.HK"] == 100.0
        assert self.manager._peak_prices["MU"] == 85.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
