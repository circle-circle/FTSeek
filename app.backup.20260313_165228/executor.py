"""
交易执行器
对接富途 API 下单，支持真实环境和模拟环境
"""
import logging
from typing import Optional

from app import config
from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.trader.executor")


class TradeExecutor:
    """富途交易执行器"""

    def __init__(self):
        self._trd_ctx = None

    def _get_trd_ctx(self):
        """获取交易上下文 (懒加载)"""
        if self._trd_ctx is None:
            try:
                from futu import OpenSecTradeContext, TrdEnv, TrdMarket
                trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
                self._trd_ctx = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.HK,
                    security_firm=None,
                )
                logger.info(f"交易上下文已创建, 环境: {'模拟' if config.TRADE_ENV == 1 else '真实'}")
            except Exception as e:
                logger.error(f"创建交易上下文失败: {e}")
                raise
        return self._trd_ctx

    def execute(self, signal: Signal) -> dict:
        """
        执行交易信号
        :return: {"success": bool, "order_id": str, "message": str}
        """
        if signal.action == SignalAction.ALERT:
            return {"success": False, "order_id": "", "message": "ALERT类型信号不执行交易"}

        try:
            from futu import TrdSide, OrderType, TrdEnv

            trd_ctx = self._get_trd_ctx()
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 确定买卖方向
            trd_side = TrdSide.BUY if signal.action == SignalAction.BUY else TrdSide.SELL

            # 确定订单类型
            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0  # 市价单不需要价格
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 安全检查: 金额限制
            estimated_amount = signal.price * signal.quantity
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 ${estimated_amount:.2f} 超过限额 ${config.MAX_ORDER_AMOUNT:.2f}, 拒绝下单"
                logger.warning(msg)
                return {"success": False, "order_id": "", "message": msg}

            logger.info(
                f"准备下单: {signal.code} {trd_side} "
                f"qty={signal.quantity} type={order_type} price={price}"
            )

            ret, data = trd_ctx.place_order(
                price=price,
                qty=signal.quantity,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                msg = f"下单成功, order_id={order_id}"
                logger.info(msg)
                return {"success": True, "order_id": order_id, "message": msg}
            else:
                msg = f"下单失败: {data}"
                logger.error(msg)
                return {"success": False, "order_id": "", "message": msg}

        except Exception as e:
            msg = f"交易执行异常: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "order_id": "", "message": msg}

    def get_account_info(self) -> Optional[dict]:
        """获取账户信息"""
        try:
            from futu import TrdEnv
            trd_ctx = self._get_trd_ctx()
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = trd_ctx.accinfo_query(trd_env=trd_env)
            if ret == 0:
                return data.to_dict("records")[0] if len(data) > 0 else {}
            return None
        except Exception as e:
            logger.error(f"获取账户信息失败: {e}")
            return None

    def close(self):
        if self._trd_ctx:
            self._trd_ctx.close()
            self._trd_ctx = None
