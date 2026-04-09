"""数据流追踪器"""
import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger("ftseek.monitoring.dataflow")

class DataFlowTracker:
    """数据流追踪器"""
    
    def __init__(self):
        self.counters: Dict[str, int] = {}
    
    def track(self, stage: str, data: Dict[str, Any]):
        """追踪数据流"""
        if stage not in self.counters:
            self.counters[stage] = 0
        self.counters[stage] += 1
        
        logger.info(f"[DataFlow] {stage} #{self.counters[stage]}: {datetime.now().isoformat()}")
    
    def get_stats(self) -> Dict[str, int]:
        """获取统计"""
        return self.counters.copy()

tracker = DataFlowTracker()
