"""
SmartExecution 单元测试 - 智能执行算法模块

测试覆盖:
- VWAP 执行策略
- TWAP 执行策略
- 市场冲击最小化
- 执行质量评估
"""
import pytest
import time
from unittest.mock import Mock
from datetime import datetime, timedelta
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.execution.smart_execution import (
    SmartExecution,
    ExecutionStrategy,
    ExecutionSlice,
    ExecutionQuality,
    VwapExecution,
    TwapExecution
)


class TestExecutionStrategy:
    """执行策略枚举测试"""
    
    def test_strategy_enum(self):
        """测试策略枚举"""
        assert ExecutionStrategy.VWAP == "VWAP"
        assert ExecutionStrategy.TWAP == "TWAP"
        assert ExecutionStrategy.PERCENT_OF_VOLUME == "PERCENT_OF_VOLUME"


class TestExecutionSlice:
    """执行切片测试"""
    
    def test_slice_creation(self):
        """测试切片创建"""
        slice = ExecutionSlice(
            sequence=1,
            quantity=100,
            scheduled_time=time.time(),
            side="BUY"
        )
        
        assert slice.sequence == 1
        assert slice.quantity == 100
        assert slice.side == "BUY"
        assert slice.executed == False
    
    def test_slice_mark_executed(self):
        """测试切片标记为已执行"""
        slice = ExecutionSlice(
            sequence=1,
            quantity=100,
            scheduled_time=time.time(),
            side="BUY"
        )
        
        slice.mark_executed(filled_qty=100, filled_price=100.5)
        
        assert slice.executed == True
        assert slice.filled_quantity == 100
        assert slice.filled_price == 100.5
    
    def test_slice_to_dict(self):
        """测试切片转字典"""
        slice = ExecutionSlice(
            sequence=1,
            quantity=100,
            scheduled_time=time.time(),
            side="BUY"
        )
        
        slice_dict = slice.to_dict()
        
        assert slice_dict["sequence"] == 1
        assert slice_dict["quantity"] == 100
        assert slice_dict["side"] == "BUY"


class TestExecutionQuality:
    """执行质量测试"""
    
    def test_quality_creation(self):
        """测试质量指标创建"""
        quality = ExecutionQuality(
            total_quantity=1000,
            total_value=100500.0,
            vwap_benchmark=100.0,
            execution_time_seconds=3600
        )
        
        assert quality.total_quantity == 1000
        assert quality.total_value == 100500.0
        assert quality.vwap_benchmark == 100.0
    
    def test_slippage_calculation(self):
        """测试滑点计算"""
        quality = ExecutionQuality(
            total_quantity=1000,
            total_value=100500.0,  # 平均成交价 100.5
            vwap_benchmark=100.0,
            execution_time_seconds=3600
        )
        
        # 滑点 = (执行价 - 基准价) / 基准价 * 10000 (bps)
        # = (100.5 - 100.0) / 100.0 * 10000 = 50 bps
        slippage = quality.calculate_slippage_bps()
        
        assert abs(slippage - 50.0) < 0.01
    
    def test_market_impact_calculation(self):
        """测试市场冲击计算"""
        quality = ExecutionQuality(
            total_quantity=1000,
            total_value=100500.0,
            vwap_benchmark=100.0,
            execution_time_seconds=3600
        )
        
        impact = quality.calculate_market_impact()
        
        assert impact is not None
        assert isinstance(impact, float)
    
    def test_quality_rating(self):
        """测试质量评级"""
        # 优秀执行 (负滑点)
        quality_good = ExecutionQuality(
            total_quantity=1000,
            total_value=99500.0,  # 平均成交价 99.5 (优于基准)
            vwap_benchmark=100.0,
            execution_time_seconds=3600
        )
        
        # 差执行 (正滑点大)
        quality_bad = ExecutionQuality(
            total_quantity=1000,
            total_value=102000.0,  # 平均成交价 102.0
            vwap_benchmark=100.0,
            execution_time_seconds=3600
        )
        
        assert quality_good.get_rating() in ["EXCELLENT", "GOOD"]
        # 200bps 滑点属于 VERY_POOR
        assert quality_bad.get_rating() in ["POOR", "FAIR", "VERY_POOR"]


