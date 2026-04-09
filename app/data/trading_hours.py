"""
交易时间管理模块
负责港股交易时间检查、高质量交易时段判断、冷却时间管理
"""
import time
from datetime import datetime
from typing import Dict, Optional
import threading
import logging

from app import config

logger = logging.getLogger("ftseek.data.trading_hours")


class TradingHoursManager:
    """港股交易时间管理器"""
    
    # 港股交易时间 (分钟表示)
    MORNING_START = 9 * 60 + 30    # 09:30
    MORNING_END = 12 * 60          # 12:00
    AFTERNOON_START = 13 * 60      # 13:00
    AFTERNOON_END = 16 * 60        # 16:00
    
    # 高质量交易时间 (排除开收盘噪音)
    OPEN_BUFFER_END = 9 * 60 + 45     # 09:45 - 排除开盘冲击期
    CLOSE_BUFFER_START = 15 * 60 + 45 # 15:45 - 排除收盘撮合期
    
    def __init__(self, default_cooldown: int = 120):
        """
        初始化交易时间管理器
        
        Args:
            default_cooldown: 默认冷却时间 (秒)
        """
        self.default_cooldown = default_cooldown
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        
        # 止损冷却机制
        self._stop_loss_cooldown: Dict[str, float] = {}
        self._stop_loss_fail_count: Dict[str, int] = {}
        
        # 追踪止盈触发时间缓存
        self._trailing_exit_time: Dict[str, float] = {}
    
    def _get_current_time_minutes(self) -> int:
        """获取当前时间的分钟表示"""
        now = datetime.now()
        return now.hour * 60 + now.minute
    
    def is_trading_hours(self) -> bool:
        """
        检查当前是否为港股交易时间
        
        港股交易时间:
        - 上午：09:30 - 12:00
        - 下午：13:00 - 16:00
        
        Returns:
            True: 在交易时间内
            False: 非交易时间 (收盘后、开盘前、周末)
        """
        now = datetime.now()
        weekday = now.weekday()
        
        # 周末不交易
        if weekday >= 5:  # 5=周六，6=周日
            return False
        
        current_time = self._get_current_time_minutes()
        
        # 上午交易时间
        if self.MORNING_START <= current_time < self.MORNING_END:
            return True
        
        # 下午交易时间
        if self.AFTERNOON_START <= current_time < self.AFTERNOON_END:
            return True
        
        return False
    
    def is_high_quality_trading_hours(self) -> bool:
        """
        检查是否为高质量交易时间 (排除开收盘噪音时段)
        
        排除时段:
        - 开盘冲击期：09:30 - 09:45 (流动性差、价格剧烈波动)
        - 收盘撮合期：15:45 - 16:00 (做市商平仓、价格失真)
        
        Returns:
            True: 信号质量可靠的交易时段
            False: 噪音较高的时段
        """
        if not self.is_trading_hours():
            return False
        
        current_time = self._get_current_time_minutes()
        
        # 排除开盘冲击期
        if self.MORNING_START <= current_time < self.OPEN_BUFFER_END:
            return False
        
        # 排除收盘撮合期
        if self.CLOSE_BUFFER_START <= current_time < self.AFTERNOON_END:
            return False
        
        return True
    
    def get_current_cooldown(self, dynamic_loader=None) -> int:
        """
        获取当前冷却时间 (支持动态更新)
        
        Args:
            dynamic_loader: 可选的动态参数加载器
        
        Returns:
            冷却时间 (秒)
        """
        # 如果提供了动态加载器，尝试获取动态冷却时间
        if dynamic_loader is not None:
            try:
                current_params = dynamic_loader.get_current_params()
                return current_params.get('signal_cooldown', self.default_cooldown)
            except Exception:
                pass
        
        return self.default_cooldown
    
    def is_cooled_down(self, code: str, dynamic_loader=None) -> bool:
        """
        检查指定标的是否冷却完成
        
        Args:
            code: 股票代码
            dynamic_loader: 可选的动态参数加载器
        
        Returns:
            True: 冷却完成，可以推送
            False: 仍在冷却中
        """
        now = time.time()
        cooldown = self.get_current_cooldown(dynamic_loader)
        
        with self._cooldown_lock:
            last_time = self._cooldown.get(code, 0)
            if now - last_time < cooldown:
                return False
            return True
    
    def update_cooldown(self, code: str) -> None:
        """
        更新标的冷却时间 (推送成功后调用)
        
        Args:
            code: 股票代码
        """
        with self._cooldown_lock:
            self._cooldown[code] = time.time()
            logger.debug(f"[{code}] 冷却时间已更新")
    
    def get_stop_loss_cooldown(self, code: str) -> float:
        """
        获取止损冷却时间
        
        Args:
            code: 股票代码
        
        Returns:
            上次止损时间戳
        """
        return self._stop_loss_cooldown.get(code, 0)
    
    def set_stop_loss_cooldown(self, code: str) -> None:
        """
        设置止损冷却时间
        
        Args:
            code: 股票代码
        """
        self._stop_loss_cooldown[code] = time.time()
    
    def get_stop_loss_fail_count(self, code: str) -> int:
        """
        获取止损失败次数
        
        Args:
            code: 股票代码
        
        Returns:
            失败次数
        """
        return self._stop_loss_fail_count.get(code, 0)
    
    def increment_stop_loss_fail_count(self, code: str) -> int:
        """
        递增止损失败次数
        
        Args:
            code: 股票代码
        
        Returns:
            更新后的失败次数
        """
        self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
        return self._stop_loss_fail_count[code]
    
    def reset_stop_loss_fail_count(self, code: str) -> None:
        """
        重置止损失败次数
        
        Args:
            code: 股票代码
        """
        self._stop_loss_fail_count.pop(code, None)
    
    def get_stop_loss_cooldown_seconds(self, code: str) -> int:
        """
        根据失败次数获取止损冷却时间 (递增冷却)
        
        Args:
            code: 股票代码
        
        Returns:
            冷却时间 (秒)
        """
        fail_count = self.get_stop_loss_fail_count(code)
        COOLDOWN_SCHEDULE = [5, 15, 30, 60, 60]  # 第 1 次 5s, 第 2 次 15s, ...
        return COOLDOWN_SCHEDULE[min(fail_count, len(COOLDOWN_SCHEDULE) - 1)]
    
    def should_execute_stop_loss(self, code: str) -> bool:
        """
        检查是否应该执行止损 (考虑冷却时间)
        
        Args:
            code: 股票代码
        
        Returns:
            True: 可以执行止损
            False: 仍在冷却中
        """
        now = time.time()
        last_stop_time = self.get_stop_loss_cooldown(code)
        cooldown_seconds = self.get_stop_loss_cooldown_seconds(code)
        
        if now - last_stop_time < cooldown_seconds:
            logger.debug(
                f"[止损冷却] {code} 冷却中，距离上次止损 {now - last_stop_time:.1f}s "
                f"< {cooldown_seconds}s (失败次数={self.get_stop_loss_fail_count(code)})"
            )
            return False
        
        return True
    
    def get_trailing_exit_time(self, code: str) -> float:
        """
        获取追踪止盈触发时间
        
        Args:
            code: 股票代码
        
        Returns:
            触发时间戳
        """
        return self._trailing_exit_time.get(code, 0)
    
    def set_trailing_exit_time(self, code: str) -> None:
        """
        设置追踪止盈触发时间
        
        Args:
            code: 股票代码
        """
        self._trailing_exit_time[code] = time.time()
    
    def is_trailing_cooldown_active(self, code: str, cooldown_sec: int = 900) -> tuple:
        """
        检查追踪止盈冷却期是否激活
        
        Args:
            code: 股票代码
            cooldown_sec: 冷却时间 (秒，默认 15 分钟)
        
        Returns:
            (is_active: bool, remaining_minutes: float)
        """
        trailing_exit_ts = self.get_trailing_exit_time(code)
        elapsed = time.time() - trailing_exit_ts
        
        if elapsed < cooldown_sec:
            remain_min = (cooldown_sec - elapsed) / 60
            return True, remain_min
        
        return False, 0.0
    
    def cleanup_cooldown(self) -> None:
        """清理过期的冷却记录"""
        now = time.time()
        with self._cooldown_lock:
            expired = [
                k for k, t in self._cooldown.items() 
                if now - t > self.default_cooldown * 2
            ]
            for k in expired:
                del self._cooldown[k]
    
    def reset(self, code: str) -> None:
        """
        重置指定标的所有冷却状态
        
        Args:
            code: 股票代码
        """
        with self._cooldown_lock:
            self._cooldown.pop(code, None)
        self._stop_loss_cooldown.pop(code, None)
        self._stop_loss_fail_count.pop(code, None)
        self._trailing_exit_time.pop(code, None)


# 单例实例
_trading_hours_manager: Optional[TradingHoursManager] = None


def get_trading_hours_manager(cooldown: int = 120) -> TradingHoursManager:
    """
    获取交易时间管理器单例
    
    Args:
        cooldown: 默认冷却时间 (秒)
    
    Returns:
        TradingHoursManager 实例
    """
    global _trading_hours_manager
    if _trading_hours_manager is None:
        _trading_hours_manager = TradingHoursManager(default_cooldown=cooldown)
    return _trading_hours_manager
