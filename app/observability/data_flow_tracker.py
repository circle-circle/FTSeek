"""数据流追踪器"""
import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger("ftseek_us.dataflow")

class DataFlowTracker:
    """数据流追踪器"""
    
    def __init__(self):
        self.counters: Dict[str, int] = {}
        self.timings: Dict[str, list] = {}
    
    def track(self, stage: str, data: Dict[str, Any] = None):
        """追踪数据流"""
        if stage not in self.counters:
            self.counters[stage] = 0
            self.timings[stage] = []
        
        self.counters[stage] += 1
        timestamp = datetime.now()
        self.timings[stage].append(timestamp)
        
        # 保留最近 100 次
        if len(self.timings[stage]) > 100:
            self.timings[stage] = self.timings[stage][-100:]
        
        logger.info(f"[DataFlow] {stage} #{self.counters[stage]}: {timestamp.isoformat()}")
    
    def get_stats(self, stage: str) -> Dict[str, Any]:
        """获取统计"""
        if stage not in self.counters:
            return {"count": 0}
        
        timings = self.timings.get(stage, [])
        return {
            "count": self.counters[stage],
            "last_time": timings[-1].isoformat() if timings else None,
            "avg_interval": self._calc_avg_interval(timings),
        }
    
    def _calc_avg_interval(self, timings: list) -> float:
        """计算平均间隔 (秒)"""
        if len(timings) < 2:
            return 0.0
        
        intervals = [
            (timings[i] - timings[i-1]).total_seconds()
            for i in range(1, len(timings))
        ]
        return sum(intervals) / len(intervals) if intervals else 0.0

# 全局实例
tracker = DataFlowTracker()
