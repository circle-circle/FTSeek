"""
SmartExecution - 智能执行算法模块

功能:
- VWAP 执行策略 (成交量加权平均价格)
- TWAP 执行策略 (时间加权平均价格)
- 市场冲击最小化
- 执行质量评估
"""
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import math

logger = logging.getLogger("ftseek.v4.smart_execution")


class ExecutionStrategy(str, Enum):
    """执行策略"""
    VWAP = "VWAP"  # 成交量加权平均价格
    TWAP = "TWAP"  # 时间加权平均价格
    PERCENT_OF_VOLUME = "PERCENT_OF_VOLUME"  # 成交量百分比


@dataclass
class ExecutionSlice:
    """执行切片"""
    sequence: int
    quantity: int
    scheduled_time: float
    side: str  # BUY or SELL
    executed: bool = False
    filled_quantity: int = 0
    filled_price: float = 0.0
    actual_time: Optional[float] = None
    
    def mark_executed(self, filled_qty: int, filled_price: float):
        """标记为已执行"""
        self.executed = True
        self.filled_quantity = filled_qty
        self.filled_price = filled_price
        self.actual_time = time.time()
    
    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "quantity": self.quantity,
            "scheduled_time": datetime.fromtimestamp(self.scheduled_time).strftime("%Y-%m-%d %H:%M:%S"),
            "side": self.side,
            "executed": self.executed,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "actual_time": datetime.fromtimestamp(self.actual_time).strftime("%Y-%m-%d %H:%M:%S") if self.actual_time else None,
        }


@dataclass
class ExecutionQuality:
    """执行质量指标"""
    total_quantity: int
    total_value: float
    vwap_benchmark: float
    execution_time_seconds: float
    num_slices: int = 0
    executed_slices: int = 0
    
    @property
    def average_price(self) -> float:
        """平均成交价"""
        if self.total_quantity == 0:
            return 0.0
        return self.total_value / self.total_quantity
    
    def calculate_slippage_bps(self) -> float:
        """
        计算滑点 (基点)
        
        滑点 = (执行价 - 基准价) / 基准价 * 10000
        
        对于 BUY: 执行价 > 基准价 = 正滑点 (不利)
        对于 SELL: 执行价 < 基准价 = 正滑点 (不利)
        """
        if self.vwap_benchmark == 0:
            return 0.0
        
        slippage = (self.average_price - self.vwap_benchmark) / self.vwap_benchmark
        return slippage * 10000  # 转换为基点
    
    def calculate_market_impact(self) -> float:
        """
        计算市场冲击 (简化模型)
        
        市场冲击 ≈ 滑点 - 时机成本
        这里简化为滑点的一部分
        """
        slippage_bps = self.calculate_slippage_bps()
        # 假设市场冲击占滑点的 60%
        return abs(slippage_bps) * 0.6
    
    def get_rating(self) -> str:
        """获取执行质量评级"""
        slippage_bps = self.calculate_slippage_bps()
        
        if slippage_bps < -10:
            return "EXCELLENT"  # 优于基准 10bps 以上
        elif slippage_bps < 0:
            return "GOOD"  # 优于基准
        elif slippage_bps < 10:
            return "FAIR"  # 略差于基准
        elif slippage_bps < 30:
            return "POOR"  # 较差
        else:
            return "VERY_POOR"  # 很差
    
    def to_dict(self) -> dict:
        return {
            "total_quantity": self.total_quantity,
            "total_value": f"{self.total_value:.2f}",
            "average_price": f"{self.average_price:.4f}",
            "vwap_benchmark": f"{self.vwap_benchmark:.4f}",
            "slippage_bps": f"{self.calculate_slippage_bps():.2f}",
            "market_impact_bps": f"{self.calculate_market_impact():.2f}",
            "execution_time_seconds": self.execution_time_seconds,
            "rating": self.get_rating(),
            "num_slices": self.num_slices,
            "executed_slices": self.executed_slices,
        }


