"""
信号管理器
存储待确认信号，供 FastAPI 回调服务查询和确认
内置信号冷却：同策略 + 同标的 + 同方向 在冷却期内不重复触发
"""
import time
import threading
import logging
from typing import Dict, Optional, List

from app import config
from app.strategy.base import Signal

logger = logging.getLogger("ftseek.signal_store")


class SignalStore:
    """线程安全的信号存储"""

    def __init__(self):
        self._signals: Dict[str, Signal] = {}
        self._lock = threading.Lock()
        self._history: List[Signal] = []  # 已处理的信号历史
        # 新增：冷却记录表 { "strategy:symbol:action": last_timestamp }
        self._cooldown_map: Dict[str, float] = {} 

    def _get_cooldown_key(self, signal: Signal) -> str:
        """生成冷却键值：策略 ID + 股票代码 + 动作"""
        # 兼容处理：如果信号对象没有 strategy_id，使用默认值
        strategy_id = getattr(signal, 'strategy_id', 'default_strategy')
        return f"{strategy_id}:{signal.code}:{signal.action.value}"

    def _check_cooldown(self, signal: Signal) -> bool:
        """
        准确性优先：检查是否处于冷却期
        返回 True 表示可以通过，False 表示被拦截
        """
        key = self._get_cooldown_key(signal)
        now = time.time()
        
        with self._lock:
            last_time = self._cooldown_map.get(key)
            if last_time and (now - last_time) < config.SIGNAL_COOLDOWN:
                remaining = int(config.SIGNAL_COOLDOWN - (now - last_time))
                logger.info(f"🚫 信号冷却拦截：{key} | 剩余 {remaining}s")
                return False
            
            # 更新冷却时间
            self._cooldown_map[key] = now
            return True

    def add(self, signal: Signal) -> bool:
        """
        添加信号，自动设置过期时间
        返回：True 表示成功入库，False 表示被冷却拦截
        """
        # 1. 先检查冷却 (准确性优先的核心)
        if not self._check_cooldown(signal):
            return False
        
        # 2. 设置过期时间
        if signal.expire_at <= 0:
            signal.expire_at = time.time() + config.SIGNAL_EXPIRE_SECONDS
            
        # 3. 入库
        with self._lock:
            self._signals[signal.id] = signal
            logger.info(f"✅ 信号入库：{signal.id} -> {signal.code} {signal.action.value}")
        
        return True

    def get(self, signal_id: str) -> Optional[Signal]:
        with self._lock:
            return self._signals.get(signal_id)

    def confirm(self, signal_id: str) -> Optional[Signal]:
        """确认信号，返回信号对象。已确认的信号不可重复执行。"""
        with self._lock:
            signal = self._signals.get(signal_id)
            if not signal:
                logger.warning(f"信号不存在：{signal_id}")
                return None
            if signal.is_expired:
                logger.warning(f"信号已过期：{signal_id}")
                del self._signals[signal_id]
                return None
            if signal.confirmed:
                logger.warning(f"信号已被确认过 (防重放): {signal_id}")
                return None
            if signal.executed:
                logger.warning(f"信号已执行过 (防重放): {signal_id}")
                return None
            
            signal.confirmed = True
            logger.info(f"🔔 信号已确认：{signal_id} -> {signal.code} {signal.action.value}")
            return signal

    def reject(self, signal_id: str) -> bool:
        """拒绝/忽略信号"""
        with self._lock:
            signal = self._signals.pop(signal_id, None)
            if signal:
                self._history.append(signal)
                logger.info(f"❌ 信号已拒绝：{signal_id}")
                return True
            return False

    def mark_executed(self, signal_id: str):
        """标记信号已执行"""
        with self._lock:
            signal = self._signals.pop(signal_id, None)
            if signal:
                signal.executed = True
                self._history.append(signal)

    def cleanup_expired(self):
        """清理过期信号"""
        with self._lock:
            expired = [
                sid for sid, s in self._signals.items() if s.is_expired
            ]
            for sid in expired:
                s = self._signals.pop(sid)
                self._history.append(s)
            if expired:
                logger.info(f"🧹 清理过期信号 {len(expired)} 条")

    def get_pending(self) -> List[Signal]:
        """获取所有待处理信号"""
        with self._lock:
            return [
                s for s in self._signals.values()
                if not s.confirmed and not s.is_expired
            ]

    def get_history(self, limit: int = 50) -> List[Signal]:
        """获取历史信号"""
        return self._history[-limit:]


# 全局单例
signal_store = SignalStore()
