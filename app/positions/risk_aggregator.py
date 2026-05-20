"""跨 source 风险敞口聚合 (2026-05-16)

风控铁律: 总敞口 = Σ(全部 source 头寸), 不能"策略盲区".
但策略可用资金 = 总敞口 - 受保护仓位占用 (不能挤占用户手动仓的额度).
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from .source import (
    PositionSource, ProtectionState,
    get_position_source, get_protection_state,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskExposureSnapshot:
    """风险敞口快照 — 跨 source 全量"""
    total_market_value: float = 0.0      # 全部头寸名义价值
    total_exposure_pct: float = 0.0      # / equity
    by_source: Dict[str, float] = None   # {source: market_value}
    by_protection: Dict[str, float] = None  # {protection: market_value}
    protected_locked_value: float = 0.0  # PROTECTED + FROZEN 占用 (策略不可用)
    strategy_available_value: float = 0.0  # equity - protected_locked
    n_positions: int = 0
    n_protected: int = 0
    n_managed: int = 0
    n_frozen: int = 0
    long_value: float = 0.0
    short_value: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "total_market_value": self.total_market_value,
            "total_exposure_pct": self.total_exposure_pct,
            "by_source": self.by_source or {},
            "by_protection": self.by_protection or {},
            "protected_locked_value": self.protected_locked_value,
            "strategy_available_value": self.strategy_available_value,
            "n_positions": self.n_positions,
            "n_protected": self.n_protected,
            "n_managed": self.n_managed,
            "n_frozen": self.n_frozen,
            "long_value": self.long_value,
            "short_value": self.short_value,
        }


class RiskAggregator:
    """跨 source 风险敞口聚合器 — 单例可"""

    def __init__(self, position_store=None):
        self.position_store = position_store

    def snapshot(self, equity: float = 0.0) -> RiskExposureSnapshot:
        """计算当前快照. equity 为 0 时, exposure_pct 跳过."""
        snap = RiskExposureSnapshot(by_source={}, by_protection={})
        if self.position_store is None:
            return snap

        try:
            positions = self.position_store.all()
        except Exception as e:
            logger.warning(f"[RiskAggregator] position_store.all 失败: {e}")
            return snap

        for p in positions:
            try:
                size = float(getattr(p, "size", 0) or 0)
                price = float(getattr(p, "avg_entry_price", 0) or 0)
                mv = abs(size * price)
                if mv <= 0:
                    continue

                src = get_position_source(p)
                ps = get_protection_state(p)

                snap.total_market_value += mv
                snap.by_source[src.value] = snap.by_source.get(src.value, 0.0) + mv
                snap.by_protection[ps.value] = snap.by_protection.get(ps.value, 0.0) + mv
                snap.n_positions += 1

                if ps == ProtectionState.PROTECTED or ps == ProtectionState.FROZEN:
                    snap.protected_locked_value += mv

                if ps == ProtectionState.PROTECTED:
                    snap.n_protected += 1
                elif ps == ProtectionState.MANAGED:
                    snap.n_managed += 1
                elif ps == ProtectionState.FROZEN:
                    snap.n_frozen += 1

                # 多空敞口
                _dir = getattr(p, "direction", None)
                _dir_str = _dir.value if hasattr(_dir, "value") else str(_dir)
                if "LONG" in _dir_str.upper():
                    snap.long_value += mv
                elif "SHORT" in _dir_str.upper():
                    snap.short_value += mv
            except Exception as e:
                logger.debug(f"[RiskAggregator] position {p.symbol} 处理异常: {e}")

        if equity > 0:
            snap.total_exposure_pct = snap.total_market_value / equity
            snap.strategy_available_value = max(0.0, equity - snap.protected_locked_value)

        return snap

    def get_strategy_budget_pct(self, equity: float, total_cap_pct: float) -> float:
        """策略可用预算 % — 总预算 - 受保护占用 / equity.

        即 PROTECTED/FROZEN 仓位占用的额度对策略不可见 (它们不计入策略可分配预算).
        """
        if equity <= 0:
            return total_cap_pct
        snap = self.snapshot(equity)
        protected_pct = snap.protected_locked_value / equity
        return max(0.0, total_cap_pct - protected_pct)
