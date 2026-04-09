#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能订单路由器 (Smart Order Router) - V2 增强版

2026-04-06 P0 优化新增:
1. 市价单滑点上限控制
2. 买卖价差评估
3. 动态限价单偏移

功能:
1. 根据订单金额和流动性选择订单类型
2. 港股：避免盘前市价单
3. 美股：支持盘前限价单
4. 超限自动切换限价单
5. 滑点超限自动切换限价单

订单类型选择逻辑:
- 滑点预估 > MAX_SLIPPAGE_PCT → 限价单 (动态偏移)
- 订单金额 > 90% 限额 → 限价单 (2% 偏移)
- 港股盘前 → 限价单 (1% 偏移)
- 高波动股票 → 限价单
- 其他 → 市价单
"""

import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, time

# 导入滑点配置
from app import config

logger = logging.getLogger("ftseek.smart_router")
logger.propagate = False  # 防止日志重复

# 订单类型
class OrderType(Enum):
    MARKET = "market"      # 市价单
    LIMIT = "limit"        # 限价单


@dataclass
class SmartOrder:
    """智能订单"""
    code: str
    action: str  # BUY/SELL
    quantity: int
    order_type: OrderType
    price: Optional[float] = None  # 限价单价格
    offset_pct: float = 0.0  # 价格偏移百分比
    reason: str = ""  # 选择该订单类型的原因
    
    def to_dict(self) -> Dict:
        return {
            'code': self.code,
            'action': self.action,
            'quantity': self.quantity,
            'order_type': self.order_type.value,
            'price': self.price,
            'offset_pct': self.offset_pct,
            'reason': self.reason,
        }


class SmartOrderRouter:
    """智能订单路由器"""
    
    def __init__(self, max_order_amount: float = 120000.0):
        """
        初始化
        
        Args:
            max_order_amount: 最大订单金额 (HKD)
        """
        self.max_order_amount = max_order_amount
        logger.info(f"智能订单路由器已初始化 | 最大订单金额：HK${max_order_amount:,.0f}")
    
    def route_order(self,
                   code: str,
                   action: str,
                   price: float,
                   quantity: int,
                   market_data: Dict = None) -> SmartOrder:
        """
        智能路由订单 (2026-04-06 增强：滑点控制)

        Args:
            code: 股票代码
            action: BUY/SELL
            price: 当前价格
            quantity: 数量
            market_data: 市场数据 (可选，包含 bid/ask/spread)

        Returns:
            SmartOrder: 智能订单
        """
        # 计算订单金额
        estimated_amount = price * quantity

        # =====================================================================
        # P0-1: 滑点上限检查 (2026-04-06 新增)
        # =====================================================================
        slippage_result = self._check_slippage(code, action, price, quantity, market_data)
        if slippage_result.get('exceeded', False):
            logger.info(
                f"[{code}] 预估滑点超限 ({slippage_result['estimated_slippage']*100:.2f}% > "
                f"{config.MAX_SLIPPAGE_PCT*100:.2f}%), 使用限价单"
            )
            return self._create_limit_order(
                code, action, price, quantity,
                offset_pct=slippage_result.get('suggested_offset', 0.005),
                reason=f"滑点超限 ({slippage_result['estimated_slippage']*100:.2f}%)"
            )

        # =====================================================================
        # 原有逻辑：订单金额检查
        # =====================================================================
        # 1. 检查是否可能超限
        if estimated_amount > self.max_order_amount * 0.9:
            logger.info(f"[{code}] 订单金额超限 ({estimated_amount:.0f} > {self.max_order_amount*0.9:.0f}), 使用限价单")
            return self._create_limit_order(
                code, action, price, quantity,
                offset_pct=0.02,  # 2% 偏移
                reason=f"订单金额超限 (HK${estimated_amount:,.0f})"
            )
        
        # 2. 检查是否为港股
        if code.startswith("HK."):
            # 检查是否在盘前时段
            if self._is_hk_pre_market():
                logger.info(f"[{code}] 港股盘前时段，使用限价单")
                return self._create_limit_order(
                    code, action, price, quantity,
                    offset_pct=0.01,  # 1% 偏移
                    reason="港股盘前时段"
                )
            
            # 检查是否为碎股
            if self._is_odd_lot(code, quantity):
                logger.info(f"[{code}] 碎股交易，使用限价单")
                return self._create_limit_order(
                    code, action, price, quantity,
                    offset_pct=0.015,  # 1.5% 偏移
                    reason="碎股交易"
                )
        
        # 3. 检查市场波动率 (如果有数据)
        if market_data:
            volatility = market_data.get('volatility', 0)
            if volatility and volatility > 0.05:  # 波动率 > 5%
                logger.info(f"[{code}] 高波动 ({volatility*100:.1f}%), 使用限价单")
                return self._create_limit_order(
                    code, action, price, quantity,
                    offset_pct=0.02,
                    reason=f"高波动 ({volatility*100:.1f}%)"
                )
        
        # 4. 默认使用市价单
        logger.debug(f"[{code}] 使用市价单 (金额={estimated_amount:.0f})")
        return SmartOrder(
            code=code,
            action=action,
            quantity=quantity,
            order_type=OrderType.MARKET,
            price=price,
            offset_pct=0.0,
            reason="默认市价单"
        )
    
    def _create_limit_order(self, code: str, action: str, 
                           base_price: float, quantity: int,
                           offset_pct: float, reason: str) -> SmartOrder:
        """
        创建限价单
        
        Args:
            code: 股票代码
            action: BUY/SELL
            base_price: 基准价格
            quantity: 数量
            offset_pct: 价格偏移百分比
            reason: 原因
        
        Returns:
            SmartOrder: 限价单
        """
        # 计算限价单价格
        if action == 'BUY':
            # 买入：价格上浮
            limit_price = base_price * (1 + offset_pct)
        else:
            # 卖出：价格下浮
            limit_price = base_price * (1 - offset_pct)
        
        # 港股价格精度处理
        if code.startswith("HK."):
            limit_price = self._round_hk_price(limit_price, action)
        else:
            # 美股：四舍五入到 0.01
            limit_price = round(limit_price, 2)
        
        return SmartOrder(
            code=code,
            action=action,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            price=limit_price,
            offset_pct=offset_pct,
            reason=reason,
        )
    
    def _round_hk_price(self, price: float, action: str = 'BUY') -> float:
        """
        港股价格精度处理
        
        富途港股价格最小变动单位规则：
        价格范围 | 最小变动单位
        0.01-0.25 | 0.001
        0.25-0.50 | 0.005
        0.50-10.00 | 0.01
        10.00-20.00 | 0.02
        20.00-100.00 | 0.05
        100.00-200.00 | 0.10
        200.00-500.00 | 0.20
        500.00-1000.00 | 0.50
        1000.00-2000.00 | 1.00
        2000.00-5000.00 | 2.00
        5000.00+ | 5.00
        """
        # 确定最小变动单位
        if price < 0.25:
            tick = 0.001
        elif price < 0.50:
            tick = 0.005
        elif price < 10.00:
            tick = 0.01
        elif price < 20.00:
            tick = 0.02
        elif price < 100.00:
            tick = 0.05
        elif price < 200.00:
            tick = 0.10
        elif price < 500.00:
            tick = 0.20
        elif price < 1000.00:
            tick = 0.50
        elif price < 2000.00:
            tick = 1.00
        elif price < 5000.00:
            tick = 2.00
        else:
            tick = 5.00
        
        # 向上或向下取整到最近的 tick
        if action == 'BUY':
            # 买入：向上取整 (更有利成交)
            rounded = (price // tick) * tick
            if rounded < price:
                rounded += tick
        else:
            # 卖出：向下取整 (更有利成交)
            rounded = (price // tick) * tick
        
        # 四舍五入到合适的精度
        if tick >= 1.0:
            return round(rounded, 0)
        elif tick >= 0.1:
            return round(rounded, 1)
        elif tick >= 0.01:
            return round(rounded, 2)
        else:
            return round(rounded, 3)
    
    def _is_hk_pre_market(self) -> bool:
        """
        检查是否为港股盘前时段
        
        港股交易时段:
        - 早市：09:30-12:00
        - 午市：13:00-16:00
        
        盘前时段:
        - 09:00-09:30 (竞价时段)
        - 12:00-13:00 (午休)
        - 16:00 之后
        
        Returns:
            bool: 是否盘前
        """
        now = datetime.now().time()
        
        # 竞价时段 (09:00-09:30)
        if time(9, 0) <= now < time(9, 30):
            return True
        
        # 午休 (12:00-13:00)
        if time(12, 0) <= now < time(13, 0):
            return True
        
        # 收盘后 (16:00 之后)
        if now >= time(16, 0) or now < time(9, 0):
            return True
        
        return False
    
    def _is_odd_lot(self, code: str, quantity: int) -> bool:
        """
        检查是否为碎股

        港股每手股数因股票而异:
        - 腾讯 (00700): 100 股/手
        - 阿里 (09988): 100 股/手
        - 美团 (03690): 100 股/手
        - 泡泡玛特 (09992): 200 股/手
        - 中芯国际 (00981): 500 股/手

        Args:
            code: 股票代码
            quantity: 数量

        Returns:
            bool: 是否碎股
        """
        # 使用 config 中定义的每手股数配置，避免重复定义 (2026-04-06 修复)
        from app.config import get_lot_size
        lot_size = get_lot_size(code)

        # 如果不是每手的整数倍，则为碎股
        return quantity % lot_size != 0

    # =====================================================================
    # P0-1: 滑点检查方法 (2026-04-06 新增)
    # =====================================================================
    def _check_slippage(self, code: str, action: str, price: float,
                        quantity: int, market_data: Dict = None) -> Dict:
        """
        检查预估滑点是否超限

        滑点来源:
        1. 买卖价差 (Bid-Ask Spread)
        2. 市场冲击 (数量过大)
        3. 波动率 (价格快速变化)

        Args:
            code: 股票代码
            action: BUY/SELL
            price: 当前价格
            quantity: 数量
            market_data: 市场数据 (可选，包含 bid/ask/spread/volatility)

        Returns:
            dict: {
                'exceeded': bool - 是否超限
                'estimated_slippage': float - 预估滑点
                'suggested_offset': float - 建议的限价单偏移
            }
        """
        if price <= 0:
            return {'exceeded': False, 'estimated_slippage': 0.0, 'suggested_offset': 0.0}

        estimated_slippage = 0.0

        # 1. 买卖价差滑点 (如果有盘口数据)
        if market_data:
            bid = market_data.get('bid', 0)
            ask = market_data.get('ask', 0)
            spread = market_data.get('spread', 0)

            if bid > 0 and ask > 0:
                # 买卖价差百分比
                spread_pct = (ask - bid) / bid
                # 市价单会吃掉价差，预估滑点为价差的一半
                estimated_slippage += spread_pct / 2
                logger.debug(f"[{code}] 买卖价差滑点：{spread_pct*100:.2f}%")
            elif spread > 0:
                estimated_slippage += spread / 2
        else:
            # 无盘口数据，根据股票类型估算
            if code.startswith('HK.'):
                # 港股：默认价差 0.3% (流动性较好的股票)
                estimated_slippage += 0.003
            else:
                # 美股：默认价差 0.1%
                estimated_slippage += 0.001

        # 2. 市场冲击滑点 (数量过大)
        # 简单估算：数量超过 10000 股，每增加 10000 股增加 0.1% 滑点
        if quantity > 10000:
            impact_slippage = min(0.02, (quantity - 10000) / 10000 * 0.001)
            estimated_slippage += impact_slippage
            logger.debug(f"[{code}] 市场冲击滑点：{impact_slippage*100:.2f}%")

        # 3. 波动率滑点 (如果有数据)
        if market_data and market_data.get('volatility', 0) > 0:
            vol = market_data.get('volatility', 0)
            # 高波动时滑点增加
            if vol > 0.05:
                vol_slippage = min(0.02, (vol - 0.05) * 0.2)
                estimated_slippage += vol_slippage
                logger.debug(f"[{code}] 波动率滑点：{vol_slippage*100:.2f}%")

        # 判断是否超限
        exceeded = estimated_slippage > config.MAX_SLIPPAGE_PCT

        # 计算建议的限价单偏移 (滑点 + 缓冲)
        suggested_offset = min(0.02, estimated_slippage * 1.5 + 0.002)

        logger.info(
            f"[{code}] 滑点检查：预估={estimated_slippage*100:.2f}% | "
            f"阈值={config.MAX_SLIPPAGE_PCT*100:.2f}% | 超限={exceeded}"
        )

        return {
            'exceeded': exceeded,
            'estimated_slippage': estimated_slippage,
            'suggested_offset': suggested_offset,
        }

    def get_order_recommendation(self, code: str, price: float, quantity: int) -> Dict:
        """
        获取订单建议
        
        Args:
            code: 股票代码
            price: 当前价格
            quantity: 数量
        
        Returns:
            dict: 订单建议
        """
        estimated_amount = price * quantity
        
        recommendation = {
            'code': code,
            'price': price,
            'quantity': quantity,
            'estimated_amount': round(estimated_amount, 2),
            'max_amount': self.max_order_amount,
            'usage_pct': round(estimated_amount / self.max_order_amount * 100, 1),
            'recommended_type': 'MARKET',
            'reason': '',
        }
        
        if estimated_amount > self.max_order_amount * 0.9:
            recommendation['recommended_type'] = 'LIMIT'
            recommendation['reason'] = f'订单金额超限 ({recommendation["usage_pct"]:.1f}%)'
            recommendation['suggested_price'] = round(price * 1.02, 2)
            recommendation['suggested_offset'] = '2%'
        
        elif code.startswith('HK.') and self._is_hk_pre_market():
            recommendation['recommended_type'] = 'LIMIT'
            recommendation['reason'] = '港股盘前时段'
            recommendation['suggested_price'] = round(price * 1.01, 2)
            recommendation['suggested_offset'] = '1%'
        
        return recommendation


# 全局单例
smart_router = SmartOrderRouter()
