#!/usr/bin/env python3
"""
时段状态管理器

管理四时段状态切换:
- OVERNIGHT_LEARNING (21:00-06:00)
- PRE_MARKET_PLANNING (08:30-09:30)
- INTRADAY_ACTIVE (10:00-16:00)
- POST_MARKET_REVIEW (16:00-18:00)

设计理念:
- 统一管理时段切换逻辑
- 提供标准化输出接口
- 支持状态查询和切换记录
- 为后续自动化提供基础
"""

import logging
from datetime import datetime, time
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger("ftseek.session_manager")


class SessionState(str, Enum):
    """时段状态枚举"""
    IDLE = "IDLE"
    OVERNIGHT_LEARNING = "OVERNIGHT_LEARNING"
    PRE_MARKET_PLANNING = "PRE_MARKET_PLANNING"
    INTRADAY_ACTIVE = "INTRADAY_ACTIVE"
    POST_MARKET_REVIEW = "POST_MARKET_REVIEW"


class SessionManager:
    """
    时段状态管理器
    
    职责:
    - 根据时间判断当前时段
    - 管理时段切换
    - 提供统一输出格式
    - 记录状态切换历史
    """
    
    # 时段定义 (港股交易日)
    # 2026-04-13 优化: 扩展盘中时段覆盖 09:30-16:00，消陥09:30-10:00的空白间隙
    SESSION_TIMES = {
        SessionState.OVERNIGHT_LEARNING: (time(21, 0), time(6, 0)),  # 跨天
        SessionState.PRE_MARKET_PLANNING: (time(8, 30), time(9, 30)),
        SessionState.INTRADAY_ACTIVE: (time(9, 30), time(16, 0)),  # 优化: 10:00→09:30，覆盖开盘执行期
        SessionState.POST_MARKET_REVIEW: (time(16, 0), time(18, 0)),
    }
    
    def __init__(self):
        self._current_state = SessionState.IDLE
        self._last_transition: Optional[datetime] = None
        self._session_outputs: Dict[str, Dict] = {}  # 存储各时段输出
        self._transition_history: list = []
        
        logger.info("时段状态管理器已初始化")
    
    def get_current_session(self) -> SessionState:
        """获取当前时段"""
        now = datetime.now()
        current_time = now.time()
        
        # 判断交易日
        if now.weekday() >= 5:  # 周末
            return SessionState.IDLE
        
        # 判断时段
        for state, (start, end) in self.SESSION_TIMES.items():
            if start < end:
                if start <= current_time < end:
                    return state
            else:  # 跨天 (overnight)
                if current_time >= start or current_time < end:
                    return state
        
        return SessionState.IDLE
    
    def transition_to(self, new_state: SessionState) -> bool:
        """
        状态切换
        
        Args:
            new_state: 新状态
        
        Returns:
            是否成功切换
        """
        if new_state == self._current_state:
            return False
        
        old_state = self._current_state
        self._current_state = new_state
        self._last_transition = datetime.now()
        
        # 记录切换历史
        self._transition_history.append({
            "from": old_state.value,
            "to": new_state.value,
            "timestamp": self._last_transition.isoformat()
        })
        
        # 保持历史最多100条
        if len(self._transition_history) > 100:
            self._transition_history = self._transition_history[-100:]
        
        logger.info(f"[时段切换] {old_state.value} -> {new_state.value}")
        return True
    
    def save_session_output(self, state: SessionState, output: Dict):
        """
        保存时段输出
        
        Args:
            state: 时段状态
            output: 输出数据 (JSON格式)
        """
        date = datetime.now().strftime("%Y-%m-%d")
        key = f"{date}_{state.value}"
        self._session_outputs[key] = output
        
        logger.info(f"[时段输出] {state.value} 已保存")
    
    def get_session_output(self, state: SessionState, date: str = None) -> Optional[Dict]:
        """
        获取时段输出
        
        Args:
            state: 时段状态
            date: 日期 (默认今天)
        
        Returns:
            输出数据
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        key = f"{date}_{state.value}"
        return self._session_outputs.get(key)
    
    def get_transition_history(self, limit: int = 10) -> list:
        """获取切换历史"""
        return self._transition_history[-limit:]
    
    @property
    def current_state(self) -> SessionState:
        """当前状态"""
        return self._current_state
    
    def get_session_name_cn(self, state: SessionState = None) -> str:
        """获取时段中文名"""
        if state is None:
            state = self._current_state
        
        names = {
            SessionState.IDLE: "空闲",
            SessionState.OVERNIGHT_LEARNING: "夜间学习",
            SessionState.PRE_MARKET_PLANNING: "盘前计划",
            SessionState.INTRADAY_ACTIVE: "盘中活跃",
            SessionState.POST_MARKET_REVIEW: "盘后复盘"
        }
        
        return names.get(state, "未知")
    
    def is_trading_hours(self) -> bool:
        """是否在交易时段"""
        return self._current_state in [
            SessionState.PRE_MARKET_PLANNING,
            SessionState.INTRADAY_ACTIVE,
            SessionState.POST_MARKET_REVIEW
        ]
    
    def get_next_session(self) -> Optional[SessionState]:
        """获取下一个时段"""
        now = datetime.now().time()
        
        sessions = [
            (time(8, 30), SessionState.PRE_MARKET_PLANNING),
            (time(10, 0), SessionState.INTRADAY_ACTIVE),
            (time(16, 0), SessionState.POST_MARKET_REVIEW),
            (time(21, 0), SessionState.OVERNIGHT_LEARNING),
        ]
        
        for session_time, session_state in sessions:
            if now < session_time:
                return session_state
        
        return SessionState.OVERNIGHT_LEARNING  # 跨天到第二天


# 全局单例
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """获取时段管理器单例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


def reset_session_manager():
    """重置时段管理器 (用于测试)"""
    global _session_manager
    _session_manager = SessionManager()
