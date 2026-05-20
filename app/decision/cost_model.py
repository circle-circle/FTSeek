"""
交易成本模型 (Cost Model)

用途
----
把"信号分数"翻译成"净边际 net_edge_bps", 扣除:
  * 买卖价差 (spread)
  * 滑点 / 冲击 (Almgren-Chriss 简化)
  * 佣金 + SEC/TAF/exchange fee
  * 借券费 (做空场景)
  * 期权 bid-ask 宽度
后若剩余 expected_return < 门槛, 直接在 signal_scorer 里扣重分甚至满扣.

核心公式 (股票)
---------------
  half_spread_bps = 0.5 * spread_bps
  impact_bps      = k_impact * sqrt(qty / ADV)
  session_mult    = 1.0 (RTH) | 3.0 (pre/after)
  total_bps       = session_mult * (half_spread_bps + impact_bps) * 2   # 双边
                  + fixed_fee_bps + commission_bps + borrow_bps

期权
----
  spread_bps = (ask - bid) / mid * 1e4
  total_bps  = 0.5 * spread_bps * 2 + option_exchange_fee_bps + commission_bps
  若 spread_pct > option_max_spread_pct -> rejected=True

对外接口
--------
CostModelConfig / CostBreakdown
CostModel:
  estimate(symbol, side, qty, price, market_ctx, asset) -> CostBreakdown
  net_edge_bps(signal, market_ctx) -> float
  score_penalty(signal, market_ctx) -> int (in [max_penalty, 0])

作者: FTSeek Quant Team  创建: 2026-04-28
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.core.models import SignalScore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class CostModelConfig:
    # ---- 股票 ---- #
    default_stock_spread_bps: float = 3.0
    impact_coef_bps: float = 12.0
    default_adv_shares: float = 5_000_000.0
    commission_per_share: float = 0.0
    fixed_fee_bps: float = 0.3
    pre_after_session_mult: float = 3.0
    default_borrow_rate: float = 0.02
    assumed_holding_days: float = 5.0

    # ---- 期权 ---- #
    option_commission_per_contract: float = 0.65
    option_exchange_fee_bps: float = 1.0
    option_max_spread_pct: float = 0.05
    default_option_spread_bps: float = 80.0

    # ---- 净边际门槛 ---- #
    net_edge_floor_bps: float = 5.0
    net_edge_safe_bps: float = 25.0
    max_penalty: int = -20


@dataclass
class CostBreakdown:
    symbol: str = ""
    asset: str = "stock"
    side: str = "LONG"
    qty: float = 0.0
    price: float = 0.0

    spread_bps: float = 0.0
    impact_bps: float = 0.0
    fixed_fee_bps: float = 0.0
    borrow_bps: float = 0.0
    commission_bps: float = 0.0

    total_bps: float = 0.0
    total_usd: float = 0.0

    notional_usd: float = 0.0
    participation_rate: float = 0.0
    session: str = "RTH"
    rejected: bool = False
    reject_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "asset": self.asset, "side": self.side,
            "qty": self.qty, "price": self.price,
            "notional_usd": round(self.notional_usd, 2),
            "spread_bps": round(self.spread_bps, 2),
            "impact_bps": round(self.impact_bps, 2),
            "fixed_fee_bps": round(self.fixed_fee_bps, 2),
            "borrow_bps": round(self.borrow_bps, 2),
            "commission_bps": round(self.commission_bps, 2),
            "total_bps": round(self.total_bps, 2),
            "total_usd": round(self.total_usd, 2),
            "participation_rate": round(self.participation_rate, 4),
            "session": self.session,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
        }


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class CostModel:
    def __init__(self, cfg: Optional[CostModelConfig] = None):
        self.cfg = cfg or CostModelConfig()

    # ---------- 总入口 ---------- #
    def estimate(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        market_ctx: Optional[Dict[str, Any]] = None,
        asset: str = "stock",
    ) -> CostBreakdown:
        ctx = market_ctx or {}
        if asset == "option":
            return self._estimate_option(symbol, side, qty, price, ctx)
        return self._estimate_stock(symbol, side, qty, price, ctx)

    # ---------- 股票 ---------- #
    def _estimate_stock(
        self, symbol: str, side: str, qty: float, price: float, ctx: Dict[str, Any],
    ) -> CostBreakdown:
        cfg = self.cfg
        notional = max(0.0, qty * price)
        spread_bps = float(ctx.get("spread_bps") or cfg.default_stock_spread_bps)
        adv = float(ctx.get("adv_shares") or cfg.default_adv_shares)
        part = qty / adv if adv > 0 else 0.0
        impact_bps = cfg.impact_coef_bps * math.sqrt(max(0.0, part))

        session = str(ctx.get("session", "RTH")).upper()
        mult = cfg.pre_after_session_mult if session in (
            "PRE", "POST", "PRE_MARKET", "AFTER_HOURS"
        ) else 1.0

        commission_usd = qty * cfg.commission_per_share
        commission_bps = (commission_usd / notional * 1e4) if notional > 0 else 0.0

        borrow_bps = 0.0
        if side.upper() == "SHORT":
            annual_rate = float(ctx.get("borrow_rate") or cfg.default_borrow_rate)
            hold_days = float(ctx.get("hold_days") or cfg.assumed_holding_days)
            borrow_bps = annual_rate * (hold_days / 252.0) * 1e4

        half_spread_bps = 0.5 * spread_bps
        liquidity_bps = mult * (half_spread_bps + impact_bps) * 2.0  # 双边
        total_bps = liquidity_bps + cfg.fixed_fee_bps + commission_bps + borrow_bps
        total_usd = total_bps * notional / 1e4 + commission_usd

        return CostBreakdown(
            symbol=symbol, asset="stock", side=side, qty=qty, price=price,
            notional_usd=notional, spread_bps=spread_bps, impact_bps=impact_bps,
            fixed_fee_bps=cfg.fixed_fee_bps, borrow_bps=borrow_bps,
            commission_bps=commission_bps, total_bps=total_bps, total_usd=total_usd,
            participation_rate=part, session=session,
        )

    # ---------- 期权 ---------- #
    def _estimate_option(
        self, symbol: str, side: str, qty: float, price: float, ctx: Dict[str, Any],
    ) -> CostBreakdown:
        cfg = self.cfg
        notional = max(0.0, qty * price * 100.0)

        bid = ctx.get("bid")
        ask = ctx.get("ask")
        if bid and ask and ask > 0 and (ask + bid) > 0:
            spread_pct = (ask - bid) / ((ask + bid) / 2.0)
            spread_bps = spread_pct * 1e4
        else:
            spread_bps = float(ctx.get("spread_bps") or cfg.default_option_spread_bps)
            spread_pct = spread_bps / 1e4

        rejected = spread_pct > cfg.option_max_spread_pct
        reject_reason = (
            f"option spread {spread_pct*100:.1f}% > cap {cfg.option_max_spread_pct*100:.1f}%"
            if rejected else ""
        )

        commission_usd = qty * cfg.option_commission_per_contract
        commission_bps = (commission_usd / notional * 1e4) if notional > 0 else 0.0

        half_spread_bps = 0.5 * spread_bps * 2.0   # 双边
        total_bps = half_spread_bps + cfg.option_exchange_fee_bps + commission_bps
        total_usd = total_bps * notional / 1e4 + commission_usd

        return CostBreakdown(
            symbol=symbol, asset="option", side=side, qty=qty, price=price,
            notional_usd=notional, spread_bps=spread_bps, impact_bps=0.0,
            fixed_fee_bps=cfg.option_exchange_fee_bps, borrow_bps=0.0,
            commission_bps=commission_bps, total_bps=total_bps, total_usd=total_usd,
            participation_rate=0.0,
            session=str(ctx.get("session", "RTH")).upper(),
            rejected=rejected, reject_reason=reject_reason,
        )

    # ---------- 与 Signal 对接 ---------- #
    def net_edge_bps(
        self,
        signal: SignalScore,
        market_ctx: Optional[Dict[str, Any]] = None,
    ) -> float:
        """net_edge = expected_return_bps - total_cost_bps."""
        ctx = market_ctx or {}
        breakdown = self.estimate(
            symbol=signal.symbol,
            side=signal.direction.value,
            qty=float(ctx.get("qty", 0.0) or 0.0),
            price=float(ctx.get("price", 0.0) or 0.0),
            market_ctx=ctx,
            asset=str(ctx.get("asset", "stock")),
        )
        k_return = float(ctx.get("return_per_score_bps") or 5.0)
        expected_bps = max(0.0, signal.strength * k_return)
        if signal.risk_reward_estimate > 0:
            expected_bps *= min(1.3, max(0.7, signal.risk_reward_estimate / 2.0))
        return expected_bps - breakdown.total_bps

    def score_penalty(
        self,
        signal: SignalScore,
        market_ctx: Optional[Dict[str, Any]] = None,
    ) -> int:
        """线性插值扣分, net_edge >= safe -> 0, <= floor -> max_penalty."""
        cfg = self.cfg
        try:
            ne = self.net_edge_bps(signal, market_ctx)
        except Exception as e:
            logger.debug("[cost] net_edge calc failed: %s", e)
            return 0

        if ne >= cfg.net_edge_safe_bps:
            return 0
        if ne <= cfg.net_edge_floor_bps:
            return cfg.max_penalty
        span = cfg.net_edge_safe_bps - cfg.net_edge_floor_bps
        frac = (cfg.net_edge_safe_bps - ne) / span
        return int(round(cfg.max_penalty * frac))


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from app.core.models import Direction as _D, SignalScore as _S

    model = CostModel()
    print("[case1 stock RTH]", model.estimate(
        "AAPL", "LONG", 500, 175.0,
        {"spread_bps": 2.5, "adv_shares": 5e7, "session": "RTH"},
    ).to_dict())

    print("[case2 stock PRE]", model.estimate(
        "AAPL", "LONG", 500, 175.0,
        {"spread_bps": 15, "adv_shares": 5e7, "session": "PRE"},
    ).to_dict())

    print("[case3 short HTB]", model.estimate(
        "GME", "SHORT", 200, 25,
        {"spread_bps": 5, "adv_shares": 1e7, "borrow_rate": 0.08, "hold_days": 5},
    ).to_dict())

    print("[case4 option wide]", model.estimate(
        "AAPL241220P00170000", "LONG", 2, 1.10,
        {"bid": 1.00, "ask": 1.20}, asset="option",
    ).to_dict())

    sig = _S(strategy_id="price_break", symbol="AAPL", direction=_D.LONG,
             strength=70, confidence=0.75, risk_reward_estimate=2.0)
    good_ctx = {"qty": 500, "price": 175, "spread_bps": 2.5, "adv_shares": 5e7, "session": "RTH"}
    bad_ctx  = {"qty": 500, "price": 175, "spread_bps": 25,  "adv_shares": 2e5, "session": "PRE"}
    print(f"[case5 good] ne={model.net_edge_bps(sig, good_ctx):.1f}  pen={model.score_penalty(sig, good_ctx)}")
    print(f"[case5 bad ] ne={model.net_edge_bps(sig, bad_ctx):.1f}  pen={model.score_penalty(sig, bad_ctx)}")
