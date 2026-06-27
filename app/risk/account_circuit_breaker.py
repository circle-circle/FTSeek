"""
账户级熔断器 (Account Circuit Breaker)

2026-06-25: 为 OKR 目标新增。基于 trade_records 实时计算当日已实现盈亏和回撤，
触发阈值时采取递减式风控：
  - 单日亏损 ≥1%: 减仓 50%
  - 单日亏损 ≥2%: 停止新开仓
  - 单日亏损 ≥3% 或 最大回撤 ≥3%: 强制全平

使用:
  from app.risk.account_circuit_breaker import get_account_circuit_breaker, CircuitAction
  breaker = get_account_circuit_breaker()
  result = breaker.check(equity)
  if result.action in ("HALT", "LIQUIDATE"):
      # 拒绝开仓
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from app.core.paths import get_trade_records_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircuitResult:
    action: str  # NORMAL / REDUCE_50 / HALT / LIQUIDATE
    reason: str
    daily_pnl_pct: float
    peak_equity: float
    current_equity: float


class AccountCircuitBreaker:
    """账户级熔断器"""

    def __init__(
        self,
        reduce_threshold: float = 0.01,
        halt_threshold: float = 0.02,
        liquidate_threshold: float = 0.03,
    ):
        self.reduce_threshold = reduce_threshold
        self.halt_threshold = halt_threshold
        self.liquidate_threshold = liquidate_threshold

        # 内存缓存, 避免频繁查询 DB
        self._cache: Dict[str, any] = {}
        self._cache_ttl_seconds = 10

    def check(self, current_equity: float) -> CircuitResult:
        """
        检查账户风控状态。

        Args:
            current_equity: 当前账户权益

        Returns:
            CircuitResult
        """
        if current_equity <= 0:
            return CircuitResult(
                action="LIQUIDATE",
                reason="当前权益 <= 0, 强制全平",
                daily_pnl_pct=0.0,
                peak_equity=0.0,
                current_equity=current_equity,
            )

        today = date.today().isoformat()
        daily_pnl, _ = self._get_daily_pnl_and_peak(today, current_equity)
        # 2026-06-26: peak_equity 不用 trade_records 的 max(notional) — ETF 大批量交易
        # (如 XLF 22404股=$1.14M) 会产生虚假 peak, 导致回撤 91% 误判熔断.
        # 改用 capital_safety 的 peak_equity (真实账户历史峰值).
        try:
            from app.risk.capital_safety import get_capital_safety_system
            peak_equity = get_capital_safety_system()._state.peak_equity
            if peak_equity <= 0:
                peak_equity = current_equity
        except Exception:
            peak_equity = current_equity
        daily_pnl_pct = daily_pnl / current_equity if current_equity else 0.0
        dd_pct = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0.0

        # 优先判断 liquidation (最严格)
        if daily_pnl_pct <= -self.liquidate_threshold or dd_pct >= self.liquidate_threshold:
            action = "LIQUIDATE"
            reason = (
                f"账户熔断触发: 日亏损={daily_pnl_pct:.2%}, "
                f"回撤={dd_pct:.2%} ≥ {self.liquidate_threshold:.0%}"
            )
        elif daily_pnl_pct <= -self.halt_threshold:
            action = "HALT"
            reason = (
                f"停止新仓: 日亏损={daily_pnl_pct:.2%} ≥ {self.halt_threshold:.0%}"
            )
        elif daily_pnl_pct <= -self.reduce_threshold:
            action = "REDUCE_50"
            reason = (
                f"减仓 50%: 日亏损={daily_pnl_pct:.2%} ≥ {self.reduce_threshold:.0%}"
            )
        else:
            action = "NORMAL"
            reason = (
                f"账户正常: 日盈亏={daily_pnl_pct:.2%}, 回撤={dd_pct:.2%}"
            )

        logger.info("[AccountCircuitBreaker] %s | equity=%.0f peak=%.0f daily_pnl=%.2f%%",
                    reason, current_equity, peak_equity, daily_pnl_pct * 100)

        return CircuitResult(
            action=action,
            reason=reason,
            daily_pnl_pct=daily_pnl_pct,
            peak_equity=peak_equity,
            current_equity=current_equity,
        )

    def _get_daily_pnl_and_peak(self, today: str, current_equity: float) -> tuple:
        """从 trade_records 读取今日已实现盈亏和峰值权益"""
        try:
            db_path = str(get_trade_records_db())
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # 今日已实现盈亏 (已平仓)
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_records "
                "WHERE trade_date = ? AND exit_price > 0",
                (today,),
            ).fetchone()
            daily_pnl = float(row[0]) if row and row[0] else 0.0

            # 历史峰值权益: 用 current_equity - 历史最大回撤近似
            # 简化: 取最近 30 天 trade_records 中最大的 (entry_price * quantity + cash)
            # 更精确的做法是维护 equity 时间序列, 这里先用 current_equity + |daily_pnl| 兜底
            row = c.execute(
                "SELECT MAX(entry_price * quantity) FROM trade_records "
                "WHERE trade_date <= ?",
                (today,),
            ).fetchone()
            max_notional = float(row[0]) if row and row[0] else 0.0
            peak_equity = max(current_equity, max_notional)

            conn.close()
            return daily_pnl, peak_equity
        except Exception as e:
            logger.error("[AccountCircuitBreaker] DB 查询失败 (fail-close): %s", e)
            # fail-close: 无法确认状态时假设亏损最大
            return -float('inf'), current_equity


# 模块级单例
_circuit_breaker_instance: Optional[AccountCircuitBreaker] = None


def get_account_circuit_breaker(
    reduce_threshold: float = 0.01,
    halt_threshold: float = 0.02,
    liquidate_threshold: float = 0.03,
) -> AccountCircuitBreaker:
    """获取 AccountCircuitBreaker 单例"""
    global _circuit_breaker_instance
    if _circuit_breaker_instance is None:
        _circuit_breaker_instance = AccountCircuitBreaker(
            reduce_threshold=reduce_threshold,
            halt_threshold=halt_threshold,
            liquidate_threshold=liquidate_threshold,
        )
    return _circuit_breaker_instance


def check_circuit_breaker(equity: float) -> CircuitResult:
    """便捷函数"""
    return get_account_circuit_breaker().check(equity)
