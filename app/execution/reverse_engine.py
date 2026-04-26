"""
反手决策引擎 (Reverse Decision Engine)

P0 核心模块：判断何时允许反手。
从简单的 True/False 升级为分级决策 (NO_ACTION, CLOSE_ONLY, REVERSE_NOW, DELAY_AND_CONFIRM)。
"""
import logging
import time
import threading
from enum import Enum
from typing import Optional, Dict, Any, Tuple
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.execution.reverse_engine")

class ReverseAction(Enum):
    NO_ACTION = 0
    CLOSE_ONLY = 1          # 仅平仓，不开新仓
    REVERSE_NOW = 2         # 立即平仓反手
    DELAY_AND_CONFIRM = 3   # 延迟观察

class ReverseDecisionEngine:
    def __init__(self, 
                 cooldown_minutes: int = 15, 
                 strength_diff_threshold: int = 15,
                 max_reverses_per_day: int = 5):
        self.cooldown_minutes = cooldown_minutes
        self.strength_diff_threshold = strength_diff_threshold
        self.max_reverses_per_day = max_reverses_per_day
        
        # 内存缓存冷却时间：{code: timestamp}
        self._cooldown_cache: Dict[str, float] = {}
        self._lock = threading.Lock()

    def evaluate(self, signal_code: str, new_signal_strength: int, 
                 current_position_conviction: int = 50, 
                 spread: float = 0.0) -> ReverseAction:
        """
        评估是否应该反手。
        """
        # 1. 冷却检查 (防止短时间反复打脸)
        if self._is_cooling(signal_code):
            return ReverseAction.NO_ACTION
        
        # 2. 信心差值检查 (新信号必须显著强于旧仓位逻辑)
        strength_diff = new_signal_strength - current_position_conviction
        if strength_diff < self.strength_diff_threshold:
            # 信号不够强，仅建议平仓，不建议立即反手
            return ReverseAction.CLOSE_ONLY
        
        # 3. 滑点检查 (流动性不足时禁止反手)
        if spread > 0.05: # 假设滑点超过 5 分钱 (港股/美股不同，需动态)
            return ReverseAction.DELAY_AND_CONFIRM
            
        # 4. 默认允许反手
        self._set_cooling(signal_code)
        return ReverseAction.REVERSE_NOW

    def _is_cooling(self, code: str) -> bool:
        with self._lock:
            last_time = self._cooldown_cache.get(code, 0)
            if time.time() - last_time < self.cooldown_minutes * 60:
                logger.debug(f"[反手冷却] {code} 处于冷却期，跳过反手")
                return True
        return False

    def _set_cooling(self, code: str):
        with self._lock:
            self._cooldown_cache[code] = time.time()
