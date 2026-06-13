"""
信号去重器

信号去重、过滤、降噪
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.models import SignalScore, Direction
from app.core.events import EventStore

logger = logging.getLogger(__name__)


class SignalDeduplicator:
    """
    信号去重器
    
    功能:
    - 同方向信号冷却期内不重复
    - 信号过期自动失效
    - 信号冲突检测
    - 信号归因记录
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        self.config = config
        self.event_store = event_store
        
        # 冷却时间 (秒)
        self.cooldown_seconds = config.get("SIGNAL_COOLDOWN", 60)
        
        # 信号缓存
        self.recent_signals: Dict[str, List[SignalScore]] = {}  # symbol -> signals
        self.expired_signals: List[str] = []
    
    def deduplicate(self, signals: List[SignalScore]) -> List[SignalScore]:
        """
        信号去重
        
        Args:
            signals: 原始信号列表
        
        Returns:
            去重后的信号列表
        """
        filtered_signals = []
        
        for signal in signals:
            # 1. 检查是否过期
            if signal.is_expired():
                self._record_rejection(signal, "signal_expired")
                continue
            
            # 2. 检查冷却期
            if self._in_cooldown(signal):
                self._record_rejection(signal, "signal_in_cooldown")
                continue
            
            # 3. 检查信号冲突
            if self._has_conflict(signal):
                self._record_rejection(signal, "signal_conflict")
                continue
            
            # 4. 通过所有检查
            filtered_signals.append(signal)
            self._add_to_cache(signal)
        
        # 清理过期信号
        self._cleanup_expired()
        
        return filtered_signals
    
    def _in_cooldown(self, signal: SignalScore) -> bool:
        """检查是否在冷却期内"""
        symbol = signal.symbol
        direction = signal.direction
        
        if symbol not in self.recent_signals:
            return False
        
        now = datetime.now()
        cooldown_until = now - timedelta(seconds=self.cooldown_seconds)
        
        # 检查同方向信号
        for recent_signal in self.recent_signals[symbol]:
            if (recent_signal.direction == direction and
                recent_signal.created_at > cooldown_until):
                return True
        
        return False
    
    def _has_conflict(self, signal: SignalScore) -> bool:
        """检查是否有冲突信号"""
        symbol = signal.symbol
        opposite_direction = Direction.SHORT if signal.direction == Direction.LONG else Direction.LONG
        
        if symbol not in self.recent_signals:
            return False
        
        now = datetime.now()
        conflict_window = now - timedelta(seconds=self.cooldown_seconds * 2)
        
        # 检查反方向信号 (更长的冲突窗口)
        for recent_signal in self.recent_signals[symbol]:
            if (recent_signal.direction == opposite_direction and
                recent_signal.created_at > conflict_window and
                recent_signal.strength > 70):  # 强信号才视为冲突
                return True
        
        return False
    
    def _add_to_cache(self, signal: SignalScore):
        """添加信号到缓存"""
        symbol = signal.symbol
        
        if symbol not in self.recent_signals:
            self.recent_signals[symbol] = []
        
        self.recent_signals[symbol].append(signal)
        
        # 限制缓存大小
        if len(self.recent_signals[symbol]) > 50:
            self.recent_signals[symbol] = self.recent_signals[symbol][-50:]
    
    def _cleanup_expired(self):
        """清理过期信号"""
        now = datetime.now()
        expiry_threshold = now - timedelta(minutes=30)
        
        for symbol in list(self.recent_signals.keys()):
            self.recent_signals[symbol] = [
                s for s in self.recent_signals[symbol]
                if s.created_at > expiry_threshold
            ]
            
            if not self.recent_signals[symbol]:
                del self.recent_signals[symbol]
    
    def _record_rejection(self, signal: SignalScore, reason: str):
        """记录信号拒绝"""
        self.event_store.append(
            TradeEvent(
                event_type="signal_rejected",
                signal_id=signal.signal_id,
                data={
                    "symbol": signal.symbol,
                    "strategy_id": signal.strategy_id,
                    "reason": reason,
                    "strength": signal.strength,
                },
            )
        )
        logger.debug(f"信号拒绝：{signal.symbol} reason={reason}")
    
    def get_signal_stats(self, symbol: str) -> Dict[str, Any]:
        """获取信号统计"""
        if symbol not in self.recent_signals:
            return {"total": 0, "long": 0, "short": 0}
        
        signals = self.recent_signals[symbol]
        return {
            "total": len(signals),
            "long": sum(1 for s in signals if s.direction == Direction.LONG),
            "short": sum(1 for s in signals if s.direction == Direction.SHORT),
            "avg_strength": sum(s.strength for s in signals) / len(signals) if signals else 0,
            "last_signal_time": signals[-1].created_at if signals else None,
        }
