#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FutuBroker — 富途经纪商实现

基于 Futu OpenD API 的具体实现。

版本历史:
  2026-05-19 v1: 创建 (Phase 19)
"""

import logging
import time
from typing import Optional, Dict, Any, List
from datetime import datetime

from app.trader.broker_interface import BrokerInterface, OrderResult, MarketSnapshot

logger = logging.getLogger(__name__)


class FutuBroker(BrokerInterface):
    """
    富途经纪商实现

    封装 Futu OpenD API 调用，提供统一的交易接口。
    """

    def __init__(self, trd_ctx, config):
        """
        初始化 FutuBroker

        Args:
            trd_ctx: Futu 交易上下文
            config: 配置对象
        """
        self._trd_ctx = trd_ctx
        self._config = config
        self._lot_size_cache: Dict[str, int] = {}

    def place_order(
        self,
        code: str,
        side: str,
        qty: int,
        price: Optional[float] = None,
        order_type: str = "NORMAL",
        remarks: str = "",
    ) -> OrderResult:
        """下单 (富途实现)"""
        try:
            from futu import TrdSide, OrderType, TrdEnv

            # 确定交易环境
            trd_env = TrdEnv.SIMULATE if getattr(self._config, 'TRADE_ENV', 1) == 1 else TrdEnv.REAL

            # 确定订单类型
            if order_type == "MARKET":
                ft_order_type = OrderType.MARKET
            elif order_type == "STOP":
                ft_order_type = OrderType.STOP
            elif order_type == "LIMIT":
                ft_order_type = OrderType.LIMIT
            else:
                ft_order_type = OrderType.NORMAL

            # 确定买卖方向
            if side == "BUY":
                trd_side = TrdSide.BUY
            else:
                trd_side = TrdSide.SELL

            # 调用富途 API 下单
            ret, data = self._trd_ctx.place_order(
                code=code,
                qty=qty,
                price=price if price else 0.0,
                trd_side=trd_side,
                trd_env=trd_env,
                order_type=ft_order_type,
                remarks=remarks,
            )

            if ret == 0:
                order_id = str(data.iloc[0]['order_id']) if not data.empty else ""
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    message="OK",
                    timestamp=time.time(),
                )
            else:
                return OrderResult(
                    success=False,
                    message=str(data) if data is not None else "Unknown error",
                    timestamp=time.time(),
                )
        except Exception as e:
            logger.error(f"[FutuBroker] 下单失败：{e}", exc_info=True)
            return OrderResult(
                success=False,
                message=str(e),
                timestamp=time.time(),
            )

    def cancel_order(self, order_id: str) -> OrderResult:
        """撤单 (富途实现)"""
        try:
            from futu import TrdEnv
            trd_env = TrdEnv.SIMULATE if getattr(self._config, 'TRADE_ENV', 1) == 1 else TrdEnv.REAL

            ret, data = self._trd_ctx.cancel_order(
                order_id=order_id,
                trd_env=trd_env,
            )

            if ret == 0:
                return OrderResult(
                    success=True,
                    message="OK",
                    timestamp=time.time(),
                )
            else:
                return OrderResult(
                    success=False,
                    message=str(data) if data is not None else "Unknown error",
                    timestamp=time.time(),
                )
        except Exception as e:
            logger.error(f"[FutuBroker] 撤单失败：{e}", exc_info=True)
            return OrderResult(
                success=False,
                message=str(e),
                timestamp=time.time(),
            )

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """查询订单状态 (富途实现)"""
        try:
            from futu import TrdEnv
            trd_env = TrdEnv.SIMULATE if getattr(self._config, 'TRADE_ENV', 1) == 1 else TrdEnv.REAL

            ret, data = self._trd_ctx.order_list_query(
                order_id=order_id,
                trd_env=trd_env,
            )

            if ret == 0 and data is not None and not data.empty:
                row = data.iloc[0]
                return {
                    'order_id': row.get('order_id', ''),
                    'code': row.get('code', ''),
                    'qty': row.get('qty', 0),
                    'price': row.get('price', 0.0),
                    'status': row.get('status', ''),
                    'filled_qty': row.get('filled_qty', 0),
                    'filled_price': row.get('filled_price', 0.0),
                }
            else:
                return {'error': str(data) if data is not None else 'Order not found'}
        except Exception as e:
            logger.error(f"[FutuBroker] 查询订单状态失败：{e}", exc_info=True)
            return {'error': str(e)}

    def get_market_snapshot(self, codes: list) -> Dict[str, MarketSnapshot]:
        """获取市场快照 (富途实现)"""
        result = {}
        try:
            from futu import SubType
            ret, data = self._trd_ctx.get_market_snapshot(codes)

            if ret == 0 and data is not None and not data.empty:
                for _, row in data.iterrows():
                    code = row.get('code', '')
                    result[code] = MarketSnapshot(
                        code=code,
                        last_price=row.get('last_price', 0.0),
                        open_price=row.get('open_price', 0.0),
                        high_price=row.get('high_price', 0.0),
                        low_price=row.get('low_price', 0.0),
                        volume=row.get('volume', 0),
                        turnover=row.get('turnover', 0.0),
                        timestamp=time.time(),
                    )
        except Exception as e:
            logger.error(f"[FutuBroker] 获取市场快照失败：{e}", exc_info=True)

        return result

    def get_account_info(self) -> Dict[str, Any]:
        """获取账户信息 (富途实现)"""
        try:
            from futu import TrdEnv
            trd_env = TrdEnv.SIMULATE if getattr(self._config, 'TRADE_ENV', 1) == 1 else TrdEnv.REAL

            ret, data = self._trd_ctx.acc_list(trd_env=trd_env)

            if ret == 0 and data is not None and not data.empty:
                row = data.iloc[0]
                return {
                    'acc_id': row.get('acc_id', ''),
                    'total_assets': row.get('total_assets', 0.0),
                    'cash': row.get('cash', 0.0),
                    'market_val': row.get('market_val', 0.0),
                    'long_mv': row.get('long_mv', 0.0),
                    'short_mv': row.get('short_mv', 0.0),
                    'pl': row.get('pl', 0.0),
                }
            else:
                return {'error': str(data) if data is not None else 'Account info not available'}
        except Exception as e:
            logger.error(f"[FutuBroker] 获取账户信息失败：{e}", exc_info=True)
            return {'error': str(e)}

    def get_lot_size(self, code: str) -> int:
        """获取标的每手股数 (富途实现)"""
        # 先查缓存
        if code in self._lot_size_cache:
            return self._lot_size_cache[code]

        try:
            from futu import KLType
            ret, data = self._trd_ctx.get_market_snapshot([code])

            if ret == 0 and data is not None and not data.empty:
                lot_size = int(data.iloc[0].get('lot_size', 1))
                self._lot_size_cache[code] = lot_size
                return lot_size
        except Exception as e:
            logger.warning(f"[FutuBroker] 获取 lot_size 失败：{e}")

        # 默认值
        default_lot = 1
        if code.startswith("HK."):
            default_lot = 1000  # 港股默认 1000
        return default_lot
