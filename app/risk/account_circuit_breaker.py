"""
账户级熔断器 (Account Circuit Breaker)

2026-06-25: 为 OKR 目标新增。基于 trade_records 实时计算当日已实现盈亏和回撤，
触发阈值时采取递减式风控：
  - 单日亏损 ≥1%: 减仓 50%
  - 单日亏损 ≥2%: 停止新开仓
  - 单日亏损 ≥3% 或 最大回撤 ≥3%: LIQUIDATE 档

2026-08-28 声明纠偏 (行为零改动): LIQUIDATE 档自建立起从未接线强制平仓 —
四个消费方 (order_executor/playbook_service/orb.engine/本文件用法示例) 全部
`action in ("HALT", "LIQUIDATE")` 仅拒新仓, 与 ≥2% 档实际同效; 存量仓的平仓
始终由各自 SL/trailing/EOD 退出规则执行。"文档宣称有防线而实际没有" 与
DEB-20260823 daemon_heartbeat 死表同族。是否真接线账户级全平属人工风险裁决
(等于给机器一键清仓的权限), 本文件只把声明改真。

使用:
  from app.risk.account_circuit_breaker import get_account_circuit_breaker, CircuitAction
  breaker = get_account_circuit_breaker()
  result = breaker.check(equity)
  if result.action in ("HALT", "LIQUIDATE"):
      # 拒绝开仓 (LIQUIDATE 亦仅此效果, 不平存量)
"""
# ── 日亏损防线登记 (2026-08-28 冲突显性化): "当日亏损"事实六套权威并存 ──
#   1. AccountCircuitBreaker  app/risk/account_circuit_breaker.py
#      净值 -1%/-2%/-3% 三档 (减半/停新仓/停新仓 — 全平从未接线)
#   2. AbsoluteCircuitBreaker app/risk/absolute_circuit_breaker.py
#      -$2000/日 + -$1000/时 + 连亏5笔 (类常量, 不受开关控制)
#   3. symbol_day_loss        app/risk/symbol_day_loss.py
#      -$500/标的/日 (出场日+净额, 主线/playbook 共用)
#   4. SymbolDailyLossGuard   app/risk/symbol_daily_loss_guard.py
#      净值0.5%/标的/日 + 连亏N笔 + 低胜率 (yaml risk.symbol_daily_loss_guard.*)
#   5. RiskCircuitBreaker     app/playbook/risk_circuit_breaker.py
#      总资金1% + 连亏3减半/连亏5停1日 (playbook 域; 周报告警组件 — "禁执行按钮+飞书"
#      从未接线, 全仓唯一调用方=周五周报 logger.warning, 仅记录不阻断; 2026-08-30 DEB-timezone 第4轮)
#   6. DrawdownController     app/risk/drawdown_control.py
#      日3% (risk.account.max_daily_drawdown, 持久化防重启归零; 仅主线平仓在喂)
# 六套独立查询/独立状态互不感知; 口径 % 与 $ 混杂, 触发先后交错, 合并/裁剪属
# 人工风险裁决 (每套有各自历史语义与恢复机制)。本登记保证读任一防线时看得见
# 全景; 已统一项: 1/3 出场日+净额 (2026-08-27/28), 其余各口径见各自头注。
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from app.core.paths import get_trade_records_db_for_arm
from app.utils.timezone import _et_trade_date, now_et

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

    def check(self, current_equity: float, target_env: str = "sim") -> CircuitResult:
        """
        检查账户风控状态。

            Args:
            current_equity: 当前账户权益
            target_env: sim/real；决定读取哪条台账和资金峰值

        Returns:
            CircuitResult
        """
        if current_equity <= 0:
            return CircuitResult(
                action="LIQUIDATE",
                reason="当前权益 <= 0 (LIQUIDATE 档, 同 HALT 仅拒新仓)",
                daily_pnl_pct=0.0,
                peak_equity=0.0,
                current_equity=current_equity,
            )

        today = _et_trade_date(now_et())
        daily_pnl = self._get_daily_pnl(today, target_env)
        # 2026-06-26: peak_equity 不用 trade_records 的 max(notional) — ETF 大批量交易
        # (如 XLF 22404股=$1.14M) 会产生虚假 peak, 导致回撤 91% 误判熔断.
        # 改用 capital_safety 的 peak_equity (真实账户历史峰值).
        try:
            if str(target_env).lower() in {"real", "live"}:
                from app.risk.capital_safety import get_capital_safety_system_for_arm
                peak_equity = get_capital_safety_system_for_arm(target_env)._state.peak_equity
            else:
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

    def _get_daily_pnl(self, today: str, target_env: str = "sim") -> float:
        """当日已实现净盈亏 (2026-08-27 口径修复): 按出场日 exit_time 归集,
        净额 pnl_net 优先 (费用可见), 旧行 NULL 回退毛额 realized_pnl.

        原实现 WHERE trade_date = today (入场日) — 隔夜仓按入场日记账,
        开盘跳空亏损在熔断口径中消失 (8/27 实证: UBER 净 -$307 记 8/26,
        老口径报日盈亏 +0.18% 而净值口径 -0.18%, -1%/-2% 减仓/停新仓
        两档在隔夜跳空日全失效).

        归集前提 (2026-08-28 实测 723 平仓行, 并非"全库一致"): 394 行 ET-aware
        (-04:00) / 327 行 naive 无偏移 (全部 ≤2026-07-20 遗留) / 1 行 +00:00
        (id=942 US.MSFT 2026-06-25)。本门只查"当日", 现役窗口 (07-21 起) 全
        ET-aware → substr(exit_time,1,10)=出场 ET 日成立; 回溯到 07-20 以前的
        全期聚合不可照搬本前提, 须先归一 naive 行。

        fail-close: 查询异常返回 -inf (无法确认状态 → 按最坏处理).
        """
        try:
            db_path = str(get_trade_records_db_for_arm(target_env))
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) "
                "FROM trade_records "
                "WHERE substr(exit_time, 1, 10) = ? AND exit_price > 0",
                (today,),
            ).fetchone()
            conn.close()
            return float(row[0]) if row and row[0] else 0.0
        except Exception as e:
            logger.error("[AccountCircuitBreaker] DB 查询失败 (fail-close): %s", e)
            return -float('inf')


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


def check_circuit_breaker(equity: float, target_env: str = "sim") -> CircuitResult:
    """便捷函数"""
    return get_account_circuit_breaker().check(equity, target_env=target_env)
