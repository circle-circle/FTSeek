"""
IntraTradeRisk 单元测试 - 交易中风控模块

测试覆盖:
- 移动止损追踪
- 异常波动检测
- 实时盈亏监控
- 仓位动态调整
"""
import pytest
import time
from unittest.mock import Mock, MagicMock
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.risk.intra_trade import IntraTradeRisk, PositionMonitor, VolatilityDetector


class TestIntraTradeRisk:
    """IntraTradeRisk 核心功能测试"""
    
    def test_trailing_stop_initialization(self):
        """测试移动止损初始化"""
        risk = IntraTradeRisk(trailing_stop_pct=0.03)
        assert risk.trailing_stop_pct == 0.03
        assert risk.entry_price is None
        assert risk.trailing_stop_price is None
    
    def test_set_entry_price(self):
        """测试设置入场价格"""
        risk = IntraTradeRisk()
        risk.set_entry_price(100.0, "BUY")
        assert risk.entry_price == 100.0
        assert risk.direction == "BUY"
        assert risk.initial_stop_loss == 97.0  # 默认 3% 止损
    
    def test_trailing_stop_update_buy_position(self):
        """测试多头仓位移动止损更新"""
        risk = IntraTradeRisk(trailing_stop_pct=0.03)
        risk.set_entry_price(100.0, "BUY")
        
        # 价格上涨到 110，止损应上移到 106.7
        risk.update_price(110.0)
        expected_stop = 110.0 * (1 - 0.03)  # 106.7
        assert abs(risk.trailing_stop_price - expected_stop) < 0.01
        
        # 价格继续上涨到 120，止损应继续上移
        risk.update_price(120.0)
        expected_stop = 120.0 * (1 - 0.03)  # 116.4
        assert abs(risk.trailing_stop_price - expected_stop) < 0.01
        
        # 价格下跌到 115，止损不应下移
        risk.update_price(115.0)
        assert abs(risk.trailing_stop_price - 116.4) < 0.01
    
    def test_trailing_stop_update_sell_position(self):
        """测试空头仓位移动止损更新"""
        risk = IntraTradeRisk(trailing_stop_pct=0.03)
        risk.set_entry_price(100.0, "SELL")
        
        # 价格下跌到 90，止损应下移到 92.7
        risk.update_price(90.0)
        expected_stop = 90.0 * (1 + 0.03)  # 92.7
        assert abs(risk.trailing_stop_price - expected_stop) < 0.01
        
        # 价格继续下跌到 80，止损应继续下移
        risk.update_price(80.0)
        expected_stop = 80.0 * (1 + 0.03)  # 82.4
        assert abs(risk.trailing_stop_price - expected_stop) < 0.01
    
    def test_should_stop_loss_triggered(self):
        """测试止损触发"""
        risk = IntraTradeRisk(trailing_stop_pct=0.03)
        risk.set_entry_price(100.0, "BUY")
        risk.update_price(110.0)  # 止损上移到 106.7
        
        # 价格下跌触及止损
        risk.update_price(106.5)
        assert risk.should_stop_loss() == True
    
    def test_should_stop_loss_not_triggered(self):
        """测试止损未触发"""
        risk = IntraTradeRisk(trailing_stop_pct=0.03)
        risk.set_entry_price(100.0, "BUY")
        risk.update_price(110.0)  # 止损上移到 106.7
        
        # 价格仍在止损之上
        risk.update_price(107.0)
        assert risk.should_stop_loss() == False
    
    def test_unrealized_pnl_calculation(self):
        """测试未实现盈亏计算"""
        risk = IntraTradeRisk()
        risk.set_entry_price(100.0, "BUY")
        risk.set_quantity(100)
        
        # 价格上涨 10%
        risk.update_price(110.0)
        pnl = risk.get_unrealized_pnl()
        assert pnl['pnl_amount'] == 1000.0  # (110-100) * 100
        assert pnl['pnl_percent'] == 0.10
    
    def test_profit_lock(self):
        """测试利润锁定功能"""
        risk = IntraTradeRisk(trailing_stop_pct=0.03, profit_lock_pct=0.05)
        risk.set_entry_price(100.0, "BUY")
        
        # 价格上涨 10%，应锁定至少 5% 利润
        risk.update_price(110.0)
        # 止损应在 105 以上 (锁定 5% 利润)
        assert risk.trailing_stop_price >= 105.0


