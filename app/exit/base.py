"""
退出模块基类定义

ExitProfile: 退出配置剖面 (可序列化为 YAML)
ExitDecision: 退出决策 (复用现有结构)
BaseExitModule: 退出模块基类 (所有退出模块必须实现)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExitProfile:
    """
    退出配置剖面 — 可序列化为 YAML，可注入辩论引擎结论
    
    定义一个完整的退出策略配置，包括:
    - 止损类型和参数
    - 止盈类型和参数
    - 时间止损
    - 盘末行为
    
    Attributes:
        name: Profile 名称 (e.g., "stable_15m_base")
        stop_loss_type: 止损类型 "fixed_pct" | "atr_multi" | "chandelier"
        stop_loss_params: 止损参数字典
        take_profit_type: 止盈类型 "fixed_pct" | "scale_out" | "trailing"
        take_profit_params: 止盈参数字典
        time_stop_days: 时间止损天数 (0 = 无时间止损)
        max_holding_minutes: 最大持仓分钟数 (0 = 无上限)
        pre_close_behavior: 盘末行为 "force_close" | "reduce" | "hold"
    """
    name: str
    stop_loss_type: str = "fixed_pct"
    stop_loss_params: Dict[str, Any] = field(default_factory=dict)
    take_profit_type: str = "fixed_pct"
    take_profit_params: Dict[str, Any] = field(default_factory=dict)
    time_stop_days: int = 0
    max_holding_minutes: int = 0
    pre_close_behavior: str = "hold"
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典 (用于 YAML 存储 / 辩论引擎输入)"""
        return {
            "name": self.name,
            "stop_loss_type": self.stop_loss_type,
            "stop_loss_params": self.stop_loss_params,
            "take_profit_type": self.take_profit_type,
            "take_profit_params": self.take_profit_params,
            "time_stop_days": self.time_stop_days,
            "max_holding_minutes": self.max_holding_minutes,
            "pre_close_behavior": self.pre_close_behavior,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExitProfile:
        """从字典反序列化"""
        return cls(
            name=data.get("name", "unknown"),
            stop_loss_type=data.get("stop_loss_type", "fixed_pct"),
            stop_loss_params=data.get("stop_loss_params", {}),
            take_profit_type=data.get("take_profit_type", "fixed_pct"),
            take_profit_params=data.get("take_profit_params", {}),
            time_stop_days=data.get("time_stop_days", 0),
            max_holding_minutes=data.get("max_holding_minutes", 0),
            pre_close_behavior=data.get("pre_close_behavior", "hold"),
        )


@dataclass
class ExitDecision:
    """
    退出决策 — 复用现有 exit_policies.py 的结构
    
    Attributes:
        action: 动作 "NO_ACTION" | "REDUCE" | "CLOSE"
        qty_pct: 平仓比例 (0.0-1.0)
        reason: 退出原因 (e.g., "ATR_STOP", "TAKE_PROFIT")
        priority: 优先级 (数字越大越优先)
        metadata: 额外信息 (e.g., pnl_pct, atr_value)
    """
    action: str = "NO_ACTION"
    qty_pct: float = 0.0
    reason: str = ""
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "action": self.action,
            "qty_pct": self.qty_pct,
            "reason": self.reason,
            "priority": self.priority,
            "metadata": self.metadata,
        }


class BaseExitModule(ABC):
    """
    退出模块基类 — 所有退出模块必须实现
    
    设计原则:
    1. evaluate() 是纯函数，不修改输入，不写文件/DB
    2. 参数通过 ExitProfile 注入
    3. 输出统一为 ExitDecision
    
    Usage:
        class MyExitModule(BaseExitModule):
            name = "my_exit"
            
            def evaluate(self, position, ctx, profile):
                # 纯计算逻辑
                ...
                return ExitDecision(action="CLOSE", reason="MY_RULE")
    """
    
    name: str = ""
    
    @abstractmethod
    def evaluate(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile: ExitProfile,
    ) -> ExitDecision:
        """
        评估是否应该退出
        
        Args:
            position: 持仓对象 (至少有 avg_entry_price, direction, quantity)
            ctx: 上下文 {current_price, atr, holding_minutes, ...}
            profile: 退出配置剖面
        
        Returns:
            ExitDecision: 退出决策
        """
        ...
    
    def get_stop_price(
        self,
        entry_price: float,
        atr: float,
        direction: str,
        profile: ExitProfile,
    ) -> float:
        """
        计算止损价
        
        Args:
            entry_price: 入场价
            atr: 当前 ATR
            direction: "LONG" | "SHORT"
            profile: 退出配置
        
        Returns:
            float: 止损价
        """
        sl_type = profile.stop_loss_type
        params = profile.stop_loss_params
        
        if sl_type == "fixed_pct":
            pct = params.get("pct", -0.05)  # 默认 -5%
            if direction == "LONG":
                return entry_price * (1 + pct)
            else:
                return entry_price * (1 - pct)
        
        elif sl_type == "atr_multi":
            multiplier = params.get("multiplier", 2.0)
            if direction == "LONG":
                return entry_price - atr * multiplier
            else:
                return entry_price + atr * multiplier
        
        elif sl_type == "chandelier":
            # Chandelier 追踪止损需要 peak_price (由 ctx 传入)
            multiplier = params.get("multiplier", 2.5)
            if direction == "LONG":
                return entry_price - atr * multiplier  # 初始值，实际会被 peak 替换
            else:
                return entry_price + atr * multiplier
        
        else:
            logger.warning(f"Unknown stop_loss_type: {sl_type}, using default -5%")
            return entry_price * 0.95 if direction == "LONG" else entry_price * 1.05
    
    def get_take_profit_price(
        self,
        entry_price: float,
        atr: float,
        direction: str,
        profile: ExitProfile,
    ) -> float:
        """
        计算止盈价 (仅用于固定止盈，分层止盈由 ScaleOutExitModule 处理)
        
        Args:
            entry_price: 入场价
            atr: 当前 ATR
            direction: "LONG" | "SHORT"
            profile: 退出配置
        
        Returns:
            float: 止盈价
        """
        tp_type = profile.take_profit_type
        params = profile.take_profit_params
        
        if tp_type == "fixed_pct":
            pct = params.get("target_pct", 0.10)  # 默认 +10%
            if direction == "LONG":
                return entry_price * (1 + pct)
            else:
                return entry_price * (1 - pct)
        
        else:
            # 其他类型 (scale_out/trailing) 不返回单一价格
            return 0.0
    
    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        """将值限制在 [min_val, max_val] 范围内"""
        return max(min_val, min(max_val, value))
