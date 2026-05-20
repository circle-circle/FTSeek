"""
智能执行路由 (SmartOrderRouter)

核心理念 (滑点控制):
"在市场流动性差、滑点过大时，不要市价下单，这会让你付出昂贵的代价。"

功能:
1. 获取 OrderBook (五档盘口)。
2. 计算当前滑点率 (Spread / MidPrice)。
3. 如果滑点在安全范围内，使用智能限价单 (Limit Order)。
4. 如果滑点过大，拒绝下单并告警。

配置来源: unified_config.yaml -> strategy_enhancements.smart_execution
"""

import logging
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class ExecutionOrder:
    """生成的优化订单"""
    symbol: str
    order_type: str           # "LIMIT" or "MARKET"
    price: float              # 下单价格 (限价单价格)
    quantity: float
    is_rejected: bool = False # 是否被拒绝
    reject_reason: str = ""   # 拒绝原因


class SmartOrderRouter:
    """智能订单路由器"""

    def __init__(self, config_path: str = None):
        loader = get_config(config_path)
        _cfg = loader.get('strategy_enhancements.smart_execution', {})
        
        self.enabled = _cfg.get('enabled', True)
        self.max_slippage_bps = _cfg.get('max_slippage_bps', 50)
        self.limit_buffer_ticks = _cfg.get('limit_order_buffer_ticks', 2)
        self.depth = _cfg.get('order_book_depth', 5)
        
        # 美股 Tick Size 简化处理 (大部分股票 $0.01)
        self.tick_size = 0.01 
        
        logger.info(
            f"[智能执行] 已初始化 | "
            f"最大滑点={self.max_slippage_bps}bps | "
            f"限价缓冲={self.limit_buffer_ticks} ticks"
        )

    def generate_order(self, symbol: str, side: str, quantity: float, 
                       market_price: float, orderbook: Optional[Dict] = None) -> ExecutionOrder:
        """
        生成优化后的订单

        Args:
            symbol: 股票代码
            side: "BUY" 或 "SELL"
            quantity: 数量
            market_price: 参考价格 (通常是 Last Price)
            orderbook: 盘口数据字典 {'Ask': [p1, p2...], 'Bid': [p1, p2...]}

        Returns:
            ExecutionOrder 优化后的订单
        """
        if not self.enabled or not orderbook:
            # 如果未启用或无盘口数据，回退到市价单
            return ExecutionOrder(
                symbol=symbol, 
                order_type="MARKET", 
                price=market_price, 
                quantity=quantity
            )

        try:
            asks = orderbook.get('Ask', [])
            bids = orderbook.get('Bid', [])

            if not asks or not bids:
                return ExecutionOrder(
                    symbol=symbol, 
                    order_type="MARKET", 
                    price=market_price, 
                    quantity=quantity,
                    is_rejected=False # 数据不全时不拒绝，降级为市价
                )

            best_ask = asks[0]
            best_bid = bids[0]
            mid_price = (best_ask + best_bid) / 2

            # 计算滑点率 (Spread %)
            spread = best_ask - best_bid
            spread_pct = (spread / mid_price) * 10000  # 转换为 BPS (Basis Points)

            order = ExecutionOrder(symbol=symbol, quantity=quantity, price=market_price)

            # 检查滑点
            if spread_pct > self.max_slippage_bps:
                order.is_rejected = True
                order.reject_reason = f"滑点过大: {spread_pct:.0f}bps > {self.max_slippage_bps}bps"
                logger.warning(
                    f"[智能执行] {symbol} 拒绝下单: {order.reject_reason} "
                    f"(Ask={best_ask}, Bid={best_bid})"
                )
                return order

            # 生成智能限价单
            if side == "BUY":
                # 买入：在 Ask1 基础上加几个 Tick 提高成交率，但仍优于直接市价
                order.price = best_ask + (self.limit_buffer_ticks * self.tick_size)
            else:
                # 卖出：在 Bid1 基础上减几个 Tick
                order.price = best_bid - (self.limit_buffer_ticks * self.tick_size)

            order.order_type = "LIMIT"
            logger.debug(
                f"[智能执行] {symbol} 生成限价单: "
                f"Type={order.order_type}, Price={order.price:.2f}, "
                f"滑点={spread_pct:.0f}bps"
            )
            return order

        except Exception as e:
            logger.error(f"[智能执行] {symbol} 生成订单异常: {e}")
            return ExecutionOrder(
                symbol=symbol, 
                order_type="MARKET", 
                price=market_price, 
                quantity=quantity
            )
