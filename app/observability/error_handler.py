"""错误处理和恢复"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from collections import defaultdict

logger = logging.getLogger("ftseek_us.errors")

class ErrorHandler:
    """错误处理器"""
    
    def __init__(self, max_errors: int = 5, reset_minutes: int = 30):
        self.max_errors = max_errors
        self.reset_minutes = reset_minutes
        self.errors: Dict[str, List[Dict]] = defaultdict(list)
        self.resets: Dict[str, datetime] = {}
    
    def record_error(self, context: str, error: Exception, data: Dict = None):
        """记录错误"""
        now = datetime.now()
        
        # 检查是否需要重置
        if context in self.resets:
            if now - self.resets[context] > timedelta(minutes=self.reset_minutes):
                self.errors[context] = []
                self.resets[context] = now
        
        # 记录错误
        self.errors[context].append({
            "time": now,
            "error": str(error),
            "type": type(error).__name__,
            "data": data,
        })
        
        logger.error(f"[{context}] 错误 #{len(self.errors[context])}: {error}")
        
        # 检查是否需要暂停
        return len(self.errors[context]) >= self.max_errors
    
    def should_reset(self, context: str) -> bool:
        """检查是否应该重置状态"""
        return len(self.errors.get(context, [])) >= self.max_errors
    
    def reset_state(self, context: str):
        """重置状态"""
        self.errors[context] = []
        self.resets[context] = datetime.now()
        logger.info(f"[{context}] 状态已重置")
    
    def get_error_count(self, context: str) -> int:
        """获取错误计数"""
        return len(self.errors.get(context, []))

# 全局实例
handler = ErrorHandler()
