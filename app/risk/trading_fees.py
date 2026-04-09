#!/usr/bin/env python3
"""
交易费用计算模块

港股交易费用结构:
1. 佣金 (Commission): 0.03% (万分之三)，最低 3 港币
2. 印花税 (Stamp Duty): 0.1% (仅卖出)
3. 交易费 (Transaction Levy): 0.005%
4. 联交所交易费 (Exchange Fee): 0.005%
5. 交易系统使用费 (Trading Fee): 0.005%

总计:
- 买入: 约 0.05%
- 卖出: 约 0.16% (含印花税)
- 双向合计: 约 0.21%
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("ftseek.trading_fees")
logger.propagate = False  # 防止日志重复


@dataclass
class TradingFees:
    """交易费用配置"""
    # 佣金
    commission_rate: float = 0.0003      # 万分之三
    min_commission: float = 3.0          # 最低佣金 3 港币
    
    # 印花税 (仅卖出)
    stamp_duty_rate: float = 0.001       # 0.1%
    
    # 交易费
    transaction_levy_rate: float = 0.00005   # 0.005%
    
    # 联交所交易费
    exchange_fee_rate: float = 0.00005       # 0.005%
    
    # 交易系统使用费
    trading_fee_rate: float = 0.00005        # 0.005%
    
    # 滑点估算
    slippage_rate: float = 0.001             # 0.1%
    
    @property
    def buy_fee_rate(self) -> float:
        """买入费用率 (不含滑点)"""
        return (
            self.commission_rate +
            self.transaction_levy_rate +
            self.exchange_fee_rate +
            self.trading_fee_rate
        )
    
    @property
    def sell_fee_rate(self) -> float:
        """卖出费用率 (含印花税，不含滑点)"""
        return (
            self.commission_rate +
            self.stamp_duty_rate +
            self.transaction_levy_rate +
            self.exchange_fee_rate +
            self.trading_fee_rate
        )
    
    @property
    def total_fee_rate(self) -> float:
        """双向总费用率 (不含滑点)"""
        return self.buy_fee_rate + self.sell_fee_rate
    
    @property
    def total_with_slippage(self) -> float:
        """双向总费用率 (含滑点)"""
        return self.total_fee_rate + self.slippage_rate * 2


# 默认费用配置
DEFAULT_FEES = TradingFees()


def calculate_buy_fees(
    price: float,
    quantity: int,
    fees: TradingFees = None
) -> dict:
    """
    计算买入费用
    
    Args:
        price: 买入价格
        quantity: 买入数量
        fees: 费用配置
        
    Returns:
        费用明细
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    notional = price * quantity
    
    # 佣金 (最低 3 港币)
    commission = max(notional * fees.commission_rate, fees.min_commission)
    
    # 交易费
    transaction_levy = notional * fees.transaction_levy_rate
    exchange_fee = notional * fees.exchange_fee_rate
    trading_fee = notional * fees.trading_fee_rate
    
    # 总费用
    total_fee = commission + transaction_levy + exchange_fee + trading_fee
    
    # 滑点
    slippage = notional * fees.slippage_rate
    
    return {
        "notional": notional,
        "commission": commission,
        "transaction_levy": transaction_levy,
        "exchange_fee": exchange_fee,
        "trading_fee": trading_fee,
        "stamp_duty": 0,  # 买入无印花税
        "slippage": slippage,
        "total_fee": total_fee,
        "total_with_slippage": total_fee + slippage,
        "fee_rate": total_fee / notional if notional > 0 else 0
    }


def calculate_sell_fees(
    price: float,
    quantity: int,
    fees: TradingFees = None
) -> dict:
    """
    计算卖出费用
    
    Args:
        price: 卖出价格
        quantity: 卖出数量
        fees: 费用配置
        
    Returns:
        费用明细
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    notional = price * quantity
    
    # 佣金 (最低 3 港币)
    commission = max(notional * fees.commission_rate, fees.min_commission)
    
    # 印花税 (仅卖出)
    stamp_duty = notional * fees.stamp_duty_rate
    
    # 交易费
    transaction_levy = notional * fees.transaction_levy_rate
    exchange_fee = notional * fees.exchange_fee_rate
    trading_fee = notional * fees.trading_fee_rate
    
    # 总费用
    total_fee = commission + stamp_duty + transaction_levy + exchange_fee + trading_fee
    
    # 滑点
    slippage = notional * fees.slippage_rate
    
    return {
        "notional": notional,
        "commission": commission,
        "transaction_levy": transaction_levy,
        "exchange_fee": exchange_fee,
        "trading_fee": trading_fee,
        "stamp_duty": stamp_duty,
        "slippage": slippage,
        "total_fee": total_fee,
        "total_with_slippage": total_fee + slippage,
        "fee_rate": total_fee / notional if notional > 0 else 0
    }


def calculate_total_fees(
    entry_price: float,
    exit_price: float,
    quantity: int,
    fees: TradingFees = None
) -> dict:
    """
    计算双向总费用
    
    Args:
        entry_price: 入场价
        exit_price: 出场价
        quantity: 数量
        fees: 费用配置
        
    Returns:
        总费用明细
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    buy_fees = calculate_buy_fees(entry_price, quantity, fees)
    sell_fees = calculate_sell_fees(exit_price, quantity, fees)
    
    total_fee = buy_fees["total_with_slippage"] + sell_fees["total_with_slippage"]
    total_notional = entry_price * quantity
    
    return {
        "buy_fees": buy_fees,
        "sell_fees": sell_fees,
        "total_fee": total_fee,
        "total_fee_rate": total_fee / total_notional if total_notional > 0 else 0
    }


