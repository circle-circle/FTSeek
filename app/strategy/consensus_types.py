#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分层共识协议 (Layered Consensus Protocol) - Phase 2 架构升级核心契约

设计目标：
1. 区分信号来源 (技术 vs 事件)，解决"技术看涨但突发利空"的冲突。
2. 实现差异化风控，为后续 Source-Aware Risk Check 铺路。
3. 全链路可追溯，支持精细化归因。
"""

import uuid
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional

class SignalSource(str, Enum):
    """
    信号来源枚举
    """
    TECHNICAL = "technical"      # 纯技术指标 (confluence_v2, supertrend 等)
    EVENT = "event"              # 新闻/事件驱动 (sentiment_ai, breaking_news 等)
    HYBRID = "hybrid"            # 技术 + 事件共振 (两者同向且均达标，由共识引擎自动判定)
    CROSS_MARKET = "cross_market" # 跨市场联动套利

@dataclass
class LayeredConsensusResult:
    """
    分层共识结果
    替代原有的单一 ConsensusResult，提供更细粒度的决策依据
    """
    code: str
    action: str # BUY / SELL
    
    # 1. 结构共识 (技术面)
    structural_score: float = 0.0
    structural_votes: int = 0
    structural_strategies: List[str] = field(default_factory=list)
    
    # 2. 事件共识 (消息面)
    event_score: float = 0.0
    event_votes: int = 0
    event_strategies: List[str] = field(default_factory=list)
    
    # 3. 融合结果 (最终决策)
    fusion_score: float = 0.0
    fusion_threshold: float = 0.0
    final_source: SignalSource = SignalSource.TECHNICAL
    
    # 4. 冲突与异常检测
    is_conflict: bool = False # 结构与事件方向是否相反
    conflict_details: str = ""
    
    # 5. 审计字段
    timestamp: float = field(default_factory=time.time)
    trace_id: str = field(default_factory=lambda: f"LAYERED-{uuid.uuid4().hex[:8].upper()}")
