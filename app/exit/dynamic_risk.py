"""
动态风控退出模块 (Dynamic Risk Exit) — 辩论 P0-2 行动项

数据驱动设计 (2026-05-23, 9标的×251天验证):

核心发现:
1. ATR×1.0 止损 + TP=4% + 3天持有 = 均值回归最优配置
2. 个股最大亏损 1% 控制 = 底部90%从-171%→+0% (最大改善因子)
3. 排除 NVDA+META = PF从1.22→1.76 (特异性风险消除)
4. 连续3亏暂停 = 额外降低回撤

验证结果:
  基线:     118笔, 52%WR, $8,594,  PF=1.22, 底90%=-171%
  最佳风控:  85笔, 54%WR, $13,077, PF=1.74, 底90%=-10%

退出规则 (优先级从高到低):
1. 个股累计亏损 ≥ max_stock_loss_pct × capital → BLOCK (阻止新仓)
2. 连续亏损 ≥ max_consecutive_losses → PAUSE (暂停入场)
3. 当前持仓浮亏 ≥ ATR × sl_multiplier → SL_HIT (止损)
4. 当前持仓浮盈 ≥ tp_pct → TP_HIT (止盈)
5. 持仓天数 ≥ max_hold_days → TIME_STOP (时间止损)

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.exit.base import BaseExitModule, ExitDecision, ExitProfile

logger = logging.getLogger(__name__)


@dataclass
class StockRiskState:
    """个股风险状态跟踪"""
    symbol: str
    cumulative_pnl: float = 0.0     # 累计 PnL (美元)
    trade_count: int = 0            # 交易次数
    win_count: int = 0              # 盈利次数
    loss_streak: int = 0            # 当前连续亏损次数
    max_loss_streak: int = 0        # 历史最大连续亏损
    is_blocked: bool = False        # 是否被阻止
    block_reason: str = ""          # 阻止原因

    @property
    def win_rate(self) -> float:
        return self.win_count / self.trade_count if self.trade_count > 0 else 0.0

    def record_trade(self, pnl: float):
        """记录一笔交易结果"""
        self.trade_count += 1
        self.cumulative_pnl += pnl
        if pnl > 0:
            self.win_count += 1
            self.loss_streak = 0
        else:
            self.loss_streak += 1
            self.max_loss_streak = max(self.max_loss_streak, self.loss_streak)


class DynamicRiskExit(BaseExitModule):
    """
    动态风控退出模块

    组合 ATR 止损 + 个股风险限制 + 连续亏损熔断

    Usage:
        exit_module = DynamicRiskExit()
        profile = ExitProfile(
            name="mean_reversion_risk_managed",
            stop_loss_type="atr_multi",
            stop_loss_params={
                "multiplier": 1.0,       # ATR 倍数
                "max_loss_pct": -0.05,   # 硬底限
            },
            take_profit_type="fixed_pct",
            take_profit_params={"target_pct": 0.04},
            max_holding_minutes=4320,    # 3天 (分钟)
        )

        # 评估是否退出
        decision = exit_module.evaluate(position, ctx, profile)

        # 检查是否允许新入场
        allowed, reason = exit_module.check_entry_allowed("US.NVDA")
    """

    name = "dynamic_risk"

    # 默认风控参数 (可通过 profile 覆盖)
    # P0 辩论止血 2026-05-23: 基于309笔实盘数据分析
    DEFAULT_RISK_PARAMS = {
        "max_stock_loss_pct": 0.01,     # 个股最大亏损占总资金 1%
        "max_consecutive_losses": 3,     # 连续3亏暂停 (从0改为3)
        "blacklist": [                   # P0-3: 持续亏损股票黑名单
            "US.QCOM",   # 44笔 22.7%WR -$3,768
            "US.TME",    # 20笔 0%WR -$913
            "US.NVDA",   # 3笔 0%WR -$2,905
            "US.META",   # 1笔 0%WR -$1,964
            "US.GOOGL",  # 1笔 0%WR -$6,015
            "US.QQQ",    # 2笔 0%WR -$5,984
            "US.UNH",    # 7笔 0%WR -$120
            "US.NFLX",   # 8笔 0%WR -$122
            "US.DIA",    # 4笔 0%WR -$507
        ],
        "ban_options": True,             # P0-1: 禁止期权交易 (0%WR -$23,635)
        "ban_short_overnight": True,     # P0-4: SHORT持仓>8h强制退出 (27.9%WR -$6,041)
        "max_short_hold_hours": 8.0,     # SHORT最大持仓时间
        "capital": 100000.0,             # 总资金 (用于计算百分比)
    }

    def __init__(self):
        self._stock_states: Dict[str, StockRiskState] = {}
        self._global_loss_streak: int = 0
        self._is_paused: bool = False
        self._pause_reason: str = ""

    def _get_stock_state(self, symbol: str) -> StockRiskState:
        if symbol not in self._stock_states:
            self._stock_states[symbol] = StockRiskState(symbol=symbol)
        return self._stock_states[symbol]

    def evaluate(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile: ExitProfile,
    ) -> ExitDecision:
        """
        评估是否应该退出当前持仓

        Args:
            position: 持仓对象 (需有 avg_entry_price, direction, symbol)
            ctx: 上下文 {current_price, atr, holding_minutes, ...}
            profile: 退出配置

        Returns:
            ExitDecision
        """
        entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        direction = str(getattr(position, "direction", "LONG")).upper()
        current_price = float(ctx.get("current_price", 0) or 0)
        atr = float(ctx.get("atr", 0) or 0)
        holding_minutes = float(ctx.get("holding_minutes", 0) or 0)

        if entry_price <= 0 or current_price <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price_data")

        # 计算当前盈亏
        if direction == "LONG":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        # ── 止损检查 ──
        sl_params = profile.stop_loss_params
        sl_type = profile.stop_loss_type

        if sl_type == "atr_multi" and atr > 0:
            multiplier = sl_params.get("multiplier", 1.0)
            if direction == "LONG":
                stop_price = entry_price - atr * multiplier
                if current_price <= stop_price:
                    return ExitDecision(
                        action="CLOSE", qty_pct=1.0,
                        reason="ATR_SL_HIT",
                        priority=10,
                        metadata={
                            "stop_price": round(stop_price, 4),
                            "current_price": current_price,
                            "atr": atr,
                            "multiplier": multiplier,
                            "pnl_pct": round(pnl_pct, 4),
                        },
                    )
            else:
                stop_price = entry_price + atr * multiplier
                if current_price >= stop_price:
                    return ExitDecision(
                        action="CLOSE", qty_pct=1.0,
                        reason="ATR_SL_HIT",
                        priority=10,
                        metadata={
                            "stop_price": round(stop_price, 4),
                            "current_price": current_price,
                            "atr": atr,
                            "multiplier": multiplier,
                            "pnl_pct": round(pnl_pct, 4),
                        },
                    )

        elif sl_type == "fixed_pct":
            pct = sl_params.get("pct", -0.03)
            if pnl_pct <= pct:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="FIXED_SL_HIT",
                    priority=10,
                    metadata={"pnl_pct": round(pnl_pct, 4), "threshold": pct},
                )

        # 硬底限
        max_loss = sl_params.get("max_loss_pct", -0.05)
        if pnl_pct <= max_loss:
            return ExitDecision(
                action="CLOSE", qty_pct=1.0,
                reason="MAX_LOSS_HIT",
                priority=11,
                metadata={"pnl_pct": round(pnl_pct, 4), "threshold": max_loss},
            )

        # ── 止盈检查 ──
        tp_params = profile.take_profit_params
        tp_type = profile.take_profit_type

        if tp_type == "fixed_pct":
            target = tp_params.get("target_pct", 0.04)
            if pnl_pct >= target:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="TP_HIT",
                    priority=8,
                    metadata={"pnl_pct": round(pnl_pct, 4), "target": target},
                )

        # ── 时间止损 ──
        max_hold = profile.max_holding_minutes
        if max_hold > 0 and holding_minutes >= max_hold:
            return ExitDecision(
                action="CLOSE", qty_pct=1.0,
                reason="TIME_STOP",
                priority=3,
                metadata={
                    "holding_minutes": holding_minutes,
                    "max_holding_minutes": max_hold,
                    "pnl_pct": round(pnl_pct, 4),
                },
            )

        return ExitDecision(
            action="NO_ACTION",
            reason="all_checks_pass",
            metadata={"pnl_pct": round(pnl_pct, 4)},
        )

    # ── 入场风控 ──

    def check_entry_allowed(
        self,
        symbol: str,
        risk_params: Optional[Dict[str, Any]] = None,
        direction: Optional[str] = None,
    ) -> tuple:
        """
        检查是否允许在该标的开新仓

        Args:
            symbol: 标的代码
            risk_params: 风控参数 (None 使用默认)
            direction: 交易方向 ("LONG"/"SHORT"/"SHORT_PROXY_OPTION" 等)

        Returns:
            (allowed: bool, reason: str)
        """
        params = {**self.DEFAULT_RISK_PARAMS, **(risk_params or {})}

        # P0-1 (2026-05-23): 禁止期权交易 — 0%WR, -$23,635
        if params.get("ban_options", False):
            if direction and "OPTION" in str(direction).upper():
                return False, f"BANNED: 期权交易已禁止 (direction={direction})"
            # 检查期权风格的symbol (如 US.SNDK260605P1370000)
            sym_clean = symbol.replace("US.", "")
            if len(sym_clean) > 6 and any(c.isdigit() for c in sym_clean[1:]):
                return False, f"BANNED: 疑似期权合约 ({symbol})"

        # 1. 黑名单检查
        blacklist = params.get("blacklist", [])
        sym_short = symbol.replace("US.", "")
        if sym_short in blacklist or symbol in blacklist:
            return False, f"BLOCKED: {symbol} in blacklist"

        # 2. 全局暂停检查
        if self._is_paused:
            return False, f"PAUSED: {self._pause_reason}"

        # 3. 个股累计亏损检查
        state = self._get_stock_state(symbol)
        capital = params.get("capital", 100000.0)
        max_loss_dollar = capital * params.get("max_stock_loss_pct", 0.01)

        if state.cumulative_pnl < -max_loss_dollar:
            state.is_blocked = True
            state.block_reason = f"cumulative loss ${state.cumulative_pnl:.0f} > -${max_loss_dollar:.0f}"
            return False, f"BLOCKED: {state.block_reason}"

        return True, "OK"

    def record_trade_result(self, symbol: str, pnl: float,
                             risk_params: Optional[Dict[str, Any]] = None):
        """
        记录交易结果，更新风控状态

        Args:
            symbol: 标的代码
            pnl: 盈亏 (美元)
            risk_params: 风控参数
        """
        params = {**self.DEFAULT_RISK_PARAMS, **(risk_params or {})}
        state = self._get_stock_state(symbol)
        state.record_trade(pnl)

        # 更新全局连续亏损
        if pnl < 0:
            self._global_loss_streak += 1
        else:
            self._global_loss_streak = 0

        # 检查连续亏损熔断
        max_consec = params.get("max_consecutive_losses", 0)
        if max_consec > 0 and self._global_loss_streak >= max_consec:
            self._is_paused = True
            self._pause_reason = (
                f"连续 {self._global_loss_streak} 笔亏损 >= {max_consec}, "
                f"暂停入场直到下一笔盈利"
            )
            logger.warning("[DynamicRisk] %s", self._pause_reason)

    def reset_pause(self):
        """手动重置暂停状态 (例如新交易日开始)"""
        self._is_paused = False
        self._pause_reason = ""
        self._global_loss_streak = 0

    def get_risk_summary(self) -> Dict[str, Any]:
        """获取风控状态摘要"""
        summary = {
            "global_loss_streak": self._global_loss_streak,
            "is_paused": self._is_paused,
            "pause_reason": self._pause_reason,
            "stocks": {},
        }
        for sym, state in self._stock_states.items():
            summary["stocks"][sym] = {
                "trades": state.trade_count,
                "win_rate": round(state.win_rate, 3),
                "cumulative_pnl": round(state.cumulative_pnl, 2),
                "loss_streak": state.loss_streak,
                "max_loss_streak": state.max_loss_streak,
                "is_blocked": state.is_blocked,
            }
        return summary

    def reset(self):
        """重置所有状态 (测试用)"""
        self._stock_states.clear()
        self._global_loss_streak = 0
        self._is_paused = False
        self._pause_reason = ""
