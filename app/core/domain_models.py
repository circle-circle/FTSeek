"""
统一领域模型 (Unified Domain Models)

目标: 为 FTSeek-US 提供单一真相源 (SSOT) 核心 dataclass，
消除同一概念在多个模块中各自定义导致的架构碎片化。

当前收敛的模型:
- ExitDecision: 退出决策 (替代 app/risk/exit_rule_dispatcher.py / app/risk/exit_rules/base.py
  / app/exit/base.py / app/strategy/exit_policies.py 中 4 处不兼容定义)
- OrderState: 订单生命周期状态 (替代 app/core/models.py / app/risk/order_tracker.py 中 2 处)
- MarketRegime: 市场状态 (替代 app/core/models.py / app/utils/dragonhead_pool.py /
  app/strategy/base.py / app/strategy/regime_filter.py 中 4 处不同枚举)
- GateResult: 风控/因子门禁结果 (替代 9 处同名不同字段的 class)
- SizingResult: 仓位 sizing 结果 (替代 risk/position_sizer.py / exit/position_sizing.py)
- CostEstimate: 交易成本估算 (替代 decision/cost_model.py / exit/cost_model.py)

迁移策略 (渐进):
1. 新代码从此模块 import。
2. 旧类保留为兼容 shim (type alias 或 wrapper)，在各自文件中标注 DEPRECATED_DOMAIN_MODEL，
   后续随重构逐步下线。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────
# 订单状态
# ─────────────────────────────────────────────────────────────
class OrderState(str, Enum):
    """订单生命周期统一状态机.

    合并来源:
    - app/core/models.py: CREATED/VALIDATED/SENT/ACKED/PARTIAL_FILLED/FILLED/CANCELLED/REJECTED/EXPIRED
    - app/risk/order_tracker.py: SUBMITTED/PENDING_CONFIRM/CONFIRMED/STALE/CANCEL_PENDING
    """
    CREATED = "created"           # 本地已创建
    VALIDATED = "validated"       # 通过风控校验
    SUBMITTED = "submitted"       # 已提交到 broker (等同于 SENT)
    ACKED = "acked"               # broker 已确认
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"               # 超时未确认


# ─────────────────────────────────────────────────────────────
# 市场状态
# ─────────────────────────────────────────────────────────────
class MarketRegime(str, Enum):
    """市场状态统一枚举.

    合并来源:
    - app/core/models.py: TREND_UP / TREND_DOWN / RANGE / HIGH_VOL
    - app/strategy/regime_filter.py: STRONG_BULL / BULL / CAUTION / BEAR / CRISIS
    - app/strategy/base.py / risk/market_regime_detector.py 的多种字符串输出

    映射关系 (向后兼容):
    - STRONG_BULL ≈ TREND_UP + 高确信
    - BULL      ≈ TREND_UP
    - CAUTION   ≈ RANGE (偏多)
    - BEAR      ≈ TREND_DOWN
    - CRISIS    ≈ HIGH_VOL / EXTREME
    """
    STRONG_BULL = "strong_bull"
    BULL = "bull"                 # = trend_up
    CAUTION = "caution"           # = range (pre-bull/bear)
    BEAR = "bear"                 # = trend_down
    CRISIS = "crisis"             # = high_vol / extreme
    RANGE = "range"               # 无明确趋势
    UNKNOWN = "unknown"


# ─────────────────────────────────────────────────────────────
# 退出决策
# ─────────────────────────────────────────────────────────────
@dataclass
class ExitDecision:
    """退出决策统一模型.

    字段语义:
    - action: "NO_ACTION" | "REDUCE" | "CLOSE"
    - qty_pct: 减仓/平仓比例 (1.0 = 全部)
    - rule_name: 触发规则名 (e.g. "flash_exit", "trailing_stop")
    - reason: 人类可读原因
    - priority: 规则优先级 (数字越小越优先)
    - exit_price: 建议退出价格 (可选)
    - metadata: 额外上下文
    """
    action: str = "NO_ACTION"      # NO_ACTION | REDUCE | CLOSE
    qty_pct: float = 0.0
    rule_name: str = ""
    reason: str = ""
    priority: int = 0
    exit_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def should_exit(self) -> bool:
        return self.action in ("REDUCE", "CLOSE") and self.qty_pct > 0


# ─────────────────────────────────────────────────────────────
# 风控/因子门禁结果
# ─────────────────────────────────────────────────────────────
@dataclass
class GateResult:
    """门禁结果统一模型.

    合并来源: factors/* / risk/options_quality_gate.py / debate/statistical_gate.py
    字段取各实现的交集 + metadata 保留扩展性.
    """
    passed: bool = False
    score: float = 0.0
    weight: float = 1.0
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.metadata.get("name", "")


# ─────────────────────────────────────────────────────────────
# Sizing 结果
# ─────────────────────────────────────────────────────────────
@dataclass
class SizingResult:
    """仓位 sizing 统一模型.

    字段合并:
    - risk/position_sizer.py: shares, risk_amount, position_value, tier, water_level
    - exit/position_sizing.py: shares, position_value, risk_dollars, cost_estimate, edge_estimate
    """
    shares: int = 0
    position_value: float = 0.0
    risk_amount: float = 0.0
    cost_estimate: float = 0.0
    edge_estimate: float = 0.0
    tier: str = ""
    water_level: str = ""
    should_trade: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────
# 交易成本
# ─────────────────────────────────────────────────────────────
@dataclass
class CostEstimate:
    """交易成本估算统一模型.

    合并 decision/cost_model.py 与 exit/cost_model.py.
    """
    commission: float = 0.0
    slippage: float = 0.0
    spread: float = 0.0
    total_per_side: float = 0.0
    round_trip: float = 0.0
    option_multiplier: float = 1.0
    pre_after_hours_multiplier: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CostBreakdown:
    """decision/cost_model.py 返回的完整 breakdown (向后兼容)."""
    commission: float = 0.0
    slippage: float = 0.0
    sec_fee: float = 0.0
    total: float = 0.0
    per_share: float = 0.0
    notes: str = ""


# ─────────────────────────────────────────────────────────────
# 持仓来源 (与 positions/source.py 对齐)
# ─────────────────────────────────────────────────────────────
class PositionSource(str, Enum):
    """持仓来源统一枚举 (从 positions/source.py 迁移至此，避免循环 import)."""
    AUTO_MAIN = "auto_main"
    AUTO_ORB = "auto_orb"
    AUTO_FAST_LANE = "auto_fast_lane"
    AUTO_LLM_PLAN = "auto_llm_plan"
    MANUAL = "manual"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


# ─────────────────────────────────────────────────────────────
# 向后兼容映射
# ─────────────────────────────────────────────────────────────
_MARKET_REGIME_LEGACY_MAP: Dict[str, MarketRegime] = {
    "trend_up": MarketRegime.BULL,
    "trending_up": MarketRegime.BULL,
    "trend_down": MarketRegime.BEAR,
    "trending_down": MarketRegime.BEAR,
    "range_bound": MarketRegime.RANGE,
    "range": MarketRegime.RANGE,
    "ranging": MarketRegime.RANGE,
    "high_vol": MarketRegime.CRISIS,
    "high_vol_chop": MarketRegime.CRISIS,
    "extreme": MarketRegime.CRISIS,
    "risk_on": MarketRegime.BULL,
    "risk_off": MarketRegime.BEAR,
    "rotation": MarketRegime.CAUTION,
    "recovery": MarketRegime.BULL,
}


def market_regime_from_legacy(label: Any) -> MarketRegime:
    """从历史字符串/枚举值安全解析为统一 MarketRegime."""
    if isinstance(label, MarketRegime):
        return label
    s = str(label or "").lower().strip()
    return _MARKET_REGIME_LEGACY_MAP.get(s, MarketRegime.UNKNOWN)
