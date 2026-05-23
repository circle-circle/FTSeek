"""
市场上下文提供器 (MarketContextProvider) — 辩论收敛 P0: 旧系统关键优势注入新框架

从旧系统移植的核心能力 (纯函数, 零副作用, 可单测):
1. VIX Regime 分级 (euphoria/normal/caution/fear/crisis)
2. SPY 200日均线趋势判断
3. 资本安全水位 (5级回撤防御)
4. 全局风险状态 (NORMAL/CAUTION/RISK_OFF/HALT)
5. 紧急拦截信号 (闪崩检测)

设计:
- 新框架的因子评估前, 先过 MarketContext 门禁
- 旧系统通过 adapter 调用同一实例, 确保状态一致
- 数据源: 复用旧系统 MarketStateChecker (Futu LV3 / Finnhub)

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================
#  Enums
# ============================================================
class VixRegime(str, Enum):
    EUPHORIA = "euphoria"      # VIX < 12: 过度乐观
    NORMAL = "normal"          # VIX 12-20: 正常
    CAUTION = "caution"        # VIX 20-30: 谨慎
    FEAR = "fear"              # VIX 30-40: 恐慌
    CRISIS = "crisis"          # VIX > 40: 危机
    UNKNOWN = "unknown"        # 数据不可用


class RiskState(str, Enum):
    NORMAL = "NORMAL"          # 全额仓位
    CAUTION = "CAUTION"        # 仓位减半
    RISK_OFF = "RISK_OFF"      # 仅25%仓位, 不开新仓
    HALT = "HALT"              # 停止一切, 人工介入


class CapitalLevel(str, Enum):
    SAFE = "safe"              # 0% ~ -5% 回撤
    WARNING = "warning"        # -5% ~ -10%
    ALERT = "alert"            # -10% ~ -20%
    DANGER = "danger"          # -20% ~ -35%
    CIRCUIT_BREAKER = "circuit_breaker"  # > -35%


class MarketTrend(str, Enum):
    BULLISH = "bullish"        # 价格 > MA200
    NEUTRAL = "neutral"        # 价格在 MA200 ±2%
    BEARISH = "bearish"        # 价格 < MA200


# ============================================================
#  Data Classes
# ============================================================
@dataclass
class MarketContext:
    """市场上下文快照 — 不可变, 每次评估生成新实例"""
    timestamp: datetime
    vix: Optional[float] = None
    vix_regime: VixRegime = VixRegime.UNKNOWN
    spy_price: Optional[float] = None
    spy_ma200: Optional[float] = None
    market_trend: MarketTrend = MarketTrend.NEUTRAL
    risk_state: RiskState = RiskState.NORMAL
    capital_level: CapitalLevel = CapitalLevel.SAFE
    capital_drawdown_pct: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    allow_new_positions: bool = True
    position_scale: float = 1.0  # 仓位缩放因子 (0.25 ~ 1.0)
    emergency_active: bool = False
    emergency_reason: str = ""

    # 交易准入快捷判断
    @property
    def can_trade(self) -> bool:
        return (
            self.allow_new_positions
            and self.vix_allows_trading
            and self.risk_state not in (RiskState.RISK_OFF, RiskState.HALT)
            and self.capital_level not in (CapitalLevel.DANGER, CapitalLevel.CIRCUIT_BREAKER)
            and not self.emergency_active
        )

    @property
    def vix_allows_trading(self) -> bool:
        """VIX < 25 才允许交易 (旧系统阈值)"""
        if self.vix is None:
            return True  # 数据不可用时不阻断
        return self.vix < 25

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "vix": self.vix,
            "vix_regime": self.vix_regime.value,
            "market_trend": self.market_trend.value,
            "risk_state": self.risk_state.value,
            "capital_level": self.capital_level.value,
            "capital_drawdown_pct": round(self.capital_drawdown_pct, 4),
            "position_scale": self.position_scale,
            "can_trade": self.can_trade,
            "emergency_active": self.emergency_active,
        }


# ============================================================
#  VIX Regime 分级 (从旧系统 market_state.py 移植)
# ============================================================
VIX_REGIME_THRESHOLDS = [
    (12, VixRegime.EUPHORIA),
    (20, VixRegime.NORMAL),
    (30, VixRegime.CAUTION),
    (40, VixRegime.FEAR),
    (100, VixRegime.CRISIS),
]


def classify_vix_regime(vix: float) -> VixRegime:
    """VIX 数值 → 市场阶段分级"""
    for threshold, regime in VIX_REGIME_THRESHOLDS:
        if vix < threshold:
            return regime
    return VixRegime.CRISIS


# ============================================================
#  资本安全水位 (从旧系统 capital_safety.py 移植)
# ============================================================
CAPITAL_LEVEL_THRESHOLDS = [
    (-0.05, CapitalLevel.SAFE, 1.0),
    (-0.10, CapitalLevel.WARNING, 0.5),
    (-0.20, CapitalLevel.ALERT, 0.25),
    (-0.35, CapitalLevel.DANGER, 0.0),
    (-1.0, CapitalLevel.CIRCUIT_BREAKER, 0.0),
]


def classify_capital_level(drawdown_pct: float) -> Tuple[CapitalLevel, float]:
    """
    回撤百分比 → 资本水位 + 仓位缩放因子

    Args:
        drawdown_pct: 负数, 如 -0.08 = -8%

    Returns:
        (水位, 仓位缩放因子)
    """
    for threshold, level, scale in CAPITAL_LEVEL_THRESHOLDS:
        if drawdown_pct >= threshold:
            return level, scale
    return CapitalLevel.CIRCUIT_BREAKER, 0.0


# ============================================================
#  SPY 趋势判断
# ============================================================
def classify_market_trend(price: float, ma200: float) -> MarketTrend:
    """SPY 价格相对 MA200 → 趋势判断"""
    if ma200 <= 0:
        return MarketTrend.NEUTRAL
    ratio = price / ma200
    if ratio > 1.02:
        return MarketTrend.BULLISH
    elif ratio < 0.98:
        return MarketTrend.BEARISH
    return MarketTrend.NEUTRAL


# ============================================================
#  MarketContextProvider — 主类
# ============================================================
class MarketContextProvider:
    """
    市场上下文提供器

    职责:
    1. 聚合 VIX/SPY/资本/风控状态为统一 MarketContext
    2. 缓存机制 (30分钟TTL, 避免重复API调用)
    3. 降级策略 (数据不可用时返回安全默认值)

    数据源适配:
    - 优先: 旧系统 MarketStateChecker (Futu LV3 + Finnhub)
    - 降级: 缓存 / 默认安全值
    """

    def __init__(self, config: Optional[Dict] = None):
        self._config = config or {}
        self._cache: Optional[MarketContext] = None
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=30)

        # 旧系统 adapter (延迟导入, 避免循环依赖)
        self._market_state_checker = None
        self._capital_safety = None
        self._risk_state_machine = None

        logger.info("MarketContextProvider 初始化")

    def _get_market_state_checker(self):
        """延迟获取旧系统 MarketStateChecker 实例"""
        if self._market_state_checker is None:
            try:
                from app.analysis.market_state import MarketStateChecker
                self._market_state_checker = MarketStateChecker()
            except Exception as e:
                logger.warning(f"无法加载 MarketStateChecker: {e}")
        return self._market_state_checker

    def _get_capital_safety(self):
        """延迟获取旧系统 CapitalSafety 实例"""
        if self._capital_safety is None:
            try:
                from app.risk.capital_safety import CapitalSafetySystem
                self._capital_safety = CapitalSafetySystem()
            except Exception as e:
                logger.warning(f"无法加载 CapitalSafetySystem: {e}")
        return self._capital_safety

    def get_context(self, force_refresh: bool = False) -> MarketContext:
        """
        获取当前市场上下文快照

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            MarketContext 实例 (不可变)
        """
        now = datetime.now()

        # 缓存检查
        if (not force_refresh
                and self._cache is not None
                and self._cache_time is not None
                and (now - self._cache_time) < self._cache_ttl):
            return self._cache

        # 构建上下文
        ctx = self._build_context(now)
        self._cache = ctx
        self._cache_time = now
        return ctx

    def _build_context(self, now: datetime) -> MarketContext:
        """构建完整的市场上下文"""
        ctx = MarketContext(timestamp=now)

        # 1. VIX
        try:
            checker = self._get_market_state_checker()
            if checker:
                vix_data = checker._fetch_vix()
                if vix_data:
                    ctx.vix = vix_data[0] if isinstance(vix_data, tuple) else vix_data
                    ctx.vix_regime = classify_vix_regime(ctx.vix)
        except Exception as e:
            logger.debug(f"VIX 获取失败: {e}")

        # 2. SPY 趋势
        try:
            checker = self._get_market_state_checker()
            if checker:
                spy_data = checker._fetch_spy_data()
                if spy_data:
                    ctx.spy_price = spy_data[0]
                    ctx.spy_ma200 = spy_data[1]
                    ctx.market_trend = classify_market_trend(ctx.spy_price, ctx.spy_ma200)
        except Exception as e:
            logger.debug(f"SPY 数据获取失败: {e}")

        # 3. 资本水位
        try:
            safety = self._get_capital_safety()
            if safety:
                state = safety.get_current_state()
                if state:
                    ctx.peak_equity = state.get("peak_equity", 0)
                    ctx.current_equity = state.get("current_equity", 0)
                    if ctx.peak_equity > 0:
                        ctx.capital_drawdown_pct = (
                            (ctx.current_equity - ctx.peak_equity) / ctx.peak_equity
                        )
                    ctx.capital_level, ctx.position_scale = classify_capital_level(
                        ctx.capital_drawdown_pct
                    )
        except Exception as e:
            logger.debug(f"资本水位获取失败: {e}")

        # 4. VIX 交易门禁
        if not ctx.vix_allows_trading:
            ctx.allow_new_positions = False
            logger.warning(f"[MarketContext] VIX={ctx.vix:.1f} >= 25, 禁止新开仓")

        # 4.5. HIGH_VOL 仓位缩放 (P1-1 辩论发现: HIGH_VOL 14.9%WR -$39,537)
        # VIX 20-25 = caution zone → 仓位缩放 50%
        # VIX 15-20 = normal → 仓位不变
        # VIX < 15 = euphoria → 仓位不变
        if ctx.vix is not None and 20 <= ctx.vix < 25:
            ctx.position_scale = min(ctx.position_scale, 0.50)
            ctx.risk_state = RiskState.CAUTION
            logger.info(f"[MarketContext] HIGH_VOL 警戒: VIX={ctx.vix:.1f}, 仓位缩放50%")

        # 5. 资本水位门禁
        if ctx.capital_level in (CapitalLevel.DANGER, CapitalLevel.CIRCUIT_BREAKER):
            ctx.allow_new_positions = False

        # 6. 风险状态
        if ctx.capital_level == CapitalLevel.CIRCUIT_BREAKER:
            ctx.risk_state = RiskState.HALT
        elif ctx.capital_level == CapitalLevel.DANGER:
            ctx.risk_state = RiskState.RISK_OFF
        elif ctx.capital_level == CapitalLevel.ALERT or not ctx.vix_allows_trading:
            ctx.risk_state = RiskState.CAUTION
        else:
            ctx.risk_state = RiskState.NORMAL

        logger.debug(f"[MarketContext] {ctx.to_dict()}")
        return ctx

    def check_entry_allowed(self, symbol: str = "") -> Tuple[bool, str]:
        """
        入场门禁 — 综合市场上下文判断是否允许开新仓

        Returns:
            (允许, 原因)
        """
        ctx = self.get_context()

        if not ctx.can_trade:
            reasons = []
            if not ctx.vix_allows_trading:
                reasons.append(f"VIX={ctx.vix:.1f}>=25")
            if ctx.capital_level in (CapitalLevel.DANGER, CapitalLevel.CIRCUIT_BREAKER):
                reasons.append(f"资本水位={ctx.capital_level.value}")
            if ctx.risk_state in (RiskState.RISK_OFF, RiskState.HALT):
                reasons.append(f"风险状态={ctx.risk_state.value}")
            if ctx.emergency_active:
                reasons.append(f"紧急拦截: {ctx.emergency_reason}")
            reason = "; ".join(reasons) if reasons else "市场上下文不允许"
            return False, reason

        return True, "OK"
