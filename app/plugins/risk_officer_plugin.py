#!/usr/bin/env python3
"""
FTSeek 自适应风控插件 (2026-05-05)

事件驱动风控 — 订阅 trade.closed 事件，检测连续止损并自动收紧风控参数。

触发条件:
  - 单标的连续止损 >= 3 次 → 自动收紧该标的止损比例 (乘以 0.75)
  - 单标的连续止损 >= 5 次 → 建议暂停该标的当日交易
  - 全局 (所有标的) 当日止损 >= 10 次 → 收紧全局止损比例
  - 每个调整均发射 system.risk_adjusted 事件，供 ObsidianPlugin 等下游消费

设计原则:
  - 纯插件 — 核心管线完全不知道此插件的存在
  - 即发即忘 — 任何处理异常不影响主线
  - 每日重置 — 开盘时自动清零计数器
"""

import logging
from datetime import date as _date
from typing import Any, Dict, List, Optional, Set

from app.core.domain_events import (
    DomainEvent,
    DomainEventBus,
    EventType,
    RiskAdjustedPayload,
    new_event,
    get_event_bus,
)

logger = logging.getLogger("ftseek.plugins.risk_officer")

# ── 可配置常量 ──────────────────────────────────────────────────────

DEFAULT_TIGHTEN_MULTIPLIER = 0.75
CONSECUTIVE_THRESHOLD_TIGHTEN = 3
CONSECUTIVE_THRESHOLD_PAUSE = 5
GLOBAL_DAILY_STOP_THRESHOLD = 10