class TestVwapExecution:
    """VWAP 执行策略测试"""
    
    def test_vwap_initialization(self):
        """测试 VWAP 初始化"""
        vwap = VwapExecution()
        
        assert vwap.strategy_name == "VWAP"
    
    def test_vwap_slice_calculation(self):
        """测试 VWAP 切片计算"""
        vwap = VwapExecution()
        
        # 基于成交量分布计算切片
        volume_profile = [100, 200, 300, 250, 150]  # 各时段成交量
        total_qty = 1000
        
        slices = vwap.calculate_slices(total_qty, volume_profile)
        
        assert len(slices) == len(volume_profile)
        assert sum(s.quantity for s in slices) == total_qty
        
        # 成交量大的时段应分配更多
        assert slices[2].quantity > slices[0].quantity  # 300 > 100
    
    def test_vwap_with_real_volume(self):
        """测试 VWAP 与实际成交量"""
        vwap = VwapExecution()
        
        # 模拟一天的成交量分布 (简化)
        volume_profile = [50, 100, 150, 200, 250, 300, 250, 200, 150, 100]
        total_qty = 5000
        
        slices = vwap.calculate_slices(total_qty, volume_profile)
        
        assert len(slices) == 10
        assert sum(s.quantity for s in slices) == total_qty
        
        # 验证按比例分配
        total_volume = sum(volume_profile)
        for i, slice in enumerate(slices):
            expected_ratio = volume_profile[i] / total_volume
            actual_ratio = slice.quantity / total_qty
            assert abs(expected_ratio - actual_ratio) < 0.01


class TestTwapExecution:
    """TWAP 执行策略测试"""
    
    def test_twap_initialization(self):
        """测试 TWAP 初始化"""
        twap = TwapExecution()
        
        assert twap.strategy_name == "TWAP"
    
    def test_twap_slice_calculation(self):
        """测试 TWAP 切片计算"""
        twap = TwapExecution()
        
        total_qty = 1000
        duration_minutes = 60
        num_slices = 6  # 每 10 分钟一个切片
        
        slices = twap.calculate_slices(total_qty, duration_minutes, num_slices)
        
        assert len(slices) == num_slices
        assert sum(s.quantity for s in slices) == total_qty
        
        # TWAP 应大致均匀分配 (允许余数差异)
        expected_per_slice = total_qty // num_slices
        for slice in slices:
            # 最后一个切片可能有余数
            assert abs(slice.quantity - expected_per_slice) <= num_slices
    
    def test_twap_time_intervals(self):
        """测试 TWAP 时间间隔"""
        twap = TwapExecution()
        
        total_qty = 1000
        duration_minutes = 60
        num_slices = 6
        
        slices = twap.calculate_slices(total_qty, duration_minutes, num_slices)
        
        # 验证时间间隔均匀
        for i in range(1, len(slices)):
            time_diff = slices[i].scheduled_time - slices[i-1].scheduled_time
            expected_diff = (duration_minutes * 60) / num_slices
            assert abs(time_diff - expected_diff) < 1  # 1 秒误差


