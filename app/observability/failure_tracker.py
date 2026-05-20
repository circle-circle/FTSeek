# app/observability/failure_tracker.py
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Union
from app.core.models import FailureType

class FailureTracker:
    """失败追踪器"""
    
    def __init__(self, retention_days: int = 30):
        self.retention = timedelta(days=retention_days)
        self.failures: List[Dict[str, Any]] = []
    
    def record_failure(
        self,
        failure_type: Union[FailureType, str],
        symbol: str,
        reason: str,
        **kwargs
    ):
        """记录失败"""
        # 支持 FailureType 枚举和字符串
        if isinstance(failure_type, FailureType):
            ftype_str = failure_type.value
        else:
            ftype_str = str(failure_type)
        
        self.failures.append({
            "failure_id": f"fail_{datetime.now().timestamp()}",
            "failure_type": ftype_str,
            "symbol": symbol,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
        
        # 清理过期记录
        self._cleanup_expired()
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            "total": len(self.failures),
            "by_type": {},
            "by_symbol": {},
            "recent_24h": 0,
        }
        
        now = datetime.now()
        cutoff = now - timedelta(hours=24)
        
        for failure in self.failures:
            # 按类型统计
            ftype = failure["failure_type"]
            stats["by_type"][ftype] = stats["by_type"].get(ftype, 0) + 1
            
            # 按股票统计
            symbol = failure["symbol"]
            stats["by_symbol"][symbol] = stats["by_symbol"].get(symbol, 0) + 1
            
            # 24 小时内
            ts = datetime.fromisoformat(failure["timestamp"])
            if ts > cutoff:
                stats["recent_24h"] += 1
        
        return stats
    
    def get_recent_failures(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取最近的失败记录"""
        return self.failures[-limit:]
    
    def get_statistics_by_type(self) -> Dict[str, int]:
        """按类型获取统计"""
        stats = {}
        for failure in self.failures:
            ftype = failure["failure_type"]
            stats[ftype] = stats.get(ftype, 0) + 1
        return stats
    
    def get_statistics_by_symbol(self) -> Dict[str, int]:
        """按股票获取统计"""
        stats = {}
        for failure in self.failures:
            symbol = failure["symbol"]
            stats[symbol] = stats.get(symbol, 0) + 1
        return stats
    
    def _cleanup_expired(self):
        """清理过期记录"""
        cutoff = datetime.now() - self.retention
        self.failures = [
            f for f in self.failures
            if datetime.fromisoformat(f["timestamp"]) > cutoff
        ]
