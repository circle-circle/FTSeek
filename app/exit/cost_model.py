"""
交易成本模型 (CostModel) — P1-1 行动项

基于真实券商费率的交易成本估算:
- 佣金: IBKR $0.005/share (tiered), min $1/trade
- 滑点: ATR 的固定比例 (大盘股 ~3% ATR)
- 买卖价差: 按 bps 估算 (大盘股 ~3bps)

用途:
- 入场前估算往返成本
- Edge Filter: 只在预期收益 > N × 成本时入场
- 回测时注入真实成本

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CostEstimate:
    """成本估算结果"""
    commission: float        # 佣金 (美元)
    slippage: float          # 滑点 (美元)
    spread: float            # 价差 (美元)
    total_per_side: float    # 单边总成本
    round_trip: float        # 往返总成本
    shares: int              # 股数
    cost_per_share: float    # 每股往返成本

    @property
    def cost_pct(self) -> float:
        """成本占入场金额的百分比"""
        return 0.0  # 需要 price 才能计算

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commission": round(self.commission, 2),
            "slippage": round(self.slippage, 2),
            "spread": round(self.spread, 2),
            "total_per_side": round(self.total_per_side, 2),
            "round_trip": round(self.round_trip, 2),
            "shares": self.shares,
            "cost_per_share": round(self.cost_per_share, 4),
        }


class CostModel:
    """
    交易成本模型

    预设配置:
    - IBKR_TIERED: $0.005/share, min $1, 3bps spread, 3% ATR slippage
    - TYPICAL_RETAIL: $0.01/share, min $1, 5bps spread, 5% ATR slippage
    - ZERO: 零成本 (理想回测)

    Usage:
        model = CostModel.ibkr()
        estimate = model.estimate(price=150.0, atr=3.0, shares=100)
        print(f"Round trip: ${estimate.round_trip:.2f}")
    """

    def __init__(
        self,
        commission_per_share: float = 0.005,
        min_commission: float = 1.0,
        spread_bps: float = 3.0,
        slippage_atr_frac: float = 0.03,
    ):
        self.commission_per_share = commission_per_share
        self.min_commission = min_commission
        self.spread_bps = spread_bps
        self.slippage_atr_frac = slippage_atr_frac

    @classmethod
    def ibkr(cls) -> CostModel:
        """IBKR Tiered 费率"""
        return cls(
            commission_per_share=0.005,
            min_commission=1.0,
            spread_bps=3.0,
            slippage_atr_frac=0.03,
        )

    @classmethod
    def typical_retail(cls) -> CostModel:
        """典型零售券商费率"""
        return cls(
            commission_per_share=0.01,
            min_commission=1.0,
            spread_bps=5.0,
            slippage_atr_frac=0.05,
        )

    @classmethod
    def zero(cls) -> CostModel:
        """零成本 (理想回测)"""
        return cls(
            commission_per_share=0.0,
            min_commission=0.0,
            spread_bps=0.0,
            slippage_atr_frac=0.0,
        )

    def estimate(self, price: float, atr: float, shares: int) -> CostEstimate:
        """
        估算交易成本

        Args:
            price: 入场价格
            atr: 当前 ATR
            shares: 交易股数

        Returns:
            CostEstimate
        """
        # 佣金
        commission = max(self.min_commission, self.commission_per_share * shares)

        # 滑点 (基于 ATR)
        slippage_per_share = atr * self.slippage_atr_frac
        slippage = slippage_per_share * shares

        # 买卖价差
        spread_per_share = price * self.spread_bps / 10000
        spread = spread_per_share * shares

        # 单边总成本
        per_side = commission + slippage + spread
        round_trip = per_side * 2

        return CostEstimate(
            commission=commission * 2,  # 往返佣金
            slippage=slippage * 2,      # 往返滑点
            spread=spread * 2,          # 往返价差
            total_per_side=per_side,
            round_trip=round_trip,
            shares=shares,
            cost_per_share=round_trip / max(1, shares),
        )

    def estimate_edge(
        self,
        price: float,
        atr: float,
        shares: int,
        tp_pct: float = 0.04,
        sl_atr_mult: float = 1.0,
        win_rate: float = 0.55,
    ) -> Dict[str, float]:
        """
        估算预期收益 (edge) 和成本比

        Args:
            price: 入场价
            atr: ATR
            shares: 股数
            tp_pct: 止盈百分比 (默认 4%)
            sl_atr_mult: 止损 ATR 倍数 (默认 1.0)
            win_rate: 预期胜率 (默认 55%)

        Returns:
            {
                "edge_per_share": 预期每股收益,
                "total_edge": 预期总收益,
                "cost": 往返成本,
                "edge_cost_ratio": edge/cost 比值,
                "should_trade": edge > 1.5 × cost,
            }
        """
        tp_dollars = price * tp_pct
        sl_dollars = atr * sl_atr_mult

        # 期望值 = WR × TP - (1-WR) × SL
        edge_per_share = win_rate * tp_dollars - (1 - win_rate) * sl_dollars
        total_edge = edge_per_share * shares

        cost = self.estimate(price, atr, shares)
        ratio = total_edge / max(0.01, cost.round_trip)

        return {
            "edge_per_share": round(edge_per_share, 4),
            "total_edge": round(total_edge, 2),
            "cost": round(cost.round_trip, 2),
            "edge_cost_ratio": round(ratio, 2),
            "should_trade": ratio >= 1.5,
        }
