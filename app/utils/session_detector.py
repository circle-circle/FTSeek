#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时段检测器 (Session Detector) - Phase 2

用于判断当前时间处于哪个市场阶段，并提供分时段风控参数。
支持多时段配置，默认回退到 STANDARD 模式。
"""

from datetime import datetime, time
from typing import Dict, Optional, Tuple

class MarketSession:
    """市场时段定义"""
    def __init__(self, name: str, start: Optional[time], end: Optional[time], config: Dict):
        self.name = name
        self.start = start
        self.end = end
        self.config = config

    def is_active(self, current_time: time) -> bool:
        """检查当前时间是否在该时段内"""
        if self.start is None and self.end is None:
            return True
        
        if self.start and self.end:
            if self.start <= self.end:
                return self.start <= current_time <= self.end
            else:
                # 跨午夜 (例如 23:00 -> 02:00)
                return current_time >= self.start or current_time <= self.end
        return False

class SessionDetector:
    """
    时段检测器
    """
    def __init__(self, time_aware_config: Dict):
        """
        初始化时段检测器
        :param time_aware_config: 配置字典，例如 { 'opening': { 'start': '09:30', ... } }
        """
        self.sessions = []
        
        # 默认时段：标准模式 (兜底)
        default_config = time_aware_config.get('afternoon', {
            'min_confidence': 0.35, 
            'max_position': 0.18
        })
        self.default_session = MarketSession("STANDARD", None, None, default_config)
        
        for name, cfg in time_aware_config.items():
            start_str = cfg.get('start')
            end_str = cfg.get('end')
            
            start_time = time.fromisoformat(start_str) if start_str else None
            end_time = time.fromisoformat(end_str) if end_str else None
            
            self.sessions.append(MarketSession(name, start_time, end_time, cfg))
        
        # 按开始时间排序，确保优先匹配早的时段
        # 但简单排序不够，因为时段可能重叠？通常不重叠。
        # 我们按优先级顺序检查：Opening -> MidDay -> Afternoon -> Closing
        priority_order = ['opening', 'mid_day', 'afternoon', 'closing']
        self.sessions.sort(key=lambda s: priority_order.index(s.name) if s.name in priority_order else 99)

    def get_current_session(self, current_time: time = None) -> MarketSession:
        """
        获取当前时段配置
        :return: MarketSession
        """
        now = current_time or datetime.now().time()
        
        # 排除非交易时间 (简单判断：不在任何定义时段内)
        # 但通常我们只关心配置中的时段。如果当前时间完全不在配置中，返回 STANDARD
        
        for session in self.sessions:
            if session.is_active(now):
                return session
                
        return self.default_session

    def get_current_config(self) -> Dict:
        """直接获取当前时段的配置字典"""
        return self.get_current_session().config
