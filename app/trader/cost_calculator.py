"""
交易成本计算器

功能:
- 港股交易成本计算 (佣金/印花税/交易费/平台费)
- 滑点估算
- 实际成本分析
"""
import logging
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger("ftseek.trader.cost_calculator")
logger.propagate = False  # 防止日志重复


@dataclass
class CostBreakdown:
    """成本明细"""
    commission: float        # 佣金
    stamp_duty: float        # 印花税
    transaction_fee: float   # 交易费
    platform_fee: float      # 平台费
    system_fee: float        # 系统费
    total_cost: float        # 总成本
    cost_rate: float         # 成本率 (%)
    break_even_price: float  # 保本价


class CostCalculator:
    """交易成本计算器 (港股)"""
    
    # 费率配置 (富途牛牛标准)
    COMMISSION_RATE = 0.0003      # 佣金 0.03% (最低 3 港元)
    MIN_COMMISSION = 3.0          # 最低佣金
    
    STAMP_DUTY_RATE = 0.001       # 印花税 0.1% (双向收取，向上取整到元)
    
    TRANSACTION_FEE_RATE = 0.0000565  # 交易费 0.00565%
    
    PLATFORM_FEE = 0.5            # 平台费 0.5 港元/笔 (最低)
    
    SYSTEM_FEE = 0.5              # 系统费 0.5 港元/笔
    
    def __init__(self, config: Dict = None):
        """初始化
        
        Args:
            config: 自定义费率配置
        """
        if config:
            self.COMMISSION_RATE = config.get('commission_rate', self.COMMISSION_RATE)
            self.MIN_COMMISSION = config.get('min_commission', self.MIN_COMMISSION)
            self.STAMP_DUTY_RATE = config.get('stamp_duty_rate', self.STAMP_DUTY_RATE)
            self.TRANSACTION_FEE_RATE = config.get('transaction_fee_rate', self.TRANSACTION_FEE_RATE)
            self.PLATFORM_FEE = config.get('platform_fee', self.PLATFORM_FEE)
            self.SYSTEM_FEE = config.get('system_fee', self.SYSTEM_FEE)
        
        logger.info("交易成本计算器已初始化")
    
    def calculate_buy_cost(self, 
                           price: float, 
                           quantity: int,
                           currency: str = "HKD") -> CostBreakdown:
        """计算买入成本
        
        Args:
            price: 股价
            quantity: 股数
            currency: 货币
        
        Returns:
            CostBreakdown 成本明细
        """
        trade_value = price * quantity
        
        # 佣金 (最低 3 港元)
        commission = max(trade_value * self.COMMISSION_RATE, self.MIN_COMMISSION)
        
        # 交易费
        transaction_fee = trade_value * self.TRANSACTION_FEE_RATE
        
        # 平台费
        platform_fee = self.PLATFORM_FEE
        
        # 系统费
        system_fee = self.SYSTEM_FEE
        
        # 买入无印花税
        
        total_cost = commission + transaction_fee + platform_fee + system_fee
        cost_rate = total_cost / trade_value if trade_value > 0 else 0
        
        # 保本价 (需要涨多少才能回本)
        break_even_price = price * (1 + cost_rate)
        
        return CostBreakdown(
            commission=commission,
            stamp_duty=0.0,
            transaction_fee=transaction_fee,
            platform_fee=platform_fee,
            system_fee=system_fee,
            total_cost=total_cost,
            cost_rate=cost_rate,
            break_even_price=break_even_price,
        )
    
    def calculate_sell_cost(self,
                            price: float,
                            quantity: int,
                            currency: str = "HKD") -> CostBreakdown:
        """计算卖出成本
        
        Args:
            price: 股价
            quantity: 股数
            currency: 货币
        
        Returns:
            CostBreakdown 成本明细
        """
        trade_value = price * quantity
        
        # 佣金 (最低 3 港元)
        commission = max(trade_value * self.COMMISSION_RATE, self.MIN_COMMISSION)
        
        # 交易费
        transaction_fee = trade_value * self.TRANSACTION_FEE_RATE
        
        # 平台费
        platform_fee = self.PLATFORM_FEE
        
        # 系统费
        system_fee = self.SYSTEM_FEE
        
        # 印花税 (卖出收取，向上取整到元)
        stamp_duty = max(1.0, round(trade_value * self.STAMP_DUTY_RATE))
        
        total_cost = commission + stamp_duty + transaction_fee + platform_fee + system_fee
        cost_rate = total_cost / trade_value if trade_value > 0 else 0
        
        # 实际到手金额
        net_proceeds = trade_value - total_cost
        
        return CostBreakdown(
            commission=commission,
            stamp_duty=stamp_duty,
            transaction_fee=transaction_fee,
            platform_fee=platform_fee,
            system_fee=system_fee,
            total_cost=total_cost,
            cost_rate=cost_rate,
            break_even_price=net_proceeds / quantity if quantity > 0 else 0,
        )
    
    def calculate_round_trip_cost(self,
                                   buy_price: float,
                                   sell_price: float,
                                   quantity: int) -> Dict:
        """计算往返交易成本
        
        Args:
            buy_price: 买入价
            sell_price: 卖出价
            quantity: 股数
        
        Returns:
            成本分析字典
        """
        buy_cost = self.calculate_buy_cost(buy_price, quantity)
        sell_cost = self.calculate_sell_cost(sell_price, quantity)
        
        buy_value = buy_price * quantity
        sell_value = sell_price * quantity
        
        gross_profit = sell_value - buy_value
        net_profit = gross_profit - buy_cost.total_cost - sell_cost.total_cost
        
        return {
            "buy_cost": buy_cost,
            "sell_cost": sell_cost,
            "total_cost": buy_cost.total_cost + sell_cost.total_cost,
            "gross_profit": gross_profit,
            "net_profit": net_profit,
            "roi": net_profit / buy_value if buy_value > 0 else 0,
            "break_even_sell_price": buy_price * (1 + buy_cost.cost_rate) / (1 - sell_cost.cost_rate),
        }
    
    def estimate_slippage(self,
                          price: float,
                          quantity: int,
                          avg_volume: float,
                          volatility: float = 0.02) -> Dict:
        """估算滑点
        
        Args:
            price: 当前价
            quantity: 交易股数
            avg_volume: 日均成交量
            volatility: 波动率 (默认 2%)
        
        Returns:
            滑点估算
        """
        # 成交量占比
        volume_ratio = quantity / avg_volume if avg_volume > 0 else 0
        
        # 滑点估算模型
        # 简单模型：滑点 = 波动率 * sqrt(成交量占比)
        import math
        slippage_rate = volatility * math.sqrt(volume_ratio)
        
        # 限制滑点范围
        slippage_rate = min(slippage_rate, 0.05)  # 最大 5%
        
        slippage_price = price * slippage_rate
        expected_fill_price = price + slippage_price  # 买入时滑点使成本上升
        
        return {
            "slippage_rate": slippage_rate,
            "slippage_price": slippage_price,
            "expected_fill_price": expected_fill_price,
            "slippage_cost": slippage_price * quantity,
            "volume_ratio": volume_ratio,
        }
    
    def get_cost_summary(self) -> Dict:
        """获取费率摘要"""
        return {
            "commission": f"{self.COMMISSION_RATE*100:.2f}% (最低{self.MIN_COMMISSION}港元)",
            "stamp_duty": f"{self.STAMP_DUTY_RATE*100:.2f}% (卖出收取)",
            "transaction_fee": f"{self.TRANSACTION_FEE_RATE*100:.4f}%",
            "platform_fee": f"{self.PLATFORM_FEE}港元",
            "system_fee": f"{self.SYSTEM_FEE}港元",
            "estimated_round_trip": "约 0.15-0.20%",
        }


# 全局单例
_cost_calculator = None

def get_cost_calculator(config: Dict = None) -> CostCalculator:
    """获取成本计算器单例"""
    global _cost_calculator
    if _cost_calculator is None or config:
        _cost_calculator = CostCalculator(config)
    return _cost_calculator