class RiskOfficerPlugin:
    """自适应风控插件 — 事件驱动，零侵入

    订阅 trade.closed 事件，追踪每个标的的连续止损次数。
    达标时自动收紧风控参数，并通过 system.risk_adjusted 事件通知下游。

    使用示例:
        from app.plugins.risk_officer_plugin import RiskOfficerPlugin
        from app.core.domain_events import get_event_bus

        officer = RiskOfficerPlugin(enabled=True)
        officer.register(get_event_bus())
        ...
        status = officer.get_status()
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

        # ── 每标的追踪 ──
        self._consecutive_stops: Dict[str, int] = {}  # code -> 连续止损次数

        # ── 全局追踪 ──
        self._daily_total_stops: int = 0

        # ── 调整历史 ──
        self._adjustments: List[Dict[str, Any]] = []

        # ── 暂停清单 (连续 >= 5 次) ──
        self._paused_codes: Set[str] = set()

        # ── 全局收紧标记 ──
        self._global_tightened: bool = False

        # ── 每日重置 ──
        self._last_date: Optional[str] = None

        # ── 当前止损值缓存 (从 dynamic_exit_manager 读取) ──
        # stop_loss 值在 config 中为负数 (e.g. -0.035)。
        # 乘以 0.75 使绝对值变小，从而实现"收紧"。
        self._current_global_stop_loss: Optional[float] = None

        logger.info(
            "[RiskOfficerPlugin] 初始化完成 enabled=%s tighten_mult=%.2f "
            "tighten_threshold=%d pause_threshold=%d global_threshold=%d",
            enabled,
            DEFAULT_TIGHTEN_MULTIPLIER,
            CONSECUTIVE_THRESHOLD_TIGHTEN,
            CONSECUTIVE_THRESHOLD_PAUSE,
            GLOBAL_DAILY_STOP_THRESHOLD,
        )

    # ═══════════════════════════════════════════════════════════════
    #  注册 / 注销
    # ═══════════════════════════════════════════════════════════════

    def register(self, bus: DomainEventBus) -> None:
        """注册到领域事件总线。

        匹配 obsidian_plugin.py 的 register(bus) 模式:
          bus.subscribe(EventType.TRADE_CLOSED, self._on_trade_closed)
        """
        if not self.enabled:
            logger.info("[RiskOfficerPlugin] 已禁用，跳过注册")
            return

        bus.subscribe(EventType.TRADE_CLOSED, self._on_trade_closed)
        # 首次注册时对齐日期
        self._daily_reset_if_needed()
        logger.info("[RiskOfficerPlugin] 已注册 1 个事件监听器 (trade.closed)")

    def unregister(self, bus: DomainEventBus) -> None:
        """从事件总线注销。"""
        bus.unsubscribe(EventType.TRADE_CLOSED, self._on_trade_closed)
        logger.info("[RiskOfficerPlugin] 已注销")

    # ═══════════════════════════════════════════════════════════════
    #  每日重置
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _today_str() -> str:
        """返回当前日期字符串 'YYYY-MM-DD'"""
        return str(_date.today())

    def _daily_reset_if_needed(self) -> None:
        """如果日期已变更，重置所有每日计数器。"""
        today = self._today_str()
        if self._last_date is not None and self._last_date != today:
            logger.info(
                "[RiskOfficerPlugin] 新交易日 %s (上次 %s)，重置所有每日计数器 "
                "昨日: total_stops=%d adjustments=%d paused_codes=%s global_tightened=%s",
                today,
                self._last_date,
                self._daily_total_stops,
                len(self._adjustments),
                sorted(self._paused_codes),
                self._global_tightened,
            )

            self._consecutive_stops.clear()
            self._daily_total_stops = 0
            self._paused_codes.clear()
            self._global_tightened = False
            self._invalidate_stop_loss_cache()

        self._last_date = today

    def force_daily_reset(self) -> None:
        """手动触发每日重置 (诊断/测试用)。"""
        logger.info("[RiskOfficerPlugin] 手动触发每日重置")
        self._last_date = None
        self._daily_reset_if_needed()

    # ═══════════════════════════════════════════════════════════════
    #  事件处理: trade.closed
    # ═══════════════════════════════════════════════════════════════

    def _on_trade_closed(self, event: DomainEvent) -> None:
        """处理 trade.closed 领域事件。

        仅处理 exit_reason == 'stop_loss' 的平仓，递增该标的和全局止损计数。
        正常平仓或止盈时，重置该标的的连续止损计数。

        Args:
            event: DomainEvent with data containing TradeClosedPayload fields.
        """
        self._daily_reset_if_needed()

        data = event.data
        # data 已归一化为 dict — 用 .get() 访问
        exit_reason = data.get("exit_reason", "")
        code = data.get("code", "")

        if not code:
            logger.warning("[RiskOfficerPlugin] trade.closed 事件缺少 code 字段，跳过")
            return

        # 已暂停的标的仍然追踪事件，但不再触发新的调整
        if code in self._paused_codes:
            logger.debug("[RiskOfficerPlugin] 标的 %s 已暂停当日交易，跳过调整", code)
            if exit_reason == "stop_loss":
                self._daily_total_stops += 1
                # 仍然检查全局阈值
                if self._daily_total_stops >= GLOBAL_DAILY_STOP_THRESHOLD and not self._global_tightened:
                    self._tighten_global_stop_loss()
            return

        if exit_reason == "stop_loss":
            self._handle_stop_loss(code, data)
        else:
            self._handle_normal_close(code, exit_reason)

    # ═══════════════════════════════════════════════════════════════
    #  止损 / 正常平仓处理
    # ═══════════════════════════════════════════════════════════════

    def _handle_stop_loss(self, code: str, data: Dict[str, Any]) -> None:
        """处理一笔止损平仓。

        1. 递增标的的连续止损计数
        2. 递增全局止损计数
        3. 检查各阈值并触发相应调整
        """
        old_count = self._consecutive_stops.get(code, 0)
        new_count = old_count + 1
        self._consecutive_stops[code] = new_count
        self._daily_total_stops += 1

        logger.warning(
            "[RiskOfficerPlugin] %s 止损 #%d 连续=%d→%d 全局=%d/%d",
            code,
            self._daily_total_stops,
            old_count,
            new_count,
            self._daily_total_stops,
            GLOBAL_DAILY_STOP_THRESHOLD,
        )

        # ── 检查全局阈值 ──
        if self._daily_total_stops >= GLOBAL_DAILY_STOP_THRESHOLD and not self._global_tightened:
            self._tighten_global_stop_loss()

        # ── 检查单标的阈值 ──
        if new_count >= CONSECUTIVE_THRESHOLD_PAUSE and code not in self._paused_codes:
            self._pause_code(code, new_count)
        elif new_count >= CONSECUTIVE_THRESHOLD_TIGHTEN:
            self._tighten_code_stop_loss(code, new_count)

    def _handle_normal_close(self, code: str, exit_reason: str) -> None:
        """正常平仓或止盈 — 重置该标的连续止损计数。

        一次非止损平仓就足以打破"连续"链条，因此直接清零该标的计数器。
        """
        old_count = self._consecutive_stops.pop(code, None)
        if old_count is not None:
            logger.info(
                "[RiskOfficerPlugin] %s 非止损平仓 (reason=%s)，连续止损已重置 (was %d)",
                code,
                exit_reason,
                old_count,
            )

    # ═══════════════════════════════════════════════════════════════
    #  止损收紧逻辑
    # ═══════════════════════════════════════════════════════════════

    def _get_current_stop_loss(self) -> float:
        """从 dynamic_exit_manager 读取当前全局止损比例。

        结果会被缓存。调用 _invalidate_stop_loss_cache() 可在调整后刷新。

        Returns:
            负值 (e.g. -0.035 表示 3.5% 止损)。
        """
        if self._current_global_stop_loss is not None:
            return self._current_global_stop_loss

        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager
            val = dynamic_exit_manager.get_stop_loss()
            self._current_global_stop_loss = float(val)
            return self._current_global_stop_loss
        except Exception as e:
            logger.warning(
                "[RiskOfficerPlugin] 读取 dynamic_exit_manager 止损参数失败: %s，"
                "使用默认 -0.025", e
            )
            self._current_global_stop_loss = -0.025
            return self._current_global_stop_loss

    def _invalidate_stop_loss_cache(self) -> None:
        """使止损缓存失效。调整参数后或每日重置时调用。"""
        self._current_global_stop_loss = None

    def _tighten_code_stop_loss(self, code: str, consecutive_count: int) -> None:
        """收紧某个标的的止损比例。

        new_stop_loss = old_stop_loss * DEFAULT_TIGHTEN_MULTIPLIER (0.75)

        注意: old_stop_loss 为负值。乘以 0.75 使绝对值变小。
        例如: -0.02 * 0.75 = -0.015 (从 2% 止损收紧至 1.5%)。
        """
        old_sl = self._get_current_stop_loss()
        new_sl = old_sl * DEFAULT_TIGHTEN_MULTIPLIER

        reason = (
            f"{code} 连续止损 {consecutive_count} 次 "
            f"(阈值={CONSECUTIVE_THRESHOLD_TIGHTEN})，自动收紧止损"
        )
        adjustment = (
            f"止损从 {abs(old_sl):.2%} 收紧至 {abs(new_sl):.2%} "
            f"(乘数 {DEFAULT_TIGHTEN_MULTIPLIER})"
        )

        self._record_adjustment(reason, adjustment, old_sl, new_sl, consecutive_count, code)
        self._emit_risk_adjusted(reason, adjustment, old_sl, new_sl, consecutive_count, code)

        logger.warning(
            "[RiskOfficerPlugin] 收紧 %s 止损: %.2f%% → %.2f%% (连续 %d 次)",
            code,
            abs(old_sl) * 100,
            abs(new_sl) * 100,
            consecutive_count,
        )

    def _tighten_global_stop_loss(self) -> None:
        """全局收紧止损 — 当日所有标的总止损次数超过阈值时触发。"""
        old_sl = self._get_current_stop_loss()
        new_sl = old_sl * DEFAULT_TIGHTEN_MULTIPLIER

        reason = (
            f"全局当日止损 {self._daily_total_stops} 次 "
            f"(阈值={GLOBAL_DAILY_STOP_THRESHOLD})，收紧全局止损"
        )
        adjustment = (
            f"全局止损从 {abs(old_sl):.2%} 收紧至 {abs(new_sl):.2%} "
            f"(乘数 {DEFAULT_TIGHTEN_MULTIPLIER})"
        )

        self._global_tightened = True

        self._record_adjustment(reason, adjustment, old_sl, new_sl, self._daily_total_stops, "")
        self._emit_risk_adjusted(reason, adjustment, old_sl, new_sl, self._daily_total_stops, "")

        logger.warning(
            "[RiskOfficerPlugin] 全局止损收紧: %.2f%% → %.2f%% (日止损 %d 次)",
            abs(old_sl) * 100,
            abs(new_sl) * 100,
            self._daily_total_stops,
        )

    def _pause_code(self, code: str, consecutive_count: int) -> None:
        """暂停某个标的的当日交易。

        达标条件: 连续止损 >= CONSECUTIVE_THRESHOLD_PAUSE (5 次)
        pause 不改变止损值，但通过 system.risk_adjusted 事件通知下游暂停该标的。
        """
        self._paused_codes.add(code)

        old_sl = self._get_current_stop_loss()

        reason = (
            f"{code} 连续止损 {consecutive_count} 次 "
            f"(阈值={CONSECUTIVE_THRESHOLD_PAUSE})，暂停当日交易"
        )
        adjustment = f"暂停 {code} 当日交易，不再开仓"

        self._record_adjustment(reason, adjustment, old_sl, old_sl, consecutive_count, code)
        self._emit_risk_adjusted(reason, adjustment, old_sl, old_sl, consecutive_count, code)

        logger.warning(
            "[RiskOfficerPlugin] 暂停 %s 当日交易 (连续 %d 次止损)",
            code,
            consecutive_count,
        )

    # ═══════════════════════════════════════════════════════════════
    #  事件发射与历史记录
    # ═══════════════════════════════════════════════════════════════

    def _emit_risk_adjusted(
        self,
        reason: str,
        adjustment: str,
        old_value: float,
        new_value: float,
        consecutive_stops: int,
        affected_code: str,
    ) -> None:
        """发送 system.risk_adjusted 领域事件。

        每个调整都发射一个事件，供 ObsidianPlugin 等下游消费者使用。
        """
        try:
            bus = get_event_bus()
            payload = RiskAdjustedPayload(
                reason=reason,
                adjustment=adjustment,
                old_value=old_value,
                new_value=new_value,
                consecutive_stops=consecutive_stops,
                affected_code=affected_code,
            )
            event = new_event(EventType.SYSTEM_RISK_ADJUSTED, payload, source="risk_officer_plugin")
            bus.emit(event)
            logger.info(
                "[RiskOfficerPlugin] system.risk_adjusted 已发射: reason=%s old=%.4f new=%.4f code=%s",
                reason,
                old_value,
                new_value,
                affected_code,
            )
        except Exception as e:
            logger.warning(
                "[RiskOfficerPlugin] 发射 system.risk_adjusted 事件失败: %s", e,
                exc_info=True,
            )

    def _record_adjustment(
        self,
        reason: str,
        adjustment: str,
        old_value: float,
        new_value: float,
        consecutive_stops: int,
        affected_code: str,
    ) -> None:
        """记录一次调整到历史列表。"""
        record: Dict[str, Any] = {
            "timestamp": self._today_str(),
            "reason": reason,
            "adjustment": adjustment,
            "old_value": old_value,
            "new_value": new_value,
            "consecutive_stops": consecutive_stops,
            "affected_code": affected_code,
        }
        self._adjustments.append(record)

    # ═══════════════════════════════════════════════════════════════
    #  状态查询
    # ═══════════════════════════════════════════════════════════════

    def get_status(self) -> Dict[str, Any]:
        """返回当前状态快照。

        供外部查询和诊断面板使用。

        Returns:
            {
                "enabled": bool,
                "date": str (YYYY-MM-DD),
                "consecutive_stops": {code: count, ...},
                "daily_total_stops": int,
                "paused_codes": [str, ...],
                "global_tightened": bool,
                "current_stop_loss": float (负值),
                "adjustments_count": int,
                "recent_adjustments": [dict, ...] (最近 10 条),
            }
        """
        self._daily_reset_if_needed()
        recent = self._adjustments[-10:] if self._adjustments else []
        return {
            "enabled": self.enabled,
            "date": self._last_date or self._today_str(),
            "consecutive_stops": dict(self._consecutive_stops),
            "daily_total_stops": self._daily_total_stops,
            "paused_codes": sorted(self._paused_codes),
            "global_tightened": self._global_tightened,
            "current_stop_loss": self._get_current_stop_loss(),
            "adjustments_count": len(self._adjustments),
            "recent_adjustments": recent,
        }

    def is_code_paused(self, code: str) -> bool:
        """检查某个标的是否已被暂停今日交易。

        Args:
            code: 标的代码 (e.g. 'HK.00700' 或 'US.AAPL')

        Returns:
            bool: 是否已暂停
        """
        self._daily_reset_if_needed()
        return code in self._paused_codes

    def get_paused_codes(self) -> List[str]:
        """返回当日所有被暂停的标的列表。"""
        self._daily_reset_if_needed()
        return sorted(self._paused_codes)

    # ═══════════════════════════════════════════════════════════════
    #  诊断 / 重置
    # ═══════════════════════════════════════════════════════════════

    def reset_all(self) -> None:
        """完全重置所有状态 (诊断/测试用)。"""
        self._consecutive_stops.clear()
        self._daily_total_stops = 0
        self._adjustments.clear()
        self._paused_codes.clear()
        self._global_tightened = False
        self._last_date = None
        self._current_global_stop_loss = None
        logger.info("[RiskOfficerPlugin] 完全重置所有状态")