class VwapExecution:
    """VWAP 执行策略"""
    
    def __init__(self):
        self.strategy_name = "VWAP"
        self.logger = logging.getLogger("ftseek.v4.vwap_execution")
    
    def calculate_slices(
        self,
        total_quantity: int,
        volume_profile: List[int],
        start_time: Optional[float] = None
    ) -> List[ExecutionSlice]:
        """
        根据成交量分布计算 VWAP 切片
        
        Args:
            total_quantity: 总执行数量
            volume_profile: 各时段预期成交量分布
            start_time: 开始时间
            
        Returns:
            执行切片列表
        """
        if not volume_profile or sum(volume_profile) == 0:
            self.logger.error("无效的成交量分布")
            return []
        
        total_volume = sum(volume_profile)
        slices = []
        current_time = start_time or time.time()
        
        # 计算每个时段的执行量 (按成交量比例分配)
        for i, volume in enumerate(volume_profile):
            ratio = volume / total_volume
            quantity = int(total_quantity * ratio)
            
            # 最后一个切片处理余数
            if i == len(volume_profile) - 1:
                quantity = total_quantity - sum(s.quantity for s in slices)
            
            if quantity > 0:
                slice = ExecutionSlice(
                    sequence=i,
                    quantity=quantity,
                    scheduled_time=current_time,
                    side="BUY"  # 默认，实际使用时应传入
                )
                slices.append(slice)
                
                # 时间间隔 (假设每个时段 5 分钟)
                current_time += 300
        
        self.logger.info(f"VWAP 切片计算完成：{len(slices)}个切片，总量={total_quantity}")
        return slices


