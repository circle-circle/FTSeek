"""
统一限速管理器

全局协调所有数据源的API调用,实现:
- 令牌桶算法统一限速
- 使用率监控和告警
- 自动等待令牌补充
"""
import time
import threading
import logging
from typing import Dict, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class RateLimiter:
    """令牌桶限速器 (线程安全)"""
    
    def __init__(self, max_tokens: int, refill_interval: float, name: str = ""):
        """
        Args:
            max_tokens: 桶容量 (最大调用次数)
            refill_interval: 补充间隔 (秒/token)
            name: 限速器名称
        """
        self.name = name
        self.max_tokens = max_tokens
        self.tokens = float(max_tokens)
        self.refill_interval = refill_interval
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()
        
        # 统计信息
        self.total_calls = 0
        self.total_wait_time = 0.0
        self.last_reset = datetime.now()
    
    def acquire(self, tokens: int = 1, timeout: float = 120.0) -> bool:
        """
        获取令牌
        
        Args:
            tokens: 需要获取的令牌数
            timeout: 最大等待时间 (秒)
        
        Returns:
            bool: 是否成功获取
        """
        deadline = time.monotonic() + timeout
        
        while True:
            with self.lock:
                self._refill()
                
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    self.total_calls += 1
                    return True
                
                # 计算需要等待的时间
                wait_time = (tokens - self.tokens) * self.refill_interval
                
                if time.monotonic() + wait_time > deadline:
                    logger.warning(f"[{self.name}] 限速等待超时 (需要{tokens} tokens, 剩余{self.tokens:.1f})")
                    return False
            
            # 等待令牌补充
            sleep_time = min(wait_time, 0.5, deadline - time.monotonic())
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                return False
    
    def _refill(self):
        """补充令牌"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        new_tokens = elapsed / self.refill_interval
        
        if new_tokens >= 0.1:  # 至少补充0.1个token
            self.tokens = min(self.max_tokens, self.tokens + new_tokens)
            self.last_refill = now
    
    def get_status(self) -> Dict:
        """获取限速状态"""
        with self.lock:
            self._refill()
            
            return {
                "name": self.name,
                "tokens_remaining": round(self.tokens, 1),
                "max_tokens": self.max_tokens,
                "usage_pct": round((1 - self.tokens / self.max_tokens) * 100, 1),
                "total_calls": self.total_calls,
                "total_wait_time_sec": round(self.total_wait_time, 2),
                "refill_interval_sec": self.refill_interval,
                "last_reset": self.last_reset.strftime("%Y-%m-%d %H:%M:%S"),
            }
    
    def reset_stats(self):
        """重置统计信息"""
        with self.lock:
            self.total_calls = 0
            self.total_wait_time = 0.0
            self.last_reset = datetime.now()


class GlobalRateLimiter:
    """全局限速管理器"""
    
    def __init__(self):
        """初始化所有限速器"""
        # TwelveData: 8 credits/min = 每7.5秒1个credit
        self.twelvedata = RateLimiter(
            max_tokens=8,
            refill_interval=7.5,
            name="TwelveData"
        )
        
        # Finnhub: 60 calls/min = 每1秒1个call
        self.finnhub = RateLimiter(
            max_tokens=60,
            refill_interval=1.0,
            name="Finnhub"
        )
        
        # Alpha Vantage: 5 calls/min = 每12秒1个call
        self.alphavantage = RateLimiter(
            max_tokens=5,
            refill_interval=12.0,
            name="AlphaVantage"
        )
        
        logger.info("[全局限速器] 初始化完成")
    
    def get_all_status(self) -> Dict[str, Dict]:
        """获取所有限速器状态"""
        return {
            "twelvedata": self.twelvedata.get_status(),
            "finnhub": self.finnhub.get_status(),
            "alphavantage": self.alphavantage.get_status(),
        }
    
    def check_and_alert(self) -> List[str]:
        """
        检查并返回告警信息
        
        Returns:
            List[str]: 告警消息列表
        """
        alerts = []
        status = self.get_all_status()
        
        for source, info in status.items():
            usage = info["usage_pct"]
            remaining = info["tokens_remaining"]
            
            if usage > 90:
                alerts.append(f"🔴 {info['name']} 限速严重: 使用率{usage:.0f}%, 剩余{remaining} tokens")
            elif usage > 75:
                alerts.append(f"🟡 {info['name']} 限速警告: 使用率{usage:.0f}%, 剩余{remaining} tokens")
            elif usage > 50:
                alerts.append(f"🟢 {info['name']} 限速正常: 使用率{usage:.0f}%")
        
        return alerts
    
    def print_status(self):
        """打印所有限速器状态"""
        print("\n" + "="*60)
        print("全局限速器状态")
        print("="*60)
        
        status = self.get_all_status()
        for source, info in status.items():
            print(f"\n{info['name']}:")
            print(f"  剩余令牌: {info['tokens_remaining']}/{info['max_tokens']}")
            print(f"  使用率: {info['usage_pct']}%")
            print(f"  总调用: {info['total_calls']} 次")
            print(f"  总等待: {info['total_wait_time_sec']} 秒")
            print(f"  补充间隔: {info['refill_interval_sec']} 秒/token")
    
    def reset_all_stats(self):
        """重置所有统计信息"""
        self.twelvedata.reset_stats()
        self.finnhub.reset_stats()
        self.alphavantage.reset_stats()
        logger.info("[全局限速器] 统计信息已重置")


# 全局单例
_global_limiter: Optional[GlobalRateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> GlobalRateLimiter:
    """获取全局限速器单例"""
    global _global_limiter
    
    if _global_limiter is None:
        with _limiter_lock:
            if _global_limiter is None:
                _global_limiter = GlobalRateLimiter()
    
    return _global_limiter


def reset_rate_limiter():
    """重置全局限速器 (用于测试)"""
    global _global_limiter
    with _limiter_lock:
        _global_limiter = None
