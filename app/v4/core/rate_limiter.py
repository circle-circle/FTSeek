"""
SignalRateLimiter (信号限流器)

控制信号频率，避免过度交易

限流规则:
1. 频率限制：每只股票最多 2 个信号/小时
2. 同方向去重：连续买入信号只保留第一个
3. 冷静期：卖出后 15 分钟内不再接受买入信号
4. 全局开关：可临时禁用所有信号

使用内存存储 (线程安全)，支持并发信号处理
"""
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import deque
from dataclasses import dataclass
import logging

from app.strategy.base import SignalAction


logger = logging.getLogger("ftseek.v4.rate_limiter")
logger.propagate = False  # 防止日志重复


@dataclass
class SignalRecord:
    """信号记录"""
    action: SignalAction
    timestamp: float  # 时间戳 (秒)


class SignalRateLimiter:
    """
    信号限流器
    
    功能:
    - 频率限制：每只股票最多 max_signals_per_hour 个信号/小时
    - 同方向去重：连续相同方向的信号只保留第一个
    - 冷静期：卖出后 cooldown_minutes 分钟内不再接受买入信号
    - 全局开关：可临时禁用所有信号
    """
    
    def __init__(self, max_signals_per_hour: int = 2, cooldown_minutes: int = 15):
        """
        初始化限流器
        
        Args:
            max_signals_per_hour: 每小时最大信号数，默认 2
            cooldown_minutes: 冷静期分钟数，默认 15
        """
        self.max_signals_per_hour = max_signals_per_hour
        self.cooldown_minutes = cooldown_minutes
        self.cooldown_seconds = cooldown_minutes * 60
        
        # 存储每只股票的信号历史
        # code -> deque of SignalRecord
        self._signal_history: Dict[str, deque] = {}
        
        # 全局开关
        self._global_enabled = True
        
        # 线程锁 (保证并发安全)
        self._lock = threading.RLock()
        
        logger.info(
            f"SignalRateLimiter 初始化：max_signals_per_hour={max_signals_per_hour}, "
            f"cooldown_minutes={cooldown_minutes}"
        )
    
    def should_allow(self, code: str, action: SignalAction) -> bool:
        """
        判断信号是否应该被允许
        
        Args:
            code: 股票代码
            action: 信号动作 (BUY/SELL/ALERT)
        
        Returns:
            bool: True 表示允许，False 表示拒绝
        """
        with self._lock:
            # 检查全局开关
            if not self._global_enabled:
                logger.debug(f"信号被拒绝 (全局禁用): {code} {action}")
                return False
            
            # ALERT 信号不受限流
            if action == SignalAction.ALERT:
                return True
            
            # 清理过期记录 (确保后续检查基于最新状态)
            current_time = time.time()
            self._cleanup_expired(code, current_time)
            
            # 检查冷静期 (卖出后一段时间内不允许买入)
            if action == SignalAction.BUY:
                if not self._check_cooldown(code):
                    logger.debug(f"信号被拒绝 (冷静期): {code} {action}")
                    return False
            
            # 检查同方向去重
            if not self._check_duplicate(code, action):
                logger.debug(f"信号被拒绝 (同方向去重): {code} {action}")
                return False
            
            # 检查频率限制
            if not self._check_frequency(code):
                logger.debug(f"信号被拒绝 (频率限制): {code} {action}")
                return False
            
            logger.debug(f"信号被允许：{code} {action}")
            return True
    
    def record_signal(self, code: str, action: SignalAction) -> None:
        """
        记录信号 (在信号被允许并执行后调用)
        
        Args:
            code: 股票代码
            action: 信号动作
        """
        with self._lock:
            # ALERT 信号不记录 (不影响限流)
            if action == SignalAction.ALERT:
                return
            
            current_time = time.time()
            
            # 初始化信号历史
            if code not in self._signal_history:
                self._signal_history[code] = deque()
            
            # 添加新信号记录
            self._signal_history[code].append(
                SignalRecord(action=action, timestamp=current_time)
            )
            
            # 清理过期记录 (保留 1 小时内的记录)
            self._cleanup_expired(code, current_time)
            
            logger.debug(f"信号已记录：{code} {action}")
    
    def reset(self, code: str) -> None:
        """
        重置指定股票的限流状态
        
        Args:
            code: 股票代码
        """
        with self._lock:
            if code in self._signal_history:
                self._signal_history[code].clear()
                logger.debug(f"限流状态已重置：{code}")
    
    def set_global_enabled(self, enabled: bool) -> None:
        """
        设置全局开关
        
        Args:
            enabled: True 启用，False 禁用
        """
        with self._lock:
            self._global_enabled = enabled
            logger.info(f"全局开关已设置：{'启用' if enabled else '禁用'}")
    
    def is_global_enabled(self) -> bool:
        """
        获取全局开关状态
        
        Returns:
            bool: 全局开关状态
        """
        return self._global_enabled
    
    def get_signal_count(self, code: str) -> int:
        """
        获取指定股票当前小时内的信号数量
        
        Args:
            code: 股票代码
        
        Returns:
            int: 信号数量
        """
        with self._lock:
            if code not in self._signal_history:
                return 0
            
            current_time = time.time()
            one_hour_ago = current_time - 3600
            
            # 计算 1 小时内的信号数
            count = sum(
                1 for record in self._signal_history[code]
                if record.timestamp > one_hour_ago
            )
            
            return count
    
    def _check_cooldown(self, code: str) -> bool:
        """
        检查冷静期 (卖出后一段时间内不允许买入)
        
        Args:
            code: 股票代码
        
        Returns:
            bool: True 表示可以买入，False 表示在冷静期内
        """
        if code not in self._signal_history:
            return True
        
        current_time = time.time()
        
        # 查找最近的卖出信号
        for record in reversed(self._signal_history[code]):
            if record.action == SignalAction.SELL:
                time_since_sell = current_time - record.timestamp
                if time_since_sell < self.cooldown_seconds:
                    remaining = self.cooldown_seconds - time_since_sell
                    logger.debug(
                        f"冷静期检查：{code} 卖出后 {time_since_sell:.0f}秒, "
                        f"剩余 {remaining:.0f}秒"
                    )
                    return False
                else:
                    # 最近的卖出信号已超出冷静期
                    break
        
        return True
    
    def _check_duplicate(self, code: str, action: SignalAction) -> bool:
        """
        检查同方向去重 (连续相同方向的信号只保留第一个)
        
        Args:
            code: 股票代码
            action: 信号动作
        
        Returns:
            bool: True 表示不是重复信号，False 表示是重复信号
        """
        if code not in self._signal_history:
            return True
        
        if len(self._signal_history[code]) == 0:
            return True
        
        # 检查最后一个信号的方向
        last_record = self._signal_history[code][-1]
        
        # 如果最后一个信号方向相同，则是重复信号
        if last_record.action == action:
            return False
        
        return True
    
    def _check_frequency(self, code: str) -> bool:
        """
        检查频率限制 (每只股票最多 max_signals_per_hour 个信号/小时)
        
        Args:
            code: 股票代码
        
        Returns:
            bool: True 表示未超限，False 表示已超限
        """
        current_count = self.get_signal_count(code)
        return current_count < self.max_signals_per_hour
    
    def _cleanup_expired(self, code: str, current_time: float) -> None:
        """
        清理过期的信号记录 (保留 1 小时内的记录)
        
        Args:
            code: 股票代码
            current_time: 当前时间戳
        """
        if code not in self._signal_history:
            return
        
        one_hour_ago = current_time - 3600
        
        # 移除过期记录
        while (
            self._signal_history[code] and
            self._signal_history[code][0].timestamp <= one_hour_ago
        ):
            self._signal_history[code].popleft()
        
        # 如果队列为空，删除该股票的记录以节省内存
        if len(self._signal_history[code]) == 0:
            del self._signal_history[code]
    
    def get_status(self, code: str) -> dict:
        """
        获取指定股票的限流状态详情
        
        Args:
            code: 股票代码
        
        Returns:
            dict: 状态信息
        """
        with self._lock:
            status = {
                'code': code,
                'global_enabled': self._global_enabled,
                'signal_count': self.get_signal_count(code),
                'max_signals_per_hour': self.max_signals_per_hour,
                'cooldown_minutes': self.cooldown_minutes,
                'can_buy': True,
                'can_sell': True,
                'reason': None
            }
            
            if code in self._signal_history and len(self._signal_history[code]) > 0:
                # 检查冷静期
                last_record = self._signal_history[code][-1]
                current_time = time.time()
                
                if last_record.action == SignalAction.SELL:
                    time_since_sell = current_time - last_record.timestamp
                    if time_since_sell < self.cooldown_seconds:
                        status['can_buy'] = False
                        status['reason'] = f"冷静期 (卖出后 {time_since_sell:.0f}秒)"
                
                # 检查同方向去重
                if last_record.action == SignalAction.BUY:
                    status['can_buy'] = False
                    status['reason'] = "同方向去重 (最近为买入)"
                elif last_record.action == SignalAction.SELL:
                    status['can_sell'] = False
                    status['reason'] = "同方向去重 (最近为卖出)"
                
                # 检查频率限制
                if status['signal_count'] >= self.max_signals_per_hour:
                    status['can_buy'] = False
                    status['can_sell'] = False
                    status['reason'] = f"频率限制 ({status['signal_count']}/{self.max_signals_per_hour})"
            
            return status