def adjust_stop_loss_for_fees(
    entry_price: float,
    stop_loss_pct: float,
    quantity: int = 100,
    fees: TradingFees = None
) -> float:
    """
    调整止损价以考虑交易费用
    
    Args:
        entry_price: 入场价
        stop_loss_pct: 原始止损比例 (如 -0.03 表示 -3%)
        quantity: 数量
        fees: 费用配置
        
    Returns:
        调整后的止损价
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    # 计算总费用率
    total_fee_rate = fees.total_with_slippage
    
    # 如果止损已经设置，需要额外考虑费用
    # 实际止损 = 原止损 - 费用
    # 例如: 原止损 -3%，费用 0.21%，实际需要 -3.21% 才能保证只亏 3%
    adjusted_stop_loss_pct = stop_loss_pct - total_fee_rate
    
    # 计算止损价
    stop_price = entry_price * (1 + adjusted_stop_loss_pct)
    
    return stop_price


def adjust_take_profit_for_fees(
    entry_price: float,
    take_profit_pct: float,
    quantity: int = 100,
    fees: TradingFees = None
) -> float:
    """
    调整止盈价以考虑交易费用
    
    Args:
        entry_price: 入场价
        take_profit_pct: 原始止盈比例 (如 0.05 表示 +5%)
        quantity: 数量
        fees: 费用配置
        
    Returns:
        调整后的止盈价
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    # 计算总费用率
    total_fee_rate = fees.total_with_slippage
    
    # 如果要实现目标盈利，需要额外考虑费用
    # 例如: 目标盈利 5%，费用 0.21%，实际需要涨 5.21% 才能净赚 5%
    adjusted_take_profit_pct = take_profit_pct + total_fee_rate
    
    # 计算止盈价
    take_profit_price = entry_price * (1 + adjusted_take_profit_pct)
    
    return take_profit_price


def calculate_break_even_price(
    entry_price: float,
    quantity: int = 100,
    fees: TradingFees = None
) -> float:
    """
    计算保本价格 (覆盖交易费用)
    
    Args:
        entry_price: 入场价
        quantity: 数量
        fees: 费用配置
        
    Returns:
        保本价格
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    # 保本需要覆盖买入费用 + 卖出费用
    total_fee_rate = fees.total_with_slippage
    
    # 保本价 = 入场价 × (1 + 总费用率)
    break_even_price = entry_price * (1 + total_fee_rate)
    
    return break_even_price


def calculate_min_profit_target(
    entry_price: float,
    min_profit_pct: float = 0.005,
    quantity: int = 100,
    fees: TradingFees = None
) -> float:
    """
    计算最小盈利目标价
    
    Args:
        entry_price: 入场价
        min_profit_pct: 最小净盈利比例 (默认 0.5%)
        quantity: 数量
        fees: 费用配置
        
    Returns:
        目标价格
    """
    if fees is None:
        fees = DEFAULT_FEES
    
    # 最小目标 = 费用 + 最小盈利
    total_fee_rate = fees.total_with_slippage
    target_rate = total_fee_rate + min_profit_pct
    
    return entry_price * (1 + target_rate)


def get_fee_summary(fees: TradingFees = None) -> str:
    """获取费用摘要"""
    if fees is None:
        fees = DEFAULT_FEES
    
    return f"""
港股交易费用摘要:
━━━━━━━━━━━━━━━━━━━━
买入费用:
  佣金: {fees.commission_rate:.2%} (最低 HK${fees.min_commission:.0f})
  交易费: {fees.transaction_levy_rate:.3%}
  联交所费: {fees.exchange_fee_rate:.3%}
  系统使用费: {fees.trading_fee_rate:.3%}
  买入合计: {fees.buy_fee_rate:.2%}

卖出费用:
  佣金: {fees.commission_rate:.2%}
  印花税: {fees.stamp_duty_rate:.2%}
  交易费: {fees.transaction_levy_rate:.3%}
  联交所费: {fees.exchange_fee_rate:.3%}
  系统使用费: {fees.trading_fee_rate:.3%}
  卖出合计: {fees.sell_fee_rate:.2%}

双向总费用: {fees.total_fee_rate:.2%}
含滑点总计: {fees.total_with_slippage:.2%}
━━━━━━━━━━━━━━━━━━━━

示例 (HK$100,000 交易):
  买入费用: HK${100000 * fees.buy_fee_rate:.0f}
  卖出费用: HK${100000 * fees.sell_fee_rate:.0f}
  双向合计: HK${100000 * fees.total_fee_rate:.0f}
  保本涨幅: {fees.total_with_slippage:.2%}
"""