"""
app/risk/hard_stop_guard.py — 硬编码止损兜底机制 (2026-05-01)

职责:
  作为DynamicExitManager的最后防线，当主止损系统异常时提供硬编码保护。

设计原则:
  1. 绝对独立: 不依赖任何外部配置、数据库、或DynamicExitManager
  2. 纯硬编码: 使用固定的止损百分比，确保即使配置加载失败也能工作
  3. 最低优先级: 只在主止损系统失效时触发

触发条件:
  - DynamicExitManager检查异常或超时
  - 持仓亏损超过硬编码止损线（默认8%）
  - 持仓时间超过最大容忍时间（默认72小时）

硬编码参数 (不可通过配置修改):
  HARD_STOP_LOSS_PCT = 0.08      # 8% 硬编码止损
  HARD_MAX_HOLDING_HOURS = 72    # 72小时最大持仓
  HARD_DAILY_LOSS_PCT = 0.03     # 3% 日亏损限制
"""
import logging
import time
from typing import Optional
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.risk.hard_stop_guard")

# ═══════════════════════════════════════════════════════════════
#  硬编码参数 — 不可通过配置修改，代码级保护
# ═══════════════════════════════════════════════════════════════

# 最大单笔亏损百分比（8%）
HARD_STOP_LOSS_PCT = 0.08

# 最大持仓时长（72小时 = 3个交易日）
HARD_MAX_HOLDING_HOURS = 72

# 单日最大亏损百分比（3%）
HARD_DAILY_LOSS_PCT = 0.03

# 连续亏损次数限制（3次）
HARD_MAX_CONSECUTIVE_LOSSES = 3


class HardStopGuard:
    """
    硬编码止损兜底守护

    用法:
        guard = HardStopGuard()
        decision = guard.check_hard_stop(
            code="HK.00700",
            entry_price=100.0,
            current_price=92.0,  # 亏损8%
            entry_time="2026-04-30 09:30:00",
            daily_pnl=-0.035     # 当日已亏损3.5%
        )
        if decision.should_exit:
            print(f"硬编码止损触发: {decision.reason}")
    """

    def __init__(self):
        self._consecutive_losses: dict = {}  # code → 连续亏损次数
        self._daily_loss: dict = {}          # date → 累计亏损

    def check_hard_stop(
        self,
        code: str,
        entry_price: float,
        current_price: float,
        entry_time,
        daily_pnl: float = 0.0,
    ) -> Optional['HardStopDecision']:
        """
        检查是否触发硬编码止损。

        Args:
            code: 股票代码
            entry_price: 入场价格
            current_price: 当前价格
            entry_time: 入场时间
            daily_pnl: 当日累计盈亏百分比

        Returns:
            HardStopDecision 如果触发，否则 None
        """
        if entry_price <= 0 or current_price <= 0:
            return None

        # 计算亏损百分比
        pnl_pct = (current_price - entry_price) / entry_price

        # ── 检查1: 单笔亏损超过8% ──
        if pnl_pct <= -HARD_STOP_LOSS_PCT:
            logger.critical(
                f"[硬编码止损] {code} 亏损 {abs(pnl_pct):.1%} >= {HARD_STOP_LOSS_PCT:.0%}，"
                f"强制平仓 (entry={entry_price}, current={current_price})"
            )
            self._record_loss(code)
            return HardStopDecision(
                should_exit=True,
                reason=f"硬编码止损: 亏损{abs(pnl_pct):.1%} >= {HARD_STOP_LOSS_PCT:.0%}",
                priority=0,  # 最高优先级
                close_ratio=1.0,
                trigger_type="hard_stop_loss",
            )

        # ── 检查2: 持仓时间超过72小时 ──
        if entry_time:
            try:
                if isinstance(entry_time, str):
                    et = datetime.fromisoformat(entry_time.replace(' ', 'T'))
                else:
                    et = entry_time
                holding_hours = (datetime.now() - et).total_seconds() / 3600
                if holding_hours >= HARD_MAX_HOLDING_HOURS:
                    logger.critical(
                        f"[硬编码止损] {code} 持仓 {holding_hours:.1f}h >= {HARD_MAX_HOLDING_HOURS}h，"
                        f"强制平仓"
                    )
                    return HardStopDecision(
                        should_exit=True,
                        reason=f"硬编码时间止损: 持仓{holding_hours:.1f}h >= {HARD_MAX_HOLDING_HOURS}h",
                        priority=0,
                        close_ratio=1.0,
                        trigger_type="hard_time_stop",
                    )
            except Exception as e:
                logger.debug(f"[硬编码止损] 解析entry_time失败: {e}")

        # ── 检查3: 当日亏损超过3% ──
        if daily_pnl <= -HARD_DAILY_LOSS_PCT:
            logger.critical(
                f"[硬编码止损] {code} 当日亏损 {abs(daily_pnl):.1%} >= {HARD_DAILY_LOSS_PCT:.0%}，"
                f"强制平仓"
            )
            self._record_loss(code)
            return HardStopDecision(
                should_exit=True,
                reason=f"硬编码日亏损止损: 当日亏损{abs(daily_pnl):.1%} >= {HARD_DAILY_LOSS_PCT:.0%}",
                priority=0,
                close_ratio=1.0,
                trigger_type="hard_daily_loss",
            )

        return None

    def _record_loss(self, code: str):
        """记录亏损次数"""
        count = self._consecutive_losses.get(code, 0) + 1
        self._consecutive_losses[code] = count
        if count >= HARD_MAX_CONSECUTIVE_LOSSES:
            logger.critical(
                f"[硬编码止损] {code} 连续亏损 {count} 次 >= {HARD_MAX_CONSECUTIVE_LOSSES} 次，"
                f"建议暂停交易"
            )

    def reset(self, code: str):
        """重置某股票的亏损记录"""
        self._consecutive_losses.pop(code, None)

    def should_halt_trading(self, code: str) -> bool:
        """判断是否应该暂停该股票的交易"""
        return self._consecutive_losses.get(code, 0) >= HARD_MAX_CONSECUTIVE_LOSSES


class HardStopDecision:
    """硬编码止损决策结果"""
    def __init__(self, should_exit: bool, reason: str, priority: int,
                 close_ratio: float, trigger_type: str):
        self.should_exit = should_exit
        self.reason = reason
        self.priority = priority
        self.close_ratio = close_ratio
        self.trigger_type = trigger_type


# 全局单例
hard_stop_guard = HardStopGuard()
