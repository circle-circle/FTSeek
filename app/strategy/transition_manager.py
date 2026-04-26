#!/usr/bin/env python3
"""
过渡管理器 (Transition Manager)

功能:
1. 管理跨市场策略与趋势策略的主导权交接
2. 监控套利窗口状态
3. 平滑过渡 (09:30-10:00 跨市场主导 → 10:00+ 趋势主导)
4. 记录过渡日志和统计数据

架构定位:
  跨市场套利从全天策略体系中剥离,作为开盘时段独立主导模块
  通过过渡管理器在合适时间和条件下,把主导权平滑交还给核心场景策略
"""

import logging
from typing import Dict, Optional
from datetime import datetime, time
from enum import Enum

from app.strategy.cross_market import CrossMarketSessionWindow

logger = logging.getLogger("ftseek.transition_manager")


class TransitionPhase(str, Enum):
    """过渡阶段"""
    PRE_MARKET = "pre_market"              # 09:00-09:30 盘前准备
    ARB_DOMINANT = "arb_dominant"          # 09:30-09:50 跨市场主导
    TRANSITIONING = "transitioning"        # 09:50-10:00 过渡期
    TREND_DOMINANT = "trend_dominant"      # 10:00-16:00 趋势主导
    AFTER_HOURS = "after_hours"            # 收盘后


class TransitionManager:
    """
    过渡管理器
    
    职责:
    1. 判断当前主导策略 (跨市场 vs 趋势)
    2. 管理过渡阶段状态
    3. 提供过渡统计数据
    4. 记录主导权交接日志
    """
    
    def __init__(self):
        # 当前阶段
        self._current_phase: TransitionPhase = TransitionPhase.AFTER_HOURS
        
        # 今日过渡日志
        self._transition_log: list = []
        
        # 统计数据
        self._stats = {
            'arb_signals_count': 0,        # 跨市场信号数
            'arb_executed_count': 0,       # 跨市场执行数
            'arb_rejected_count': 0,       # 跨市场否决数
            'trend_signals_count': 0,      # 趋势信号数
            'trend_delayed_count': 0,      # 趋势延迟数
            'last_transition_time': None,  # 最后过渡时间
        }
        
        logger.info("过渡管理器已初始化")
    
    def update_phase(self, current_time: Optional[datetime] = None) -> TransitionPhase:
        """
        更新当前阶段
        
        Args:
            current_time: 当前时间
        
        Returns:
            当前过渡阶段
        """
        if current_time is None:
            current_time = datetime.now()
        
        session_phase = CrossMarketSessionWindow.get_session_phase(current_time)
        
        # 映射会话阶段到过渡阶段
        if session_phase == 'pre_market':
            new_phase = TransitionPhase.PRE_MARKET
        elif session_phase == 'arb_window':
            # 套利窗口内部分为两个子阶段
            t = current_time.time()
            transition_start = time(9, 50)  # 09:50 开始过渡
            
            if t < transition_start:
                new_phase = TransitionPhase.ARB_DOMINANT
            else:
                new_phase = TransitionPhase.TRANSITIONING
        elif session_phase == 'trend_session':
            new_phase = TransitionPhase.TREND_DOMINANT
        else:
            new_phase = TransitionPhase.AFTER_HOURS
        
        # 阶段变化时记录日志
        if new_phase != self._current_phase:
            old_phase = self._current_phase
            self._current_phase = new_phase
            
            logger.info(
                f"[过渡管理] 阶段变化: {old_phase.value} → {new_phase.value}"
            )
            
            self._transition_log.append({
                'timestamp': current_time.isoformat(),
                'old_phase': old_phase.value,
                'new_phase': new_phase.value,
            })
            
            self._stats['last_transition_time'] = current_time.isoformat()
        
        return self._current_phase
    
    def should_prioritize_arb(self, current_time: Optional[datetime] = None) -> bool:
        """
        是否应该优先跨市场策略
        
        Args:
            current_time: 当前时间
        
        Returns:
            True=跨市场优先, False=趋势策略优先
        """
        phase = self.update_phase(current_time)
        
        return phase in (
            TransitionPhase.PRE_MARKET,
            TransitionPhase.ARB_DOMINANT,
        )
    
    def should_delay_trend(self, current_time: Optional[datetime] = None) -> bool:
        """
        是否应该延迟趋势策略 (给跨市场让路)
        
        Args:
            current_time: 当前时间
        
        Returns:
            True=延迟趋势策略
        """
        phase = self.update_phase(current_time)
        
        # 跨市场主导期,延迟趋势策略 5 分钟
        if phase == TransitionPhase.ARB_DOMINANT:
            return True
        
        return False
    
    def record_arb_signal(self, executed: bool = False, rejected: bool = False):
        """记录跨市场信号"""
        if executed:
            self._stats['arb_executed_count'] += 1
        if rejected:
            self._stats['arb_rejected_count'] += 1
        self._stats['arb_signals_count'] += 1
    
    def record_trend_signal(self, delayed: bool = False):
        """记录趋势信号"""
        self._stats['trend_signals_count'] += 1
        if delayed:
            self._stats['trend_delayed_count'] += 1
    
    def get_stats(self) -> Dict:
        """获取统计数据"""
        return {
            **self._stats,
            'current_phase': self._current_phase.value,
            'is_arb_dominant': self._current_phase == TransitionPhase.ARB_DOMINANT,
            'is_transitioning': self._current_phase == TransitionPhase.TRANSITIONING,
        }
    
    def get_transition_log(self, last_n: int = 20) -> list:
        """获取过渡日志"""
        return self._transition_log[-last_n:]
    
    def reset_daily_stats(self):
        """重置每日统计"""
        self._stats = {
            'arb_signals_count': 0,
            'arb_executed_count': 0,
            'arb_rejected_count': 0,
            'trend_signals_count': 0,
            'trend_delayed_count': 0,
            'last_transition_time': None,
        }
        self._transition_log.clear()
        logger.info("[过渡管理] 每日统计已重置")


# 全局单例
_transition_manager: Optional[TransitionManager] = None


def get_transition_manager() -> TransitionManager:
    """获取过渡管理器单例"""
    global _transition_manager
    if _transition_manager is None:
        _transition_manager = TransitionManager()
    return _transition_manager


def init_transition_manager() -> TransitionManager:
    """初始化过渡管理器"""
    global _transition_manager
    _transition_manager = TransitionManager()
    return _transition_manager