class TestVolatilityDetector:
    """波动检测器测试"""
    
    def test_volatility_calculation(self):
        """测试波动率计算"""
        detector = VolatilityDetector(window_size=5)
        
        # 添加价格序列
        prices = [100, 102, 101, 103, 105]
        for price in prices:
            detector.add_price(price)
        
        volatility = detector.calculate_volatility()
        assert volatility > 0
    
    def test_abnormal_volatility_detection(self):
        """测试异常波动检测"""
        detector = VolatilityDetector(threshold_multiplier=2.0)
        
        # 先建立正常波动基准 (稳定价格)
        for _ in range(20):
            detector.add_price(100 + (_ % 2))  # 小幅波动
        
        # 突然大幅波动
        detector.add_price(120)
        
        is_abnormal = detector.is_abnormal_volatility()
        assert is_abnormal == True
    
    def test_normal_volatility(self):
        """测试正常波动"""
        detector = VolatilityDetector(threshold_multiplier=2.0)
        
        # 稳定波动
        for i in range(20):
            detector.add_price(100 + (i % 3))
        
        is_abnormal = detector.is_abnormal_volatility()
        assert is_abnormal == False
    
    def test_insufficient_data(self):
        """测试数据不足情况"""
        detector = VolatilityDetector(window_size=10)
        
        # 只添加少量数据
        for i in range(5):
            detector.add_price(100 + i)
        
        # 数据不足，不应判定为异常
        is_abnormal = detector.is_abnormal_volatility()
        assert is_abnormal == False


class TestPositionMonitor:
    """仓位监控器测试"""
    
    def test_position_tracking(self):
        """测试仓位追踪"""
        monitor = PositionMonitor()
        
        # 添加仓位
        monitor.add_position("HK.00700", {
            "entry_price": 100.0,
            "quantity": 100,
            "direction": "BUY"
        })
        
        assert "HK.00700" in monitor.positions
        assert monitor.positions["HK.00700"].quantity == 100
    
    def test_position_update(self):
        """测试仓位更新"""
        monitor = PositionMonitor()
        monitor.add_position("HK.00700", {
            "entry_price": 100.0,
            "quantity": 100,
            "direction": "BUY"
        })
        
        # 更新价格
        monitor.update_price("HK.00700", 110.0)
        
        assert monitor.positions["HK.00700"].current_price == 110.0
    
    def test_position_pnl(self):
        """测试仓位盈亏"""
        monitor = PositionMonitor()
        monitor.add_position("HK.00700", {
            "entry_price": 100.0,
            "quantity": 100,
            "direction": "BUY"
        })
        monitor.update_price("HK.00700", 110.0)
        
        pnl = monitor.get_position_pnl("HK.00700")
        assert pnl["unrealized_pnl"] == 1000.0
        assert pnl["unrealized_pnl_percent"] == 0.10
    
    def test_remove_position(self):
        """测试移除仓位"""
        monitor = PositionMonitor()
        monitor.add_position("HK.00700", {
            "entry_price": 100.0,
            "quantity": 100,
            "direction": "BUY"
        })
        
        monitor.remove_position("HK.00700")
        assert "HK.00700" not in monitor.positions
    
    def test_get_all_positions_pnl(self):
        """测试获取所有仓位盈亏"""
        monitor = PositionMonitor()
        
        monitor.add_position("HK.00700", {
            "entry_price": 100.0,
            "quantity": 100,
            "direction": "BUY"
        })
        monitor.add_position("HK.09988", {
            "entry_price": 80.0,
            "quantity": 200,
            "direction": "BUY"
        })
        
        monitor.update_price("HK.00700", 110.0)
        monitor.update_price("HK.09988", 78.0)
        
        all_pnl = monitor.get_all_positions_pnl()
        
        assert "HK.00700" in all_pnl
        assert "HK.09988" in all_pnl
        assert all_pnl["HK.00700"]["unrealized_pnl"] == 1000.0
        assert all_pnl["HK.09988"]["unrealized_pnl"] == -400.0


class TestRiskAlerts:
    """风险警报测试"""
    
    def test_large_loss_alert(self):
        """测试大额亏损警报"""
        risk = IntraTradeRisk(large_loss_threshold=-0.05)
        risk.set_entry_price(100.0, "BUY")
        risk.set_quantity(100)
        
        # 亏损 6%
        risk.update_price(94.0)
        
        alerts = risk.get_alerts()
        assert any(alert['type'] == 'LARGE_LOSS' for alert in alerts)
    
    def test_profit_target_alert(self):
        """测试止盈警报"""
        risk = IntraTradeRisk(profit_target_pct=0.10)
        risk.set_entry_price(100.0, "BUY")
        
        # 盈利 10%
        risk.update_price(110.0)
        
        alerts = risk.get_alerts()
        assert any(alert['type'] == 'PROFIT_TARGET' for alert in alerts)
    
    def test_alert_clear(self):
        """测试警报清除"""
        risk = IntraTradeRisk(large_loss_threshold=-0.05)
        risk.set_entry_price(100.0, "BUY")
        
        # 触发亏损警报
        risk.update_price(94.0)
        assert len(risk.get_alerts()) > 0
        
        # 清除警报
        risk.clear_alerts()
        assert len(risk.get_alerts()) == 0


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
