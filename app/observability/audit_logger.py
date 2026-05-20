"""审计日志记录器"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path


class AuditLogger:
    """审计日志记录器"""
    
    def __init__(
        self,
        log_file: str = "logs/audit.log",
        retention_days: int = 90,
    ):
        self.log_file = Path(log_file)
        self.retention_days = retention_days
        self.events: List[Dict[str, Any]] = []
        
        # 确保目录存在
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 配置日志
        self.logger = logging.getLogger("audit")
        self.logger.setLevel(logging.INFO)
        
        handler = logging.FileHandler(self.log_file)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(message)s'
        ))
        self.logger.addHandler(handler)
    
    def record_event(
        self,
        event_type: str,
        user_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        """记录审计事件"""
        event = {
            "event_id": f"audit_{datetime.now().timestamp()}",
            "event_type": event_type,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "details": details or {},
            **kwargs
        }
        
        self.events.append(event)
        
        # 写入日志文件
        self.logger.info(json.dumps(event, ensure_ascii=False))
        
        # 清理过期记录
        self._cleanup_expired()
    
    def query_events(
        self,
        event_type: Optional[str] = None,
        user_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询审计事件"""
        results = self.events
        
        if event_type:
            results = [e for e in results if e["event_type"] == event_type]
        
        if user_id:
            results = [e for e in results if e.get("user_id") == user_id]
        
        if start_time:
            results = [
                e for e in results
                if e["timestamp"] >= start_time
            ]
        
        if end_time:
            results = [
                e for e in results
                if e["timestamp"] <= end_time
            ]
        
        return results
    
    def _cleanup_expired(self):
        """清理过期记录"""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        self.events = [
            e for e in self.events
            if datetime.fromisoformat(e["timestamp"]) > cutoff
        ]
