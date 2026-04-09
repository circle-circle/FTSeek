"""
市场熔断器 (Market Circuit Breaker)

功能:
- 检测极端市场行情 (恒指单日跌幅>5%)
- 暂停所有新开仓
- 恢复交易条件判断
- 熔断状态通知

使用示例:
    from app.risk.market_circuit_breaker import circuit_breaker
    if circuit_breaker.is_trading_suspended():
        print("交易暂停：市场熔断已触发")
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict
import threading

logger = logging.getLogger("ftseek.risk.market_circuit_breaker")
logger.propagate = False


class MarketCircuitBreaker:
    """
    市场熔断器

    监控恒指 (HSI) 涨跌幅，触发条件时暂停交易
    """

    def __init__(self):
        """初始化市场熔断器"""
        # 熔断阈值
        self.hsi_drop_threshold = -0.05  # 恒指跌幅 5%
        self.hsi_surge_threshold = 0.05  # 恒指涨幅 5% (可选)

        # 熔断状态
        self._is_suspended = False
        self._suspend_reason: Optional[str] = None
        self._suspend_time: Optional[datetime] = None
        self._last_check_time: Optional[datetime] = None
        self._hsi_last_close: Optional[float] = None

        # 配置
        self._auto_resume_enabled = True
        self._auto_resume_minutes = 30  # 30 分钟后自动恢复

        # 锁
        self._lock = threading.RLock()

        # 回调函数
        self._on_suspend_callbacks = []
        self._on_resume_callbacks = []

        logger.info("市场熔断器已初始化")
        logger.info(f"  恒指跌幅阈值：{self.hsi_drop_threshold:.1%}")
        logger.info(f"  自动恢复时间：{self._auto_resume_minutes}分钟")

    def check_hsi_drop(self, hsi_current: float, hsi_close_yesterday: float) -> bool:
        """
        检查恒指跌幅是否触发熔断

        Args:
            hsi_current: 恒指当前点位
            hsi_close_yesterday: 恒指昨日收盘

        Returns:
            True 如果触发熔断
        """
        if hsi_close_yesterday <= 0:
            return False

        change_rate = (hsi_current - hsi_close_yesterday) / hsi_close_yesterday

        with self._lock:
            self._last_check_time = datetime.now()

            if change_rate < self.hsi_drop_threshold:
                # 触发熔断
                self._trigger_suspend(f"恒指跌幅 {change_rate:.1%} 超过阈值 {self.hsi_drop_threshold:.1%}")
                return True
            elif self._is_suspended and self._auto_resume_enabled:
                # 检查是否应该自动恢复
                if self._suspend_time and (datetime.now() - self._suspend_time) > timedelta(minutes=self._auto_resume_minutes):
                    self._resume("熔断时间超过 30 分钟，自动恢复交易")
                    return False

        return False

    def check_hsi_intraday_drop(self, hsi_current: float, hsi_open: float) -> bool:
        """
        检查恒指日内跌幅是否触发熔断

        Args:
            hsi_current: 恒指当前点位
            hsi_open: 恒指今日开盘

        Returns:
            True 如果触发熔断
        """
        if hsi_open <= 0:
            return False

        change_rate = (hsi_current - hsi_open) / hsi_open

        with self._lock:
            self._last_check_time = datetime.now()

            if change_rate < self.hsi_drop_threshold:
                self._trigger_suspend(f"恒指日内跌幅 {change_rate:.1%} 超过阈值 {self.hsi_drop_threshold:.1%}")
                return True

        return False

    def _trigger_suspend(self, reason: str):
        """
        触发熔断，暂停交易

        Args:
            reason: 熔断原因
        """
        if self._is_suspended:
            return  # 已在熔断状态

        self._is_suspended = True
        self._suspend_reason = reason
        self._suspend_time = datetime.now()

        logger.critical(f"[市场熔断] 交易暂停：{reason}")
        logger.critical(f"[市场熔断] 熔断时间：{self._suspend_time.isoformat()}")

        # 触发回调
        for callback in self._on_suspend_callbacks:
            try:
                callback(reason)
            except Exception as e:
                logger.error(f"熔断回调失败：{e}")

    def _resume(self, reason: str):
        """
        恢复交易

        Args:
            reason: 恢复原因
        """
        if not self._is_suspended:
            return  # 未在熔断状态

        suspend_duration = (datetime.now() - self._suspend_time) if self._suspend_time else None

        self._is_suspended = False
        old_reason = self._suspend_reason
        self._suspend_reason = None
        self._suspend_time = None

        logger.info(f"[市场熔断] 交易恢复：{reason}")
        logger.info(f"[市场熔断] 熔断持续时间：{suspend_duration}")
        logger.info(f"[市场熔断] 原熔断原因：{old_reason}")

        # 触发回调
        for callback in self._on_resume_callbacks:
            try:
                callback(reason)
            except Exception as e:
                logger.error(f"恢复回调失败：{e}")

    def is_trading_suspended(self) -> bool:
        """
        检查交易是否被暂停

        Returns:
            True 如果交易被暂停
        """
        with self._lock:
            # 检查自动恢复
            if self._is_suspended and self._auto_resume_enabled:
                if self._suspend_time and (datetime.now() - self._suspend_time) > timedelta(minutes=self._auto_resume_minutes):
                    self._resume("熔断时间超过 30 分钟，自动恢复交易")
                    return False

            return self._is_suspended

    def get_suspend_status(self) -> Dict:
        """
        获取熔断状态详情

        Returns:
            包含熔断状态的字典
        """
        with self._lock:
            suspend_duration = None
            if self._suspend_time:
                suspend_duration = str(datetime.now() - self._suspend_time)

            return {
                "is_suspended": self._is_suspended,
                "reason": self._suspend_reason,
                "suspend_time": self._suspend_time.isoformat() if self._suspend_time else None,
                "suspend_duration": suspend_duration,
                "last_check_time": self._last_check_time.isoformat() if self._last_check_time else None,
                "auto_resume_enabled": self._auto_resume_enabled,
                "auto_resume_minutes": self._auto_resume_minutes,
            }

    def manual_suspend(self, reason: str):
        """
        手动触发熔断

        Args:
            reason: 熔断原因
        """
        with self._lock:
            self._trigger_suspend(f"[手动触发] {reason}")

    def manual_resume(self, reason: str):
        """
        手动恢复交易

        Args:
            reason: 恢复原因
        """
        with self._lock:
            self._resume(f"[手动恢复] {reason}")

    def on_suspend(self, callback):
        """
        注册熔断回调

        Args:
            callback: 回调函数，签名 (reason: str)
        """
        self._on_suspend_callbacks.append(callback)

    def on_resume(self, callback):
        """
        注册恢复回调

        Args:
            callback: 回调函数，签名 (reason: str)
        """
        self._on_resume_callbacks.append(callback)

    def set_auto_resume(self, enabled: bool, minutes: int = 30):
        """
        设置自动恢复

        Args:
            enabled: 是否启用自动恢复
            minutes: 自动恢复时间 (分钟)
        """
        with self._lock:
            self._auto_resume_enabled = enabled
            self._auto_resume_minutes = minutes
            logger.info(f"自动恢复已{'启用' if enabled else '禁用'}, 时间：{minutes}分钟")


# 全局单例
circuit_breaker: Optional[MarketCircuitBreaker] = None


def get_circuit_breaker() -> MarketCircuitBreaker:
    """获取市场熔断器单例"""
    global circuit_breaker
    if circuit_breaker is None:
        circuit_breaker = MarketCircuitBreaker()
    return circuit_breaker


def check_market_circuit_breaker(hsi_current: float, hsi_close_yesterday: float) -> bool:
    """
    检查市场熔断 (便捷函数)

    Args:
        hsi_current: 恒指当前点位
        hsi_close_yesterday: 恒指昨日收盘

    Returns:
        True 如果触发熔断 (交易应暂停)
    """
    breaker = get_circuit_breaker()
    return breaker.check_hsi_drop(hsi_current, hsi_close_yesterday)


def is_trading_suspended() -> bool:
    """
    检查交易是否被暂停 (便捷函数)

    Returns:
        True 如果交易被暂停
    """
    breaker = get_circuit_breaker()
    return breaker.is_trading_suspended()
