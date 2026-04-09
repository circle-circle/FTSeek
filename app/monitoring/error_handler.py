"""错误处理和恢复"""
import logging
from datetime import datetime, timedelta
from typing import Dict
from collections import defaultdict

logger = logging.getLogger("ftseek.monitoring.errors")

class ErrorHandler:
    """错误处理器"""
    
    def __init__(self, max_errors: int = 5, reset_minutes: int = 30):
        self.max_errors = max_errors
        self.reset_minutes = reset_minutes
        self.errors: Dict[str, list] = defaultdict(list)
        self.resets: Dict[str, datetime] = {}
    
    def record_error(self, code: str, error: Exception):
        """记录错误"""
        now = datetime.now()
        
        if code in self.resets:
            if now - self.resets[code] > timedelta(minutes=self.reset_minutes):
                self.errors[code] = []
                self.resets[code] = now
        
        self.errors[code].append({"time": now, "error": str(error)})
        logger.error(f"[{code}] 错误 #{len(self.errors[code])}: {error}")
        
        return len(self.errors[code]) >= self.max_errors
    
    def should_reset(self, code: str) -> bool:
        return len(self.errors.get(code, [])) >= self.max_errors
    
    def reset_state(self, code: str):
        self.errors[code] = []
        self.resets[code] = datetime.now()
        logger.info(f"[{code}] 状态已重置")

handler = ErrorHandler()
