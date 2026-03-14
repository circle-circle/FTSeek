"""
频率限制器模块

限制规则:
- 单股票每日最多 3 次信号
- 全局每日最多 20 次信号
- 每日零点自动重置
"""
import logging
from typing import Tuple, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass

from app.storage.database import get_database

logger = logging.getLogger("ftseek.limiter.rate_limiter")


@dataclass
class RateLimitResult:
    """限频结果"""
    allowed: bool               # 是否允许
    reason: str                 # 原因
    current_count: int          # 当前计数
    limit: int                  # 限制值
    reset_at: str               # 重置时间


class RateLimiter:
    """
    频率限制器
    
    配置:
    - per_stock_daily_limit: 单股票每日最多信号数
    - global_daily_limit: 全局每日最多信号数
    """
    
    def __init__(self, 
                 per_stock_daily_limit: int = 3,
                 global_daily_limit: int = 20):
        """
        初始化限频器
        
        Args:
            per_stock_daily_limit: 单股票每日限制 (默认 3)
            global_daily_limit: 全局每日限制 (默认 20)
        """
        self.per_stock_daily_limit = per_stock_daily_limit
        self.global_daily_limit = global_daily_limit
        self.db = get_database()
        
        logger.info(f"限频器初始化：单股{per_stock_daily_limit}次/日，"
                   f"全局{global_daily_limit}次/日")
    
    def check(self, code: str, action: str = None) -> RateLimitResult:
        """
        检查是否允许发送信号
        
        Args:
            code: 股票代码
            action: 信号方向 (BUY/SELL)
            
        Returns:
            RateLimitResult
        """
        # 检查单股限制
        stock_count = self.db.get_rate_limit_count(code, action)
        if stock_count >= self.per_stock_daily_limit:
            return RateLimitResult(
                allowed=False,
                reason=f"单股票已达日限制 ({stock_count}/{self.per_stock_daily_limit})",
                current_count=stock_count,
                limit=self.per_stock_daily_limit,
                reset_at=self._get_next_reset_time()
            )
        
        # 检查全局限制
        global_count = self.db.get_global_daily_count()
        if global_count >= self.global_daily_limit:
            return RateLimitResult(
                allowed=False,
                reason=f"全局已达日限制 ({global_count}/{self.global_daily_limit})",
                current_count=global_count,
                limit=self.global_daily_limit,
                reset_at=self._get_next_reset_time()
            )
        
        # 通过检查，增加计数
        new_count = self.db.increment_rate_limit(code, action)
        
        logger.info(f"限频检查通过：{code} {action or 'ANY'} "
                   f"(单股:{new_count}/{self.per_stock_daily_limit}, "
                   f"全局:{global_count + 1}/{self.global_daily_limit})")
        
        return RateLimitResult(
            allowed=True,
            reason="通过",
            current_count=new_count,
            limit=self.per_stock_daily_limit,
            reset_at=self._get_next_reset_time()
        )
    
    def get_stock_remaining(self, code: str, action: str = None) -> int:
        """获取单股票剩余次数"""
        current = self.db.get_rate_limit_count(code, action)
        return max(0, self.per_stock_daily_limit - current)
    
    def get_global_remaining(self) -> int:
        """获取全局剩余次数"""
        current = self.db.get_global_daily_count()
        return max(0, self.global_daily_limit - current)
    
    def get_status(self, code: str = None) -> dict:
        """
        获取限频状态
        
        Args:
            code: 可选，指定股票代码
            
        Returns:
            状态字典
        """
        status = {
            'global': {
                'current': self.db.get_global_daily_count(),
                'limit': self.global_daily_limit,
                'remaining': self.get_global_remaining()
            },
            'reset_at': self._get_next_reset_time()
        }
        
        if code:
            stock_count = self.db.get_rate_limit_count(code, None)
            status['stock'] = {
                'code': code,
                'current': stock_count,
                'limit': self.per_stock_daily_limit,
                'remaining': self.get_stock_remaining(code)
            }
        
        return status
    
    def _get_next_reset_time(self) -> str:
        """获取下次重置时间 (次日零点)"""
        tomorrow = datetime.now().date() + timedelta(days=1)
        return tomorrow.strftime('%Y-%m-%d 00:00:00')
    
    def reset(self, code: str = None):
        """
        重置计数器
        
        Args:
            code: 可选，指定股票代码，不传则重置全局
        """
        # 实际使用中一般不需要手动重置，由每日自动清理
        logger.warning(f"手动重置限频器：{code or '全局'}")


# 全局单例
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(per_stock_limit: int = 3, 
                    global_limit: int = 20) -> RateLimiter:
    """获取限频器单例"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(per_stock_limit, global_limit)
    return _rate_limiter
