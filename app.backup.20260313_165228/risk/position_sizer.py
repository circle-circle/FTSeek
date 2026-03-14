"""
仓位管理模块 (Position Sizing)

支持多种仓位计算方法：
- 固定比例
- Kelly 公式
- 固定风险
- 波动率调整
- 风险平价
"""
import logging
from typing import Optional, Dict
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ftseek.risk.position_sizer")


class PositionSizingMethod(Enum):
    """仓位计算方法"""
    FIXED_PERCENTAGE = "fixed_percentage"  # 固定比例
    KELLY = "kelly"                        # Kelly 公式
    FIXED_RISK = "fixed_risk"              # 固定风险
    VOLATILITY_ADJUSTED = "volatility_adjusted"  # 波动率调整
    RISK_PARITY = "risk_parity"            # 风险平价


@dataclass
class PositionSize:
    """仓位计算结果"""
    quantity: int           # 交易数量
    position_value: float   # 仓位价值
    risk_amount: float      # 风险金额
    risk_percent: float     # 风险百分比
    method: str             # 使用方法
    confidence: float       # 置信度 (0-1)
    notes: str = ""         # 备注说明


class PositionSizer:
    """
    仓位计算器
    
    根据策略信号、账户信息和风险参数计算最优仓位
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        risk_per_trade: float = 0.02,      # 每笔交易风险 2%
        max_position_percent: float = 0.20, # 单标的最大仓位 20%
        max_total_exposure: float = 0.80,   # 总仓位上限 80%
        method: PositionSizingMethod = PositionSizingMethod.FIXED_RISK,
    ):
        """
        初始化仓位计算器
        
        Args:
            initial_capital: 初始资金
            risk_per_trade: 每笔交易风险比例
            max_position_percent: 单标的最大仓位比例
            max_total_exposure: 总仓位上限
            method: 仓位计算方法
        """
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.max_position_percent = max_position_percent
        self.max_total_exposure = max_total_exposure
        self.method = method

        # 持仓信息
        self.positions: Dict[str, dict] = {}

        logger.info(
            f"仓位计算器已初始化 | 方法={method.value} | "
            f"单笔风险={risk_per_trade:.1%} | 最大仓位={max_position_percent:.1%}"
        )

    def update_capital(self, capital: float):
        """更新当前资金"""
        self.current_capital = capital

    def add_position(self, code: str, quantity: int, entry_price: float):
        """添加持仓记录"""
        self.positions[code] = {
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": entry_price,
        }

    def remove_position(self, code: str):
        """移除持仓记录"""
        if code in self.positions:
            del self.positions[code]

    def update_position_price(self, code: str, current_price: float):
        """更新持仓当前价格"""
        if code in self.positions:
            self.positions[code]["current_price"] = current_price

    def get_total_exposure(self) -> float:
        """计算总仓位暴露"""
        total_value = 0
        for pos in self.positions.values():
            total_value += pos["quantity"] * pos["current_price"]
        return total_value / self.current_capital

    def get_available_capital(self) -> float:
        """计算可用资金"""
        used_capital = sum(
            pos["quantity"] * pos["current_price"]
            for pos in self.positions.values()
        )
        return self.current_capital - used_capital

    def calculate(
        self,
        code: str,
        signal_price: float,
        stop_loss_price: Optional[float] = None,
        volatility: Optional[float] = None,
        win_rate: Optional[float] = None,
        avg_win_loss_ratio: Optional[float] = None,
        signal_strength: float = 1.0,
    ) -> PositionSize:
        """
        计算最优仓位
        
        Args:
            code: 标的代码
            signal_price: 信号价格
            stop_loss_price: 止损价格 (可选)
            volatility: 波动率 (可选，如 ATR)
            win_rate: 策略胜率 (可选，用于 Kelly)
            avg_win_loss_ratio: 盈亏比 (可选，用于 Kelly)
            signal_strength: 信号强度 (0-1)
            
        Returns:
            PositionSize 仓位计算结果
        """
        # 检查总仓位限制
        current_exposure = self.get_total_exposure()
        if current_exposure >= self.max_total_exposure:
            return PositionSize(
                quantity=0,
                position_value=0,
                risk_amount=0,
                risk_percent=0,
                method=self.method.value,
                confidence=1.0,
                notes=f"已达总仓位上限 ({current_exposure:.1%} >= {self.max_total_exposure:.1%})",
            )

        # 根据方法计算仓位
        if self.method == PositionSizingMethod.FIXED_PERCENTAGE:
            size = self._fixed_percentage(signal_price, signal_strength)
        elif self.method == PositionSizingMethod.KELLY:
            size = self._kelly(signal_price, win_rate, avg_win_loss_ratio, signal_strength)
        elif self.method == PositionSizingMethod.FIXED_RISK:
            size = self._fixed_risk(signal_price, stop_loss_price, signal_strength)
        elif self.method == PositionSizingMethod.VOLATILITY_ADJUSTED:
            size = self._volatility_adjusted(signal_price, volatility, signal_strength)
        elif self.method == PositionSizingMethod.RISK_PARITY:
            size = self._risk_parity(signal_price, volatility, signal_strength)
        else:
            size = self._fixed_percentage(signal_price, signal_strength)

        # 应用单标的限制
        max_position_value = self.current_capital * self.max_position_percent
        if size.position_value > max_position_value:
            scale_factor = max_position_value / size.position_value
            size.quantity = int(size.quantity * scale_factor)
            size.position_value = size.quantity * signal_price
            size.risk_amount *= scale_factor
            size.notes += f" | 按单标的限制缩放 ({scale_factor:.2f})"

        # 检查可用资金
        available = self.get_available_capital()
        if size.position_value > available:
            scale_factor = available / size.position_value
            size.quantity = int(size.quantity * scale_factor)
            size.position_value = size.quantity * signal_price
            size.risk_amount *= scale_factor
            size.notes += f" | 按可用资金缩放 ({scale_factor:.2f})"

        # 最小数量检查
        min_qty = 100 if code.startswith("HK.") else 1
        if size.quantity > 0 and size.quantity < min_qty:
            size.quantity = 0
            size.position_value = 0
            size.risk_amount = 0
            size.notes += f" | 低于最小交易数量 ({min_qty})"

        size.method = self.method.value
        return size

    def _fixed_percentage(
        self,
        signal_price: float,
        signal_strength: float,
    ) -> PositionSize:
        """固定比例法"""
        base_percent = self.max_position_percent * 0.5  # 基础 50% 最大仓位
        adjusted_percent = base_percent * signal_strength
        
        position_value = self.current_capital * adjusted_percent
        quantity = int(position_value / signal_price)
        risk_amount = position_value * 0.05  # 假设 5% 风险

        return PositionSize(
            quantity=quantity,
            position_value=position_value,
            risk_amount=risk_amount,
            risk_percent=risk_amount / self.current_capital,
            method="fixed_percentage",
            confidence=0.8,
        )

    def _kelly(
        self,
        signal_price: float,
        win_rate: Optional[float],
        avg_win_loss_ratio: Optional[float],
        signal_strength: float,
    ) -> PositionSize:
        """Kelly 公式法"""
        if win_rate is None or avg_win_loss_ratio is None:
            # 降级为固定比例
            return self._fixed_percentage(signal_price, signal_strength)

        # Kelly 公式：f = (p * b - q) / b
        # p = 胜率，q = 1-p, b = 盈亏比
        p = win_rate
        q = 1 - p
        b = avg_win_loss_ratio

        kelly_fraction = (p * b - q) / b
        
        # 使用半 Kelly (降低风险)
        kelly_fraction = kelly_fraction * 0.5
        
        # 限制在合理范围
        kelly_fraction = max(0, min(kelly_fraction, 0.25))
        
        # 根据信号强度调整
        kelly_fraction *= signal_strength

        position_value = self.current_capital * kelly_fraction
        quantity = int(position_value / signal_price)
        risk_amount = position_value * 0.10  # Kelly 本身已考虑风险

        return PositionSize(
            quantity=quantity,
            position_value=position_value,
            risk_amount=risk_amount,
            risk_percent=risk_amount / self.current_capital,
            method="kelly",
            confidence=0.7 if win_rate and avg_win_loss_ratio else 0.5,
            notes=f"Kelly={kelly_fraction:.2%}",
        )

    def _fixed_risk(
        self,
        signal_price: float,
        stop_loss_price: Optional[float],
        signal_strength: float,
    ) -> PositionSize:
        """固定风险法 (推荐)"""
        if stop_loss_price is None or stop_loss_price == 0:
            # 无止损价，降级为固定比例
            return self._fixed_percentage(signal_price, signal_strength)

        # 计算每股风险
        risk_per_share = abs(signal_price - stop_loss_price)
        
        if risk_per_share <= 0:
            return PositionSize(
                quantity=0,
                position_value=0,
                risk_amount=0,
                risk_percent=0,
                method="fixed_risk",
                confidence=0.5,
                notes="止损价无效",
            )

        # 根据风险金额计算数量
        risk_amount = self.current_capital * self.risk_per_trade * signal_strength
        quantity = int(risk_amount / risk_per_share)
        
        position_value = quantity * signal_price

        return PositionSize(
            quantity=quantity,
            position_value=position_value,
            risk_amount=risk_amount,
            risk_percent=risk_amount / self.current_capital,
            method="fixed_risk",
            confidence=0.9,
            notes=f"止损={stop_loss_price:.2f}, 每股风险={risk_per_share:.2f}",
        )

    def _volatility_adjusted(
        self,
        signal_price: float,
        volatility: Optional[float],
        signal_strength: float,
    ) -> PositionSize:
        """波动率调整法"""
        if volatility is None or volatility <= 0:
            return self._fixed_percentage(signal_price, signal_strength)

        # 目标风险：固定金额风险 / 波动率
        target_risk = self.current_capital * self.risk_per_trade
        volatility_multiplier = 1.0 / (volatility / signal_price)  # 波动率倒数
        
        # 限制波动率乘数范围
        volatility_multiplier = max(0.5, min(volatility_multiplier, 2.0))
        
        position_value = target_risk * volatility_multiplier * signal_strength
        quantity = int(position_value / signal_price)

        return PositionSize(
            quantity=quantity,
            position_value=position_value,
            risk_amount=target_risk,
            risk_percent=target_risk / self.current_capital,
            method="volatility_adjusted",
            confidence=0.8,
            notes=f"波动率={volatility:.2f}, 乘数={volatility_multiplier:.2f}",
        )

    def _risk_parity(
        self,
        signal_price: float,
        volatility: Optional[float],
        signal_strength: float,
    ) -> PositionSize:
        """风险平价法"""
        if volatility is None or volatility <= 0:
            return self._fixed_percentage(signal_price, signal_strength)

        # 风险平价：每个标的贡献相同风险
        n_positions = max(1, len(self.positions) + 1)
        risk_per_position = self.current_capital * self.risk_per_trade / n_positions
        
        # 根据波动率调整
        volatility_pct = volatility / signal_price
        position_value = risk_per_position / volatility_pct
        quantity = int(position_value / signal_price)

        return PositionSize(
            quantity=quantity,
            position_value=position_value,
            risk_amount=risk_per_position,
            risk_percent=risk_per_position / self.current_capital,
            method="risk_parity",
            confidence=0.75,
            notes=f"标的数={n_positions}, 波动率={volatility_pct:.2%}",
        )

    def get_summary(self) -> Dict:
        """获取仓位摘要"""
        total_value = sum(
            pos["quantity"] * pos["current_price"]
            for pos in self.positions.values()
        )
        
        return {
            "total_capital": self.current_capital,
            "used_capital": total_value,
            "available_capital": self.get_available_capital(),
            "total_exposure": total_value / self.current_capital,
            "n_positions": len(self.positions),
            "method": self.method.value,
            "risk_per_trade": self.risk_per_trade,
        }


# 全局单例
position_sizer = PositionSizer()
