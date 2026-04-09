"""
FTSeek 频率限制器 (由 news_push/rate_limiter.py 移植)

功能:
- per_stock_limit: 每只股票每日最大交易次数
- global_limit: 全局每日最大交易次数

使用内存存储 (线程安全)
"""
import time
import threading
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger("ftseek.limiter.rate_limiter")
logger.propagate = False


@dataclass
class RateLimitResult:
    """限流检查结果"""
    allowed: bool
    reason: str


class RateLimiter:
    """
    交易频率限制器
    
    功能:
    - per_stock_limit: 每只股票每日最大交易次数
    - global_limit: 全局每日最大交易次数
    """
    
    def __init__(self, per_stock_limit: int = 3, global_limit: int = 20):
        """
        初始化限流器
        
        Args:
            per_stock_limit: 每只股票每日最大交易次数，默认 3
            global_limit: 全局每日最大交易次数，默认 20
        """
        self.per_stock_limit = per_stock_limit
        self.global_limit = global_limit
        
        # 存储每只股票的交易次数：{code: {date: count}}
        self._stock_trades: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # 存储全局交易次数：{date: count}
        self._global_trades: Dict[str, int] = defaultdict(int)
        
        # 线程锁 (保证并发安全)
        self._lock = threading.RLock()
        
        logger.info(
            f"RateLimiter 初始化：per_stock_limit={per_stock_limit}, "
            f"global_limit={global_limit}"
        )
    
    def check(self, code: str, timestamp: Optional[float] = None) -> RateLimitResult:
        """
        检查交易是否被允许
        
        Args:
            code: 股票代码
            timestamp: 时间戳 (None 表示当前时间)
        
        Returns:
            RateLimitResult: 检查结果
        """
        with self._lock:
            if timestamp is None:
                timestamp = time.time()
            
            # 获取当日日期字符串 (YYYY-MM-DD)
            today = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
            
            # 检查全局限制
            global_count = self._global_trades.get(today, 0)
            if global_count >= self.global_limit:
                return RateLimitResult(
                    allowed=False,
                    reason=f"全局交易次数超限：{global_count}/{self.global_limit}"
                )
            
            # 检查个股限制
            stock_count = self._stock_trades[code].get(today, 0)
            if stock_count >= self.per_stock_limit:
                return RateLimitResult(
                    allowed=False,
                    reason=f"{code} 交易次数超限：{stock_count}/{self.per_stock_limit}"
                )
            
            return RateLimitResult(allowed=True, reason="检查通过")
    
    def record_trade(self, code: str, timestamp: Optional[float] = None) -> None:
        """
        记录交易 (在交易成功执行后调用)
        
        Args:
            code: 股票代码
            timestamp: 时间戳 (None 表示当前时间)
        """
        with self._lock:
            if timestamp is None:
                timestamp = time.time()
            
            # 获取当日日期字符串 (YYYY-MM-DD)
            today = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
            
            # 记录个股交易
            self._stock_trades[code][today] += 1
            
            # 记录全局交易
            self._global_trades[today] += 1
            
            logger.debug(f"交易已记录：{code} (今日累计: {self._stock_trades[code][today]})")
    
    def get_status(self, code: str, timestamp: Optional[float] = None) -> Dict:
        """
        获取限流状态详情
        
        Args:
            code: 股票代码
            timestamp: 时间戳 (None 表示当前时间)
        
        Returns:
            dict: 状态信息
        """
        with self._lock:
            if timestamp is None:
                timestamp = time.time()
            
            today = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
            
            stock_count = self._stock_trades[code].get(today, 0)
            global_count = self._global_trades.get(today, 0)
            
            return {
                'code': code,
                'today': today,
                'stock_count': stock_count,
                'stock_remaining': max(0, self.per_stock_limit - stock_count),
                'global_count': global_count,
                'global_remaining': max(0, self.global_limit - global_count),
                'can_trade': stock_count < self.per_stock_limit and global_count < self.global_limit
            }
    
    def reset(self, code: str = None, timestamp: Optional[float] = None) -> None:
        """
        重置限流状态
        
        Args:
            code: 股票代码 (None 表示重置全部)
            timestamp: 时间戳 (None 表示当前时间)
        """
        with self._lock:
            if timestamp is None:
                timestamp = time.time()
            
            today = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
            
            if code:
                # 重置个股
                if code in self._stock_trades:
                    self._stock_trades[code].pop(today, None)
                    logger.debug(f"限流状态已重置：{code}")
            else:
                # 重置全部
                self._stock_trades.clear()
                self._global_trades.clear()
                logger.info("全部限流状态已重置")


# 单例
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(per_stock_limit: int = 3, global_limit: int = 20) -> RateLimiter:
    """获取频率限制器单例"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(per_stock_limit, global_limit)
    return _rate_limiter
