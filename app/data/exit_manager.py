"""
退出执行管理模块
负责止损、止盈、追踪止盈的执行逻辑
"""
import logging
from typing import Optional, Dict
from datetime import datetime

from app.strategy.base import Signal, SignalAction
from app.trader.position_manager import PositionManager
from app.trader.executor import TradeExecutor

logger = logging.getLogger("ftseek.data.exit_manager")


class ExitManager:
    """退出执行管理器"""
    
    def __init__(self, position_manager: PositionManager, trade_executor: TradeExecutor):
        """
        初始化退出管理器
        
        Args:
            position_manager: 持仓管理器
            trade_executor: 交易执行器
        """
        self.position_manager = position_manager
        self.trade_executor = trade_executor
        
        # 追踪止盈缓存
        self._trailing_stop_cache: Dict[str, float] = {}
        self._peak_price_cache: Dict[str, float] = {}
    
    def update_trailing_stop(
        self,
        code: str,
        current_price: float,
        position,
        atr_pct: float,
        trail_atr_mult: float = 2.0
    ) -> None:
        """
        激活/上移追踪止盈止损价
        
        规则:
        - 多头：盈利 >= 2% 时激活；追踪距离 = ATR×倍数；只上移不下移
        - 空头：盈利 >= 2% 时激活；追踪距离 = ATR×倍数；只下移不上移
        
        Args:
            code: 股票代码
            current_price: 当前价格
            position: 持仓对象
            atr_pct: ATR 百分比
            trail_atr_mult: 追踪 ATR 倍数
        """
        if position is None:
            return
        
        try:
            entry = position.entry_price
            if entry <= 0:
                return
            
            trail_gap = current_price * atr_pct * trail_atr_mult
            
            if position.action == 'BUY':
                pnl_pct = (current_price - entry) / entry
                # 激活阈值 2%
                if pnl_pct >= 0.02:
                    new_trail = current_price - trail_gap * 1.5
                    old_trail = self._trailing_stop_cache.get(code, 0.0)
                    if new_trail > old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%})"
                        )
                        
            elif position.action == 'SELL':
                pnl_pct = (entry - current_price) / entry
                if pnl_pct >= 0.02:
                    new_trail = current_price + trail_gap * 1.5
                    old_trail = self._trailing_stop_cache.get(code, float('inf'))
                    if new_trail < old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈 - 空头] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%})"
                        )
        
        except Exception as e:
            logger.debug(f"[追踪止盈] 更新异常：{e}")
    
    def check_trailing_stop_triggered(
        self,
        code: str,
        current_price: float,
        position
    ) -> bool:
        """
        检查追踪止盈是否触发
        
        Args:
            code: 股票代码
            current_price: 当前价格
            position: 持仓对象
        
        Returns:
            True: 触发追踪止盈
            False: 未触发
        """
        if position is None:
            return False
        
        trail_price = self._trailing_stop_cache.get(code, 0)
        if trail_price <= 0:
            return False
        
        triggered = (
            (position.action == 'BUY' and current_price < trail_price) or
            (position.action == 'SELL' and current_price > trail_price)
        )
        
        if triggered:
            # 验证盈利
            pnl_rate = (current_price - position.entry_price) / position.entry_price
            if position.action == 'SELL':
                pnl_rate = -pnl_rate
            
            if pnl_rate <= 0:
                logger.debug(f"[追踪止盈跳过] {code} 未盈利 ({pnl_rate:.2%})")
                return False
        
        return triggered
    
    def get_trailing_stop_price(self, code: str) -> float:
        """
        获取追踪止盈价
        
        Args:
            code: 股票代码
        
        Returns:
            追踪止盈价
        """
        return self._trailing_stop_cache.get(code, 0)
    
    def clear_trailing_stop(self, code: str) -> None:
        """
        清除追踪止盈缓存
        
        Args:
            code: 股票代码
        """
        self._trailing_stop_cache.pop(code, None)
        self._peak_price_cache.pop(code, None)
    
    def update_peak_price(self, code: str, current_price: float, position) -> None:
        """
        更新峰值价格缓存
        
        Args:
            code: 股票代码
            current_price: 当前价格
            position: 持仓对象
        """
        if position is None:
            self._peak_price_cache.pop(code, None)
            return
        
        if code not in self._peak_price_cache:
            self._peak_price_cache[code] = current_price
        else:
            if position.action == 'BUY':
                self._peak_price_cache[code] = max(
                    self._peak_price_cache[code], current_price
                )
            else:
                self._peak_price_cache[code] = min(
                    self._peak_price_cache[code], current_price
                )
    
    def get_peak_price(self, code: str) -> Optional[float]:
        """
        获取峰值价格
        
        Args:
            code: 股票代码
        
        Returns:
            峰值价格
        """
        return self._peak_price_cache.get(code)
    
    def execute_stop_loss(
        self,
        code: str,
        current_price: float,
        lot_size: int = 100
    ) -> bool:
        """
        执行止损平仓
        
        Args:
            code: 股票代码
            current_price: 当前价格
            lot_size: 每手股数
        
        Returns:
            True: 执行成功
            False: 执行失败
        """
        position = self.position_manager.get_position(code)
        if position is None:
            return False
        
        # 确保平仓数量为整手
        close_qty = position.quantity
        if close_qty % lot_size != 0:
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止损数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        # 创建止损信号
        pnl_rate = (current_price - position.entry_price) / position.entry_price
        stop_loss_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止损触发 (盈亏={pnl_rate*100:.2f}%)",
            strategy="止损",
            extra={
                "stop_loss": True,
                "entry_price": position.entry_price,
                "pnl_rate": pnl_rate
            }
        )
        
        logger.info(f"[止损尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # 执行平仓
        trade_result = self.trade_executor.execute(stop_loss_signal, 'CLOSE')
        
        if trade_result.success:
            logger.info(
                f"[止损成功] {code} | 入场价={position.entry_price} | "
                f"出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            self.clear_trailing_stop(code)
            return True
        else:
            logger.error(f"[止损失败] {code}: {trade_result.message}")
            return False
    
    def execute_take_profit(
        self,
        code: str,
        current_price: float,
        lot_size: int = 100
    ) -> bool:
        """
        执行止盈平仓
        
        Args:
            code: 股票代码
            current_price: 当前价格
            lot_size: 每手股数
        
        Returns:
            True: 执行成功
            False: 执行失败
        """
        position = self.position_manager.get_position(code)
        if position is None:
            return False
        
        # 确保平仓数量为整手
        close_qty = position.quantity
        if close_qty % lot_size != 0:
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止盈数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        # 创建止盈信号
        pnl_rate = (current_price - position.entry_price) / position.entry_price
        take_profit_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止盈触发 (盈亏={pnl_rate*100:.2f}%)",
            strategy="止盈",
            extra={
                "take_profit": True,
                "entry_price": position.entry_price,
                "pnl_rate": pnl_rate
            }
        )
        
        logger.info(f"[止盈尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # 执行平仓
        trade_result = self.trade_executor.execute(take_profit_signal, 'CLOSE')
        
        if trade_result.success:
            logger.info(
                f"[止盈成功] {code} | 入场价={position.entry_price} | "
                f"出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            self.clear_trailing_stop(code)
            return True
        else:
            logger.error(f"[止盈失败] {code}: {trade_result.message}")
            return False
    
    def execute_partial_take_profit(
        self,
        code: str,
        current_price: float,
        ratio: float,
        hk_lot_sizes: Optional[Dict[str, int]] = None,
        default_lot_size: int = 100
    ) -> bool:
        """
        执行分批止盈平仓
        
        Args:
            code: 股票代码
            current_price: 当前价格
            ratio: 平仓比例 (0.6 = 60%)
            hk_lot_sizes: 港股每手股数配置
            default_lot_size: 默认每手股数
        
        Returns:
            True: 执行成功
            False: 执行失败
        """
        position = self.position_manager.get_position(code)
        if position is None:
            return False
        
        # 获取每手股数
        lot_size = 100
        if hk_lot_sizes:
            lot_size = hk_lot_sizes.get(code, default_lot_size)
        else:
            lot_size = default_lot_size
        
        total_qty = position.quantity
        
        # 计算平仓数量 - 向下取整到整手
        raw_close_qty = int(total_qty * ratio)
        close_qty = (raw_close_qty // lot_size) * lot_size
        
        # 不足一手处理
        if close_qty < lot_size:
            if total_qty >= lot_size * 2:
                close_qty = lot_size
                logger.info(
                    f"[分批止盈调整] {code} 按{ratio:.0%}计算={raw_close_qty}股 不足一手，"
                    f"改为平仓最少 1 手={lot_size}股"
                )
            elif total_qty >= lot_size:
                close_qty = total_qty
                ratio = 1.0
                logger.info(f"[分批止盈调整] {code} 仅 1 手持仓，退化为全仓平仓")
            else:
                logger.warning(
                    f"[分批止盈跳过] {code} 持仓{total_qty}股不足一手，跳过"
                )
                return False
        
        # 检查剩余数量
        remaining_qty = total_qty - close_qty
        if remaining_qty > 0 and remaining_qty < lot_size:
            adjusted_close = total_qty - lot_size
            if adjusted_close >= lot_size:
                close_qty = (adjusted_close // lot_size) * lot_size
                logger.info(
                    f"[分批止盈调整] {code} 平仓量调整为{close_qty}股，保留{lot_size}股整手剩余"
                )
            else:
                close_qty = total_qty
                ratio = 1.0
                logger.info(f"[分批止盈调整] {code} 无法保留整手剩余，改为全仓平仓")
        
        logger.info(
            f"[分批止盈尝试] {code} 持仓={total_qty}股 | "
            f"平仓={close_qty}股 ({ratio:.0%}) | 每手={lot_size}股"
        )
        
        # 创建分批止盈信号
        pnl_rate = (current_price - position.entry_price) / position.entry_price
        partial_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"分批止盈 ({ratio:.0%}) 盈亏={pnl_rate*100:.2f}%",
            strategy="分批止盈",
            extra={
                "partial_take_profit": True,
                "ratio": ratio,
                "entry_price": position.entry_price,
                "pnl_rate": pnl_rate
            }
        )
        
        # 执行部分平仓
        if ratio >= 1.0:
            trade_result = self.trade_executor.execute(partial_signal, 'CLOSE')
        else:
            trade_result = self.trade_executor.execute_partial_close(partial_signal, ratio)
        
        if trade_result.success:
            logger.info(
                f"[分批止盈成功] {code} | 平仓{close_qty}股 ({ratio:.0%}) | "
                f"盈亏={trade_result.pnl:.2f}"
            )
            return True
        else:
            # 失败时尝试全仓平仓
            logger.warning(
                f"[分批止盈失败] {code}: {trade_result.message}，尝试全仓平仓"
            )
            full_signal = Signal(
                code=code,
                name=position.code,
                action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
                price=current_price,
                quantity=position.quantity,
                reason="分批止盈失败后全仓平仓",
                strategy="分批止盈兜底",
                extra={
                    "partial_take_profit": True,
                    "fallback": True,
                    "entry_price": position.entry_price,
                }
            )
            retry_result = self.trade_executor.execute(full_signal, 'CLOSE')
            if retry_result.success:
                logger.info(f"[分批止盈兜底成功] {code} | 全仓平仓{position.quantity}股")
                return True
            else:
                logger.error(f"[分批止盈兜底失败] {code}: {retry_result.message}")
                return False


# 工厂函数
def create_exit_manager(position_manager: PositionManager, trade_executor: TradeExecutor) -> ExitManager:
    """
    创建退出管理器实例
    
    Args:
        position_manager: 持仓管理器
        trade_executor: 交易执行器
    
    Returns:
        ExitManager 实例
    """
    return ExitManager(position_manager, trade_executor)
