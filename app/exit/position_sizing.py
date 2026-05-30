"""
ATR 仓位控制 (PositionSizing) — P1-2 行动项

核心逻辑:
- 每笔风险 = capital × risk_per_trade_pct (默认 2%)
- 股数 = 风险金额 / (ATR × sl_multiplier)
- 上限: 单股不超过 capital × max_position_pct (默认 15%)
- 下限: 至少 min_shares 股 (默认 10)

配合 Edge Filter:
- 计算 edge/cost 比值
- 仅在 edge ≥ min_edge_ratio × cost 时入场

数据验证 (9标的×251天, GDIAN[5,10)+MFFF>0):
  固定100股: PF=1.22, 底90%=-171%
  ATR仓位+Edge: PF=1.69, 底90%=+28%

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

from .cost_model import CostModel

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    """仓位计算结果"""
    shares: int                  # 建议股数
    position_value: float        # 持仓市值
    risk_dollars: float          # 风险金额
    cost_estimate: float         # 估算往返成本
    edge_estimate: float         # 估算预期收益
    edge_cost_ratio: float       # edge/cost 比值
    should_trade: bool           # 是否应该入场
    reason: str                  # 决策原因

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shares": self.shares,
            "position_value": round(self.position_value, 2),
            "risk_dollars": round(self.risk_dollars, 2),
            "cost_estimate": round(self.cost_estimate, 2),
            "edge_estimate": round(self.edge_estimate, 2),
            "edge_cost_ratio": round(self.edge_cost_ratio, 2),
            "should_trade": self.should_trade,
            "reason": self.reason,
        }


class PositionSizing:
    """
    ATR 仓位控制器

    Usage:
        sizer = PositionSizing(capital=100000)
        result = sizer.calculate(
            price=150.0, atr=3.0,
            cost_model=CostModel.ibkr(),
        )
        if result.should_trade:
            print(f"Buy {result.shares} shares")
    """

    def __init__(
        self,
        capital: float = 100000.0,
        # Task #6 (2026-05-30): 0.005 → 0.008 提升单笔风险预算
        risk_per_trade_pct: float = 0.008,
        # Task #6 (2026-05-30): 0.15 → 0.12 收紧单标的上限
        max_position_pct: float = 0.12,
        min_shares: int = 10,
        sl_atr_mult: float = 1.0,
        tp_pct: float = 0.04,
        expected_win_rate: float = 0.55,
        min_edge_ratio: float = 1.5,
        watchlist_config: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.capital = capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.min_shares = min_shares
        self.sl_atr_mult = sl_atr_mult
        self.tp_pct = tp_pct
        self.expected_win_rate = expected_win_rate
        self.min_edge_ratio = min_edge_ratio
        # Task #6 (2026-05-30): 按 watchlist 中的 max_position_ratio / tier 分级设仓位上限
        # watchlist_config: {"TSLA": {"max_position_ratio": 0.15, "tier": "TIER1"}, ...}
        self.watchlist_config: Dict[str, Dict[str, Any]] = watchlist_config or {}

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """去掉可能的 'US.' 前缀, 统一为大写。"""
        if not symbol:
            return ""
        s = symbol.strip().upper()
        if s.startswith("US."):
            s = s[3:]
        return s

    def _get_symbol_config(self, symbol: str, key: str) -> Optional[Any]:
        """从 watchlist 配置中读取指定 key。未配置返回 None。"""
        if not symbol:
            return None
        s = self._normalize_symbol(symbol)
        cfg = self.watchlist_config.get(s) or self.watchlist_config.get(symbol)
        if not cfg:
            return None
        return cfg.get(key)

    def _get_max_position_ratio(self, symbol: str) -> float:
        """按标的 tier 分级返回最大仓位比例。

        优先顺序:
          1. watchlist 中显式配置的 max_position_ratio
          2. tier 默认规则: TIER1=0.10, TIER2=0.12
          3. 实例默认 self.max_position_pct (0.12)
        """
        # 1) watchlist 显式配置
        watchlist_ratio = self._get_symbol_config(symbol, "max_position_ratio")
        if watchlist_ratio is not None:
            try:
                return float(watchlist_ratio)
            except (TypeError, ValueError):
                logger.warning(
                    "[position_sizing] invalid max_position_ratio for %s: %r",
                    symbol, watchlist_ratio,
                )
        # 2) tier 默认规则
        tier = self._get_symbol_config(symbol, "tier")
        if tier == "TIER1":
            return 0.10  # TIER1 默认 10%
        # 3) 实例默认值 (12%)
        return self.max_position_pct

    def calculate(
        self,
        price: float,
        atr: float,
        cost_model: Optional[CostModel] = None,
        symbol: Optional[str] = None,
    ) -> SizingResult:
        """
        计算建议仓位

        Args:
            price: 当前价格
            atr: 当前 ATR
            cost_model: 成本模型 (None = IBKR)
            symbol: 可选标的代码 — 提供后启用 tier 分级仓位上限 (Task #6)

        Returns:
            SizingResult
        """
        if cost_model is None:
            cost_model = CostModel.ibkr()

        if price <= 0 or atr <= 0:
            return SizingResult(
                shares=0, position_value=0, risk_dollars=0,
                cost_estimate=0, edge_estimate=0, edge_cost_ratio=0,
                should_trade=False, reason="invalid price or atr",
            )

        # 1. 风险金额 (核心公式: shares = (capital × risk_pct) / (ATR × sl_mult))
        risk_dollars = self.capital * self.risk_per_trade_pct

        # 2. 止损距离
        sl_dollars = atr * self.sl_atr_mult

        # 3. 基础股数 (风险 / 止损)
        base_shares = int(risk_dollars / sl_dollars)

        # 4. 上限: 按 symbol tier 分级取最大持仓比例 (Task #6)
        max_position_pct = (
            self._get_max_position_ratio(symbol)
            if symbol else self.max_position_pct
        )
        max_shares = int(self.capital * max_position_pct / price)

        # 5. 最终股数
        shares = max(self.min_shares, min(base_shares, max_shares))

        # 6. 持仓市值
        position_value = shares * price

        # 7. 成本估算
        cost = cost_model.estimate(price, atr, shares)

        # 8. Edge 估算
        edge_info = cost_model.estimate_edge(
            price=price, atr=atr, shares=shares,
            tp_pct=self.tp_pct, sl_atr_mult=self.sl_atr_mult,
            win_rate=self.expected_win_rate,
        )

        edge_estimate = edge_info["total_edge"]
        edge_cost_ratio = edge_info["edge_cost_ratio"]

        # 9. 决策
        should_trade = edge_cost_ratio >= self.min_edge_ratio

        if not should_trade:
            reason = (
                f"edge/cost={edge_cost_ratio:.2f} < {self.min_edge_ratio:.1f} "
                f"(edge=${edge_estimate:.0f}, cost=${cost.round_trip:.0f})"
            )
        elif shares < self.min_shares:
            should_trade = False
            reason = f"shares={shares} < min={self.min_shares}"
        else:
            reason = (
                f"OK: {shares}shares, edge=${edge_estimate:.0f}, "
                f"cost=${cost.round_trip:.0f}, ratio={edge_cost_ratio:.1f}x"
            )

        return SizingResult(
            shares=shares,
            position_value=position_value,
            risk_dollars=risk_dollars,
            cost_estimate=cost.round_trip,
            edge_estimate=edge_estimate,
            edge_cost_ratio=edge_cost_ratio,
            should_trade=should_trade,
            reason=reason,
        )
