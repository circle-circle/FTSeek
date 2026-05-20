"""性能监控器"""
import logging
import time
from typing import Dict, Optional
from contextlib import contextmanager

logger = logging.getLogger("ftseek_us.performance")

class PerformanceMonitor:
    """性能监控器"""
    
    def __init__(self):
        self.timings: Dict[str, list] = {}
    
    @contextmanager
    def measure(self, operation: str):
        """测量操作耗时"""
        start = time.time()
        try:
            yield
        finally:
            elapsed = time.time() - start
            
            if operation not in self.timings:
                self.timings[operation] = []
            self.timings[operation].append(elapsed)
            
            # 只保留最近 100 次
            if len(self.timings[operation]) > 100:
                self.timings[operation] = self.timings[operation][-100:]
            
            # 记录慢操作
            if elapsed > 1.0:
                logger.warning(f"[Performance] {operation} 耗时 {elapsed:.3f}s (慢)")
            else:
                logger.debug(f"[Performance] {operation} 耗时 {elapsed:.3f}s")
    
    def get_stats(self, operation: str) -> Optional[Dict[str, float]]:
        """获取统计"""
        if operation not in self.timings or not self.timings[operation]:
            return None
        
        timings = self.timings[operation]
        return {
            "count": len(timings),
            "avg": sum(timings) / len(timings),
            "min": min(timings),
            "max": max(timings),
            "latest": timings[-1],
        }

# 全局实例
monitor = PerformanceMonitor()
