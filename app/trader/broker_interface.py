#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BrokerInterface — 交易经纪商抽象接口

设计目的:
  将 TradeExecutor 与 Futu SDK 解耦，支持未来扩展到其他经纪商 API。
  通过依赖注入实现，TradeExecutor 不再直接依赖 Futu 相关类。

版本历史:
  2026-05-19 v1: 创建 (Phase 19)
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """订单执行结果"""
    success: bool
    order_id: str = ""
    message: str = ""
    filled_qty: float = 0.0
    filled_price: float = 0.0
    timestamp: float = 0.0


@dataclass
class MarketSnapshot:
    """市场快照数据"""
    code: str
    last_price: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    volume: int = 0
    turnover: float = 0.0
    timestamp: float = 0.0


class BrokerInterface(ABC):
    """
    交易经纪商抽象接口

    所有具体的经纪商实现 (如 FutuBroker, IBroker, etc.) 都需要继承此类。
    TradeExecutor 通过依赖注入使用此接口，而非直接绑定具体经纪商 SDK。
    """

    @abstractmethod
    def place_order(
        self,
        code: str,
        side: str,  # "BUY" / "SELL"
        qty: int,
        price: Optional[float] = None,
        order_type: str = "NORMAL",  # "NORMAL" / "MARKET" / "STOP" / "LIMIT"
        remarks: str = "",
    ) -> OrderResult:
        """
        下单

        Args:
            code: 股票代码
            side: 买卖方向
            qty: 数量
            price: 价格 (市价单可为 None)
            order_type: 订单类型
            remarks: 备注

        Returns:
            OrderResult 对象
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        """
        撤单

        Args:
            order_id: 订单 ID

        Returns:
            OrderResult 对象
        """
        pass

    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        查询订单状态

        Args:
            order_id: 订单 ID

        Returns:
            订单状态字典
        """
        pass

    @abstractmethod
    def get_market_snapshot(self, codes: list) -> Dict[str, MarketSnapshot]:
        """
        获取市场快照

        Args:
            codes: 股票代码列表

        Returns:
            {code: MarketSnapshot} 字典
        """
        pass

    @abstractmethod
    def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息

        Returns:
            账户信息字典 (包含资产、持仓等)
        """
        pass

    @abstractmethod
    def get_lot_size(self, code: str) -> int:
        """
        获取标的每手股数

        Args:
            code: 股票代码

        Returns:
            每手股数 (如港股 1000, 美股 1)
        """
        pass
