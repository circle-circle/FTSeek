"""
持仓追踪管理模块 (Position Tracker)
负责持仓期间的峰值价格、追踪止盈价、动态止损止盈价等状态管理
"""
import logging
from typing import Dict, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger("ftseek.data.position_tracker")
logger.propagate = False


class PositionTracker:
    """持仓状态追踪器"""

    def __init__(self):
        """初始化持仓追踪器"""
        # 峰值价格缓存：code -> peak_price
        self._peak_price_cache: Dict[str, float] = {}

        # 追踪止盈价缓存：code -> trailing_stop_price
        self._trailing_stop_cache: Dict[str, float] = {}

        # 动态止损止盈价缓存：code -> (stop_loss_price, take_profit_price)
        self._dynamic_exit_cache: Dict[str, Tuple[float, float]] = {}

        logger.info("持仓追踪器已初始化")

    def update_peak_price(
        self,
        code: str,
        current_price: float,
        action: str
    ) -> float:
        """
        更新持仓期间的峰值价格

        Args:
            code: 股票代码
            current_price: 当前价格
            action: 持仓方向 ('BUY' 或 'SELL')

        Returns:
            更新后的峰值价格
        """
        if code not in self._peak_price_cache:
            self._peak_price_cache[code] = current_price
        else:
            if action == 'BUY':
                # 多头：更新最高价
                self._peak_price_cache[code] = max(
                    self._peak_price_cache[code], current_price
                )
            else:
                # 空头：更新最低价
                self._peak_price_cache[code] = min(
                    self._peak_price_cache[code], current_price
                )

        return self._peak_price_cache[code]

    def get_peak_price(self, code: str) -> Optional[float]:
        """获取峰值价格"""
        return self._peak_price_cache.get(code)

    def clear_peak_price(self, code: str) -> None:
        """清除峰值价格缓存"""
        self._peak_price_cache.pop(code, None)

    def update_trailing_stop(
        self,
        code: str,
        current_price: float,
        position,
        atr_pct: float,
        trail_atr_mult: float = 2.0
    ) -> Optional[float]:
        """
        激活/上移追踪止盈止损价

        Args:
            code: 股票代码
            current_price: 当前价格
            position: 持仓对象 (需有 entry_price 和 action 属性)
            atr_pct: ATR 百分比
            trail_atr_mult: 追踪 ATR 倍数

        Returns:
            更新后的追踪止盈价，或 None (未激活)
        """
        if position is None:
            return None

        try:
            entry = position.entry_price
            if entry <= 0:
                return None

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
                        return new_trail

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
                        return new_trail

            return self._trailing_stop_cache.get(code)

        except Exception as e:
            logger.debug(f"[追踪止盈] 更新异常：{e}")
            return None

    def get_trailing_stop(self, code: str) -> Optional[float]:
        """获取追踪止盈价"""
        return self._trailing_stop_cache.get(code)

    def clear_trailing_stop(self, code: str) -> None:
        """清除追踪止盈缓存"""
        self._trailing_stop_cache.pop(code, None)

    def is_trailing_stop_triggered(
        self,
        code: str,
        current_price: float,
        action: str
    ) -> bool:
        """
        检查追踪止盈是否触发

        Args:
            code: 股票代码
            current_price: 当前价格
            action: 持仓方向 ('BUY' 或 'SELL')

        Returns:
            True: 追踪止盈已触发
            False: 未触发
        """
        trail_price = self._trailing_stop_cache.get(code, 0)
        if trail_price <= 0:
            return False

        if action == 'BUY':
            return current_price < trail_price
        else:
            return current_price > trail_price

    def set_dynamic_exit_prices(
        self,
        code: str,
        stop_loss_price: float,
        take_profit_price: float
    ) -> None:
        """
        设置动态止损止盈价格

        Args:
            code: 股票代码
            stop_loss_price: 止损价
            take_profit_price: 止盈价
        """
        self._dynamic_exit_cache[code] = (stop_loss_price, take_profit_price)
        logger.debug(
            f"[动态止损] {code} 止损={stop_loss_price:.3f}, "
            f"止盈={take_profit_price:.3f}"
        )

    def get_dynamic_exit_prices(
        self,
        code: str
    ) -> Optional[Tuple[float, float]]:
        """
        获取动态止损止盈价格

        Args:
            code: 股票代码

        Returns:
            (stop_loss_price, take_profit_price) 或 None
        """
        return self._dynamic_exit_cache.get(code)

    def clear_dynamic_exit(self, code: str) -> None:
        """清除动态止损止盈缓存"""
        self._dynamic_exit_cache.pop(code, None)

    def clear_all(self, code: str) -> None:
        """
        清除所有缓存

        Args:
            code: 股票代码
        """
        self.clear_peak_price(code)
        self.clear_trailing_stop(code)
        self.clear_dynamic_exit(code)
        logger.debug(f"[{code}] 持仓追踪缓存已清理")


# 全局单例
_position_tracker: Optional[PositionTracker] = None


def get_position_tracker() -> PositionTracker:
    """获取持仓追踪器单例"""
    global _position_tracker
    if _position_tracker is None:
        _position_tracker = PositionTracker()
    return _position_tracker
