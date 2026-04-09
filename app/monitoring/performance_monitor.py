"""性能监控器"""
import logging
import time
from typing import Dict, Optional
from contextlib import contextmanager

logger = logging.getLogger("ftseek.monitoring.performance")

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
            if len(self.timings[operation]) > 100:
                self.timings[operation] = self.timings[operation][-100:]
            if elapsed > 1.0:
                logger.warning(f"[Performance] {operation} 耗时 {elapsed:.3f}s (慢)")
    
    def get_stats(self, operation: str) -> Optional[Dict[str, float]]:
        if operation not in self.timings or not self.timings[operation]:
            return None
        timings = self.timings[operation]
        return {"count": len(timings), "avg": sum(timings)/len(timings), "min": min(timings), "max": max(timings)}

monitor = PerformanceMonitor()
