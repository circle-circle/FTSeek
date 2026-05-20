#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分层共识协议 (Layered Consensus Protocol) - Phase 2 架构升级核心契约

设计目标：
1. 区分信号来源 (技术 vs 事件)，解决"技术看涨但突发利空"的冲突。
2. 实现差异化风控，为后续 Source-Aware Risk Check 铺路。
3. 全链路可追溯，支持精细化归因。

Phase 18: 从 consensus_engine.py 提取 SignalOutcome, Vote, ConsensusResult
"""

import uuid
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any


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


# ============================================================
#  Phase 18: 从 consensus_engine.py 提取的类型
# ============================================================

@dataclass
class SignalOutcome:
    """信号成交结果记录（增强版，支持回溯分析）"""
    code: str
    action: str                    # 'BUY' / 'SELL'
    strategy: str                  # 触发策略
    entry_price: float
    exit_price: float
    pnl_pct: float                 # 盈亏百分比
    holding_periods: int           # 持仓周期数
    market_regime: str             # 市场状态 (trending/ranging/neutral)
    scenario: str                  # 场景标签 (Phase 1 新增)
    adx_value: float               # ADX 值
    mtf_alignment: float           # 多周期对齐度 (0-1)
    sentiment_score: float         # 情绪得分
    consensus_score: float         # 共识得分
    quality_score: float           # 当时的质量分
    max_drawdown: float            # 持仓期间最大回撤
    max_profit: float              # 持仓期间最大浮盈
    timestamp: float = 0.0         # 平仓时间戳


@dataclass
class Vote:
    """一票：策略 + 方向 + 信号详情"""
    strategy: str
    action: str  # SignalAction (BUY/SELL)
    price: float
    reason: str
    signal: Any  # Signal 对象
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ConsensusResult:
    """加权共识结果（frozen - 并发安全，禁止字段重赋值）"""
    code: str
    action: str  # SignalAction
    price: float
    score: float               # 最终加权得分
    threshold: float           # 使用的阈值
    market_regime: str         # "trending" / "ranging" / "neutral"
    scenario: str              # 场景标签 (Phase 1 新增)
    adx_value: Optional[float]
    agree_strategies: Tuple[str, ...]       # 同向策略
    oppose_strategies: Tuple[str, ...]      # 反向策略
    weight_details: Dict[str, float]        # {策略：实际权重}
    reasons: Tuple[str, ...]
    votes: Tuple[Vote, ...]
    confidence: float = 0.0    # 置信度 (0-1, 用于风控检查)
    trace_id: str = ""         # 【新增 2026-04-02】决策链路追踪 ID
    divergence: float = 0.0    # 策略分歧度 (0=一致，1=完全分裂)
    quality_result: Optional[object] = None  # 统一质量评分结果
    source: str = "TECHNICAL"  # Phase 2: 信号来源 (TECHNICAL / EVENT / HYBRID)
    extra: Optional[Dict[str, Any]] = None   # 扩展字段

    def __post_init__(self):
        """frozen dataclass 深度不可变：List→Tuple, Dict→MappingProxyType"""
        from types import MappingProxyType
        if not isinstance(self.agree_strategies, tuple):
            object.__setattr__(self, 'agree_strategies', tuple(self.agree_strategies))
        if not isinstance(self.oppose_strategies, tuple):
            object.__setattr__(self, 'oppose_strategies', tuple(self.oppose_strategies))
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, 'reasons', tuple(self.reasons))
        if not isinstance(self.votes, tuple):
            object.__setattr__(self, 'votes', tuple(self.votes))
        if not isinstance(self.weight_details, MappingProxyType):
            object.__setattr__(self, 'weight_details', MappingProxyType(self.weight_details))