class TestSmartExecution:
    """SmartExecution 核心功能测试"""
    
    def test_smart_execution_vwap(self):
        """测试 VWAP 策略执行"""
        executor = SmartExecution(strategy=ExecutionStrategy.VWAP)
        
        volume_profile = [100, 200, 300, 200, 100]
        total_qty = 1000
        
        # 使用 start_execution 来触发切片计算
        slices = executor.start_execution("HK.00700", "BUY", total_qty, volume_profile=volume_profile)
        
        assert len(slices) == 5
        assert sum(s.quantity for s in slices) == total_qty
    
    def test_smart_execution_twap(self):
        """测试 TWAP 策略执行"""
        executor = SmartExecution(strategy=ExecutionStrategy.TWAP)
        
        total_qty = 1000
        duration_minutes = 60
        
        # 使用 start_execution 来触发切片计算
        slices = executor.start_execution("HK.00700", "BUY", total_qty, duration_minutes=duration_minutes)
        
        assert len(slices) > 0
        assert sum(s.quantity for s in slices) == total_qty
    
    def test_execution_tracking(self):
        """测试执行追踪"""
        executor = SmartExecution()
        
        # 开始执行
        executor.start_execution("HK.00700", "BUY", 1000)
        
        assert executor.is_executing == True
        assert executor.current_code == "HK.00700"
        assert executor.remaining_quantity == 1000
    
    def test_slice_execution_update(self):
        """测试切片执行更新"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000)
        
        # 执行第一个切片
        executor.update_slice_executed(100, 100.5)
        
        assert executor.executed_quantity == 100
        assert executor.remaining_quantity == 900
    
    def test_execution_completion(self):
        """测试执行完成"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000)
        
        # 完成所有执行
        executor.update_slice_executed(500, 100.5)
        executor.update_slice_executed(500, 100.6)
        
        assert executor.remaining_quantity == 0
        assert executor.is_executing == False
    
    def test_vwap_calculation(self):
        """测试 VWAP 计算"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000)
        
        # 分批执行
        executor.update_slice_executed(300, 100.0)
        executor.update_slice_executed(300, 101.0)
        executor.update_slice_executed(400, 100.5)
        
        vwap = executor.get_execution_vwap()
        
        # VWAP = (300*100 + 300*101 + 400*100.5) / 1000 = 100.5
        assert abs(vwap - 100.5) < 0.01
    
    def test_execution_quality_evaluation(self):
        """测试执行质量评估"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000)
        
        # 模拟执行
        for i in range(10):
            executor.update_slice_executed(100, 100.0 + i * 0.1)
        
        quality = executor.evaluate_execution(vwap_benchmark=100.5)
        
        assert quality is not None
        assert quality.total_quantity == 1000
        # slippage_bps 是方法不是属性
        assert quality.calculate_slippage_bps() is not None
    
    def test_pause_resume_execution(self):
        """测试暂停/恢复执行"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000)
        
        # 暂停
        executor.pause()
        assert executor.is_paused == True
        
        # 恢复
        executor.resume()
        assert executor.is_paused == False
    
    def test_cancel_execution(self):
        """测试取消执行"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000)
        executor.update_slice_executed(300, 100.5)
        
        # 取消
        executor.cancel()
        
        assert executor.is_executing == False
        assert executor.executed_quantity == 300  # 已执行部分保留
    
    def test_multiple_orders_tracking(self):
        """测试多订单追踪"""
        executor = SmartExecution()
        
        # 开始多个执行
        executor.start_execution("HK.00700", "BUY", 1000, order_id="ORD1")
        executor.start_execution("HK.09988", "SELL", 500, order_id="ORD2")
        
        assert len(executor.active_executions) == 2
    
    def test_execution_summary(self):
        """测试执行摘要"""
        executor = SmartExecution()
        
        executor.start_execution("HK.00700", "BUY", 1000, order_id="ORD1")
        executor.update_slice_executed(500, 100.5)
        
        summary = executor.get_execution_summary("ORD1")
        
        assert summary["order_id"] == "ORD1"
        assert summary["total_quantity"] == 1000
        assert summary["executed_quantity"] == 500
        assert summary["remaining_quantity"] == 500


class TestMarketImpact:
    """市场冲击模型测试"""
    
    def test_impact_estimation(self):
        """测试冲击估算"""
        from app.v4.execution.smart_execution import MarketImpactModel
        
        model = MarketImpactModel()
        
        # 大单应有更大冲击
        impact_small = model.estimate_impact(quantity=100, avg_volume=10000)
        impact_large = model.estimate_impact(quantity=1000, avg_volume=10000)
        
        assert impact_large > impact_small
    
    def test_participation_rate(self):
        """测试参与率计算"""
        from app.v4.execution.smart_execution import MarketImpactModel
        
        model = MarketImpactModel()
        
        # 参与率 = 订单量 / 市场成交量
        rate = model.calculate_participation_rate(order_qty=500, market_volume=10000)
        
        assert abs(rate - 0.05) < 0.001  # 5%


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