class TwapExecution:
    """TWAP 执行策略"""
    
    def __init__(self):
        self.strategy_name = "TWAP"
        self.logger = logging.getLogger("ftseek.v4.twap_execution")
    
    def calculate_slices(
        self,
        total_quantity: int,
        duration_minutes: int,
        num_slices: Optional[int] = None,
        start_time: Optional[float] = None
    ) -> List[ExecutionSlice]:
        """
        计算 TWAP 切片 (时间加权平均)
        
        Args:
            total_quantity: 总执行数量
            duration_minutes: 执行总时长 (分钟)
            num_slices: 切片数量 (默认每 10 分钟一个)
            start_time: 开始时间
            
        Returns:
            执行切片列表
        """
        if num_slices is None:
            num_slices = max(1, duration_minutes // 10)  # 默认每 10 分钟一个切片
        
        quantity_per_slice = total_quantity // num_slices
        time_interval = (duration_minutes * 60) / num_slices  # 秒
        
        slices = []
        current_time = start_time or time.time()
        
        for i in range(num_slices):
            quantity = quantity_per_slice
            
            # 最后一个切片处理余数
            if i == num_slices - 1:
                quantity = total_quantity - sum(s.quantity for s in slices)
            
            slice = ExecutionSlice(
                sequence=i,
                quantity=quantity,
                scheduled_time=current_time,
                side="BUY"
            )
            slices.append(slice)
            
            current_time += time_interval
        
        self.logger.info(
            f"TWAP 切片计算完成：{len(slices)}个切片，"
            f"每切片≈{quantity_per_slice}, 间隔={time_interval:.0f}秒"
        )
        return slices


class MarketImpactModel:
    """市场冲击模型"""
    
    def __init__(self, alpha: float = 0.5, beta: float = 0.5):
        """
        初始化市场冲击模型
        
        Args:
            alpha: 固定冲击系数
            beta: 可变冲击系数
        """
        self.alpha = alpha
        self.beta = beta
        self.logger = logging.getLogger("ftseek.v4.market_impact")
    
    def estimate_impact(
        self,
        quantity: int,
        avg_volume: int,
        volatility: float = 0.02
    ) -> float:
        """
        估算市场冲击 (基点)
        
        简化模型：冲击 = alpha * (qty/vol)^(beta) * volatility
        
        Args:
            quantity: 订单数量
            avg_volume: 时段平均成交量
            volatility: 波动率
            
        Returns:
            预估市场冲击 (基点)
        """
        if avg_volume == 0:
            return 0.0
        
        participation = quantity / avg_volume
        
        # 简化冲击模型
        impact = self.alpha * (participation ** self.beta) * volatility * 100
        
        self.logger.debug(f"市场冲击估算：参与率={participation:.2%}, 冲击={impact:.2f}bps")
        return impact
    
    def calculate_participation_rate(self, order_qty: int, market_volume: int) -> float:
        """
        计算参与率
        
        Args:
            order_qty: 订单数量
            market_volume: 市场成交量
            
        Returns:
            参与率
        """
        if market_volume == 0:
            return 0.0
        return order_qty / market_volume


class SmartExecution:
    """智能执行核心类"""
    
    def __init__(
        self,
        strategy: ExecutionStrategy = ExecutionStrategy.TWAP,
        max_participation_rate: float = 0.10,
    ):
        """
        初始化智能执行
        
        Args:
            strategy: 执行策略 (默认 TWAP)
            max_participation_rate: 最大参与率 (默认 10%)
        """
        self.strategy = strategy
        self.max_participation_rate = max_participation_rate
        
        # 执行状态
        self.is_executing = False
        self.is_paused = False
        self.current_code: Optional[str] = None
        self.current_side: Optional[str] = None
        self.total_quantity: int = 0
        self.executed_quantity: int = 0
        self.remaining_quantity: int = 0
        self.total_value: float = 0.0
        self.start_time: Optional[float] = None
        
        # 切片管理
        self.slices: List[ExecutionSlice] = []
        self.current_slice_index: int = 0
        
        # 执行器
        self.vwap_executor = VwapExecution()
        self.twap_executor = TwapExecution()
        self.impact_model = MarketImpactModel()
        
        # 多订单追踪
        self.active_executions: Dict[str, "SmartExecution"] = {}
        
        self.logger = logging.getLogger("ftseek.v4.smart_execution")
    
    def start_execution(
        self,
        code: str,
        side: str,
        quantity: int,
        duration_minutes: int = 60,
        volume_profile: Optional[List[int]] = None,
        order_id: Optional[str] = None
    ) -> List[ExecutionSlice]:
        """
        开始执行
        
        Args:
            code: 股票代码
            side: 方向 (BUY/SELL)
            quantity: 总数量
            duration_minutes: 执行时长 (分钟)
            volume_profile: 成交量分布 (VWAP 用)
            order_id: 订单 ID
            
        Returns:
            执行切片列表
        """
        self.is_executing = True
        self.is_paused = False
        self.current_code = code
        self.current_side = side
        self.total_quantity = quantity
        self.executed_quantity = 0
        self.remaining_quantity = quantity
        self.total_value = 0.0
        self.start_time = time.time()
        self.current_slice_index = 0
        
        # 根据策略计算切片
        if self.strategy == ExecutionStrategy.VWAP:
            if volume_profile:
                self.slices = self.vwap_executor.calculate_slices(
                    quantity, volume_profile, self.start_time
                )
            else:
                self.logger.warning("VWAP 策略需要成交量分布，回退到 TWAP")
                self.slices = self.twap_executor.calculate_slices(
                    quantity, duration_minutes, start_time=self.start_time
                )
        else:  # TWAP
            self.slices = self.twap_executor.calculate_slices(
                quantity, duration_minutes, start_time=self.start_time
            )
        
        # 更新切片方向
        for slice in self.slices:
            slice.side = side
        
        order_key = order_id or f"{code}_{side}_{int(self.start_time)}"
        self.active_executions[order_key] = self
        
        self.logger.info(
            f"开始执行：{code} {side} {quantity}, 策略={self.strategy.value}, "
            f"切片数={len(self.slices)}"
        )
        
        return self.slices
    
    def get_next_slice(self) -> Optional[ExecutionSlice]:
        """获取下一个待执行切片"""
        if self.is_paused or not self.is_executing:
            return None
        
        while self.current_slice_index < len(self.slices):
            slice = self.slices[self.current_slice_index]
            
            if not slice.executed:
                # 检查是否到执行时间
                if time.time() >= slice.scheduled_time:
                    self.current_slice_index += 1
                    return slice
            
            self.current_slice_index += 1
        
        return None
    
    def update_slice_executed(self, filled_qty: int, filled_price: float):
        """
        更新切片执行结果
        
        Args:
            filled_qty: 成交数量
            filled_price: 成交价格
        """
        self.executed_quantity += filled_qty
        self.remaining_quantity -= filled_qty
        self.total_value += filled_qty * filled_price
        
        if self.remaining_quantity <= 0:
            self.is_executing = False
            self.logger.info(f"执行完成：{self.current_code}, 总量={self.executed_quantity}")
        else:
            self.logger.debug(
                f"切片执行：{filled_qty}@{filled_price}, "
                f"累计={self.executed_quantity}/{self.total_quantity}"
            )
    
    def pause(self):
        """暂停执行"""
        self.is_paused = True
        self.logger.info(f"执行暂停：{self.current_code}")
    
    def resume(self):
        """恢复执行"""
        self.is_paused = False
        self.logger.info(f"执行恢复：{self.current_code}")
    
    def cancel(self):
        """取消执行"""
        self.is_executing = False
        self.logger.info(
            f"执行取消：{self.current_code}, 已执行={self.executed_quantity}/{self.total_quantity}"
        )
    
    def get_execution_vwap(self) -> float:
        """获取执行 VWAP"""
        if self.executed_quantity == 0:
            return 0.0
        return self.total_value / self.executed_quantity
    
    def evaluate_execution(self, vwap_benchmark: float) -> ExecutionQuality:
        """
        评估执行质量
        
        Args:
            vwap_benchmark: 基准 VWAP
            
        Returns:
            执行质量指标
        """
        execution_time = time.time() - self.start_time if self.start_time else 0
        
        executed_slices = sum(1 for s in self.slices if s.executed)
        
        quality = ExecutionQuality(
            total_quantity=self.executed_quantity,
            total_value=self.total_value,
            vwap_benchmark=vwap_benchmark,
            execution_time_seconds=execution_time,
            num_slices=len(self.slices),
            executed_slices=executed_slices,
        )
        
        slippage = quality.calculate_slippage_bps()
        self.logger.info(
            f"执行质量评估：滑点={slippage:.2f}bps, 评级={quality.get_rating()}"
        )
        
        return quality
    
    def get_execution_summary(self, order_id: Optional[str] = None) -> dict:
        """
        获取执行摘要
        
        Args:
            order_id: 订单 ID
            
        Returns:
            执行摘要字典
        """
        return {
            "order_id": order_id,
            "code": self.current_code,
            "side": self.current_side,
            "strategy": self.strategy.value,
            "total_quantity": self.total_quantity,
            "executed_quantity": self.executed_quantity,
            "remaining_quantity": self.remaining_quantity,
            "execution_progress": f"{self.executed_quantity / self.total_quantity:.1%}" if self.total_quantity > 0 else "0%",
            "average_price": f"{self.get_execution_vwap():.4f}",
            "is_executing": self.is_executing,
            "is_paused": self.is_paused,
            "start_time": datetime.fromtimestamp(self.start_time).strftime("%Y-%m-%d %H:%M:%S") if self.start_time else None,
            "total_slices": len(self.slices),
            "executed_slices": sum(1 for s in self.slices if s.executed),
        }
    
    def estimate_market_impact(self, quantity: int, avg_volume: int) -> float:
        """
        估算市场冲击
        
        Args:
            quantity: 订单数量
            avg_volume: 时段平均成交量
            
        Returns:
            预估市场冲击 (基点)
        """
        return self.impact_model.estimate_impact(quantity, avg_volume)
    
    def check_participation_rate(self, order_qty: int, market_volume: int) -> bool:
        """
        检查参与率是否超过限制
        
        Args:
            order_qty: 订单数量
            market_volume: 市场成交量
            
        Returns:
            是否超过限制
        """
        rate = self.impact_model.calculate_participation_rate(order_qty, market_volume)
        return rate <= self.max_participation_rate
    
    def get_status(self) -> dict:
        """获取执行器状态"""
        return {
            "is_executing": self.is_executing,
            "is_paused": self.is_paused,
            "active_executions": len(self.active_executions),
            "strategy": self.strategy.value,
            "max_participation_rate": self.max_participation_rate,
        }


# 全局实例 (可选)
smart_execution: Optional[SmartExecution] = None


def get_smart_execution() -> SmartExecution:
    """获取 SmartExecution 单例"""
    global smart_execution
    if smart_execution is None:
        smart_execution = SmartExecution()
    return smart_execution
