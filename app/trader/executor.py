"""
交易执行器
对接富途 API 下单，支持港股/美股双市场，自动识别标的市场
"""
import logging
from typing import Optional
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.strategy.base import Signal, SignalAction
from app.trader.position_manager import PositionManager
from app.risk.risk_manager import get_risk_manager

logger = logging.getLogger("ftseek.trader.executor")


@dataclass
class TradeResult:
    """交易结果"""
    success: bool
    order_id: Optional[str] = None
    position_id: Optional[int] = None
    trade_type: Optional[str] = None  # OPEN/CLOSE
    pnl: Optional[float] = None
    message: str = ""
    close_ratio: Optional[float] = None  # 平仓比例 (部分平仓时使用)
    remaining_quantity: Optional[int] = None  # 剩余数量 (部分平仓时使用)


def _detect_market(code: str) -> str:
    """根据标的代码判断市场：HK / US"""
    if code.startswith("US."):
        return "US"
    return "HK"


class TradeExecutor:
    """富途交易执行器 (港股 + 美股双市场)"""

    def __init__(self):
        self._trd_ctx_hk = None
        self._trd_ctx_us = None
        self.position_manager = PositionManager()
        self.risk_manager = get_risk_manager()

    def _get_trd_ctx(self, market: str):
        """获取交易上下文 (按市场懒加载)"""
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket

        if market == "US":
            if self._trd_ctx_us is None:
                self._trd_ctx_us = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.US,
                    security_firm=None,
                )
                logger.info(f"美股交易上下文已创建，环境：{'模拟' if config.TRADE_ENV == 1 else '真实'}")
            return self._trd_ctx_us
        else:
            if self._trd_ctx_hk is None:
                self._trd_ctx_hk = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.HK,
                    security_firm=None,
                )
                logger.info(f"港股交易上下文已创建，环境：{'模拟' if config.TRADE_ENV == 1 else '真实'}")
            return self._trd_ctx_hk

    def _get_qty(self, signal: Signal) -> int:
        """根据市场返回合适的下单数量"""
        market = _detect_market(signal.code)
        if signal.quantity and signal.quantity > 0:
            return signal.quantity
        if market == "US":
            return config.DEFAULT_ORDER_QTY_US
        return config.DEFAULT_ORDER_QTY_HK

    def execute(self, signal: Signal, trade_type: str = 'OPEN') -> TradeResult:
        """
        执行交易信号
        :param signal: 交易信号
        :param trade_type: OPEN(开仓) 或 CLOSE(平仓)
        :return: TradeResult {success, order_id, position_id, trade_type, pnl}
        """
        if signal.action == SignalAction.ALERT:
            return TradeResult(success=False, message="ALERT 类型信号不执行交易")

        # 风控检查 (仅开仓需要)
        if trade_type == 'OPEN':
            risk_result = self.risk_manager.check_before_trade(signal)
            
            if not risk_result.passed:
                failed_checks = [k for k, v in risk_result.details.items() if not v]
                logger.warning(f"风控检查失败：{', '.join(failed_checks)}")
                return TradeResult(
                    success=False,
                    message=f"风控检查失败：{', '.join(failed_checks)}"
                )
            
            logger.debug(f"风控检查通过：{risk_result.details}")

        if trade_type == 'OPEN':
            return self._execute_open(signal)
        else:
            return self._execute_close(signal)

    def _execute_open(self, signal: Signal) -> TradeResult:
        """
        执行开仓交易
        1. 富途下单
        2. 下单成功后创建持仓记录
        3. 返回 position_id
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        try:
            from futu import TrdSide, OrderType, TrdEnv

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            trd_side = TrdSide.BUY if signal.action == SignalAction.BUY else TrdSide.SELL

            qty = self._get_qty(signal)

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 安全检查：金额限制
            estimated_amount = signal.price * qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝下单"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            logger.info(
                f"准备下单：[{market}] {signal.code} {trd_side} "
                f"qty={qty} type={order_type} price={price}"
            )

            ret, data = trd_ctx.place_order(
                price=price,
                qty=qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 创建持仓记录
                entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                action_str = "BUY" if signal.action == SignalAction.BUY else "SELL"
                position_id = self.position_manager.create_position(
                    code=signal.code,
                    action=action_str,
                    quantity=qty,
                    entry_price=signal.price,
                    entry_time=entry_time
                )
                
                msg = f"[{market}] 开仓下单成功，order_id={order_id}, position_id={position_id}"
                logger.info(msg)
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position_id,
                    trade_type='OPEN',
                    message=msg
                )
            else:
                msg = f"[{market}] 开仓下单失败：{data}"
                logger.error(msg)
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 开仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            return TradeResult(success=False, message=msg)

    def _execute_close(self, signal: Signal) -> TradeResult:
        """
        执行平仓交易
        1. 获取持仓
        2. 富途下单平仓
        3. 更新持仓状态
        4. 计算已实现盈亏
        5. 返回 pnl
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        try:
            # 1. 获取持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                msg = f"[{market}] 平仓失败：未找到 {signal.code} 的持仓记录"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            from futu import TrdSide, OrderType, TrdEnv

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 平仓方向与开仓相反
            if position.action == 'BUY':
                trd_side = TrdSide.SELL
            else:
                trd_side = TrdSide.BUY

            # 使用持仓的数量
            qty = position.quantity

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 安全检查：金额限制
            estimated_amount = signal.price * qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝平仓"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            logger.info(
                f"准备平仓：[{market}] {signal.code} {trd_side} "
                f"qty={qty} type={order_type} price={price} position_id={position.id}"
            )

            ret, data = trd_ctx.place_order(
                price=price,
                qty=qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 更新持仓状态并计算盈亏
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                pnl = self.position_manager.close_position(
                    code=signal.code,
                    exit_price=signal.price,
                    exit_time=exit_time
                )
                
                msg = f"[{market}] 平仓下单成功，order_id={order_id}, pnl={pnl:.2f}"
                logger.info(msg)
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position.id,
                    trade_type='CLOSE',
                    pnl=pnl,
                    message=msg
                )
            else:
                msg = f"[{market}] 平仓下单失败：{data}"
                logger.error(msg)
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 平仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            return TradeResult(success=False, message=msg)

    def execute_partial_close(self, signal: Signal, close_ratio: float = 0.5) -> TradeResult:
        """
        执行部分平仓
        :param signal: 交易信号
        :param close_ratio: 平仓比例 (0.5 = 50%)
        :return: TradeResult {success, order_id, pnl, close_ratio, remaining_quantity}
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # 验证平仓比例
        if not 0 < close_ratio <= 1:
            msg = f"无效的平仓比例：{close_ratio}，必须在 (0, 1] 范围内"
            logger.error(msg)
            return TradeResult(success=False, message=msg)

        try:
            # 1. 获取持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                msg = f"[{market}] 部分平仓失败：未找到 {signal.code} 的持仓记录"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            from futu import TrdSide, OrderType, TrdEnv

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 平仓方向与开仓相反
            if position.action == 'BUY':
                trd_side = TrdSide.SELL
            else:
                trd_side = TrdSide.BUY

            # 2. 计算平仓数量 = quantity * close_ratio
            close_qty = int(position.quantity * close_ratio)
            if close_qty < 1:
                close_qty = 1  # 至少平仓 1 股
            
            # 剩余数量
            remaining_qty = position.quantity - close_qty

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 安全检查：金额限制
            estimated_amount = signal.price * close_qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝部分平仓"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            logger.info(
                f"准备部分平仓：[{market}] {signal.code} {trd_side} "
                f"close_qty={close_qty} (ratio={close_ratio}) remaining_qty={remaining_qty} "
                f"type={order_type} price={price} position_id={position.id}"
            )

            # 3. 执行平仓下单
            ret, data = trd_ctx.place_order(
                price=price,
                qty=close_qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 4. 更新持仓 (减少数量，不改变状态)
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                pnl = self.position_manager.partial_close_position(
                    code=signal.code,
                    close_qty=close_qty,
                    exit_price=signal.price,
                    exit_time=exit_time
                )
                
                msg = f"[{market}] 部分平仓下单成功，order_id={order_id}, close_qty={close_qty}, remaining_qty={remaining_qty}, pnl={pnl:.2f}"
                logger.info(msg)
                
                # 5. 返回结果 (包含剩余数量)
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position.id,
                    trade_type='PARTIAL_CLOSE',
                    pnl=pnl,
                    message=msg,
                    close_ratio=close_ratio,
                    remaining_quantity=remaining_qty
                )
            else:
                msg = f"[{market}] 部分平仓下单失败：{data}"
                logger.error(msg)
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 部分平仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            return TradeResult(success=False, message=msg)

    def get_account_info(self, market: str = "HK") -> Optional[dict]:
        """获取账户信息"""
        try:
            from futu import TrdEnv
            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = trd_ctx.accinfo_query(trd_env=trd_env)
            if ret == 0:
                return data.to_dict("records")[0] if len(data) > 0 else {}
            return None
        except Exception as e:
            logger.error(f"获取{market}账户信息失败：{e}")
            return None

    def close(self):
        if self._trd_ctx_hk:
            self._trd_ctx_hk.close()
            self._trd_ctx_hk = None
        if self._trd_ctx_us:
            self._trd_ctx_us.close()
            self._trd_ctx_us = None
