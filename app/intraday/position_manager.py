"""
持仓管理器 — 委托 DynamicExitManager 进行所有离场决策

职责：
1. 组合式离场检查 (委托 DynamicExitManager.check_exit_priority)
2. 退出状态机追踪 (INIT → BE_ACTIVE → TRAIL → PARTIAL → EXIT)
3. 峰值价格追踪 + 止损/止盈执行
"""

import logging
import time
import threading
from typing import Optional, Dict, Any
from datetime import datetime

from app.core.config_loader import cfg
from app.core.trading_env import get_trading_env
from app.risk.dynamic_exit_manager import (
    DynamicExitManager, dynamic_exit_manager, ExitState, ExitDecision, StopLossType,
)

logger = logging.getLogger("ftseek.intraday.position_manager")


class IntradayPositionManager:
    """盘中持仓管理器 — 所有离场决策委托给 DynamicExitManager"""

    def __init__(self, position_manager, trade_executor, notifier=None):
        self._pm = position_manager
        self._te = trade_executor
        self._notifier = notifier
        self._dem: DynamicExitManager = dynamic_exit_manager

        # 每标的状态追踪
        self._exit_state: Dict[str, str] = {}        # code → ExitState
        self._peak_price: Dict[str, float] = {}       # code → peak_price
        self._entry_time: Dict[str, Any] = {}         # code → entry_time

        # 止损冷却 + 坏订单
        self._stop_loss_cooldown: Dict[str, float] = {}
        self._stop_loss_fail_count: Dict[str, int] = {}
        self._bad_orders: Dict[str, Dict] = {}
        self._max_cache_size = 200

        self._lock = threading.Lock()

        # P0 修复 2026-04-30: 从 DB 恢复 OPEN 持仓的状态机状态
        # 防止重启后 exit_state/peak_price 丢失导致止损失效
        self._restore_state_from_db()

        # 回调
        self._on_stop_loss_executed = None
        self._on_take_profit_executed = None

    def set_callbacks(self, on_stop_loss=None, on_take_profit=None):
        self._on_stop_loss_executed = on_stop_loss
        self._on_take_profit_executed = on_take_profit

    def get_open_positions_count(self) -> int:
        now = time.time()
        if not hasattr(self, '_pos_count_cache'):
            self._pos_count_cache = (0, 0.0)
        cached_count, cached_time = self._pos_count_cache
        if now - cached_time < 5.0:
            return cached_count
        try:
            count = self._pm.get_open_positions_count()
        except Exception:
            count = 0
        self._pos_count_cache = (count, now)
        return count

    def _restore_state_from_db(self):
        """
        P0 修复 2026-04-30: 从 DB 恢复 OPEN 持仓的状态机状态

        解决重启后 _exit_state 全为 INIT、_peak_price 全部丢失的问题。
        """
        try:
            positions = self._pm.get_all_positions()
            if not positions:
                logger.info("[状态恢复] 无 OPEN 持仓, 跳过恢复")
                return

            for pos in positions:
                code = getattr(pos, 'code', None) or getattr(pos, 'code', None)
                if not code:
                    continue

                # 恢复 exit_state (从 DB 读取, 若为空则用 INIT)
                exit_state = getattr(pos, 'exit_state', None)
                if exit_state:
                    self._exit_state[code] = exit_state
                else:
                    self._exit_state[code] = ExitState.INITIAL

                # 恢复 entry_time
                entry_time = getattr(pos, 'entry_time', None)
                if entry_time:
                    self._entry_time[code] = entry_time

                # 峰值价格: 取 highest_price (DB) 或 entry_price 的较大值
                highest = getattr(pos, 'highest_price', None) or 0
                entry = getattr(pos, 'entry_price', 0)
                self._peak_price[code] = max(float(highest), float(entry)) if entry > 0 else entry

                logger.info(
                    f"[状态恢复] {code}: state={self._exit_state[code]}, "
                    f"peak={self._peak_price[code]}, entry_time={self._entry_time.get(code, 'N/A')}"
                )

            logger.info(f"[状态恢复] 已恢复 {len(positions)} 个持仓的状态")
        except Exception as e:
            logger.error(f"[状态恢复] 失败: {e}")

    # =================================================================
    #  统一离场检查入口 (替代原有的 3 个分散方法)
    # =================================================================

    def check_exits(self, code: str, current_price: float,
                    atr_value: float = 0, current_volume: float = 0,
                    avg_volume: float = 0, quote_data: dict = None) -> Optional[ExitDecision]:
        """
        统一离场检查 — 委托 DynamicExitManager.check_exit_priority()

        替代原有的 check_and_execute_stop_loss / check_and_execute_take_profit /
        update_trailing_stop 三个分散调用。

        v2.1: 增加停牌检测 — 停牌时跳过止损止盈检查，防止误触发
        """
        # ── 停牌检测 (P0-2 新增) ──
        try:
            from app.risk.suspension_guard import suspension_guard
            if suspension_guard.is_suspended(code, quote_data):
                action = suspension_guard.handle_suspension(code)
                if action == "alert" and suspension_guard.should_alert(code):
                    logger.critical(f"[停牌告警] {code} 长期停牌，建议人工干预")
                    suspension_guard.mark_alerted(code)
                # 停牌期间跳过止损止盈检查
                return None
            else:
                # 复牌 → 重置记录
                suspension_guard.reset(code)
        except Exception as e:
            logger.debug(f"[停牌检测] 失败: {e}")

        position = self._pm.get_position(code)
        if position is None:
            return None

        entry_price = position.entry_price
        if entry_price <= 0 or current_price <= 0:
            return None

        with self._lock:
            # 更新峰值
            peak = self._peak_price.get(code, entry_price)
            if current_price > peak:
                self._peak_price[code] = current_price
                peak = current_price

            # 获取/初始化 entry_time
            entry_time = self._entry_time.get(code)
            if entry_time is None:
                entry_time = getattr(position, 'entry_time', None)
                if entry_time:
                    self._entry_time[code] = entry_time

            direction = getattr(position, 'action', 'BUY') or 'BUY'
            current_state = self._exit_state.get(code, ExitState.INITIAL)

            # 判断是否当日开仓
            is_same_day = False
            if entry_time:
                try:
                    if isinstance(entry_time, str):
                        et_dt = datetime.fromisoformat(entry_time.replace(' ', 'T'))
                    else:
                        et_dt = entry_time
                    is_same_day = et_dt.date() == datetime.now().date()
                except Exception:
                    pass

        # 委托 DynamicExitManager 做全部判断 (pure computation, no lock needed)
        decision = self._dem.check_exit_priority(
            code=code,
            entry_price=entry_price,
            current_price=current_price,
            peak_price=peak,
            entry_time=entry_time,
            atr_value=atr_value,
            direction=direction,
            is_same_day=is_same_day,
            current_volume=current_volume,
            avg_volume=avg_volume,
            current_state=current_state,
        )

        # ── 硬编码止损兜底 (P0-3 新增) ──
        # 如果DynamicExitManager未触发止损，但硬编码检查触发，强制覆盖
        try:
            from app.risk.hard_stop_guard import hard_stop_guard
            if not decision or not decision.should_exit:
                hard_decision = hard_stop_guard.check_hard_stop(
                    code=code,
                    entry_price=entry_price,
                    current_price=current_price,
                    entry_time=entry_time,
                )
                if hard_decision and hard_decision.should_exit:
                    logger.critical(
                        f"[硬编码兜底] {code} DynamicExitManager未触发，"
                        f"但硬编码止损触发: {hard_decision.reason}"
                    )
                    decision = hard_decision
        except Exception as e:
            logger.debug(f"[硬编码兜底] 检查失败: {e}")

        with self._lock:
            # 更新状态机 (re-read state under lock to prevent TOCTOU)
            actual_state = self._exit_state.get(code, ExitState.INITIAL)
            if decision.new_state and decision.new_state != actual_state:
                self._exit_state[code] = decision.new_state
                if decision.new_stop_loss is not None:
                    try:
                        cur_tp = getattr(position, 'take_profit', 0) or 0
                        self._pm.update_stop_loss_take_profit(code, decision.new_stop_loss, cur_tp)
                    except Exception:
                        pass

        # 执行离场
        if decision.should_exit:
            self._execute_exit_decision(code, current_price, position, decision)
        elif decision.new_state == ExitState.BREAKEVEN_ACTIVE and decision.new_stop_loss:
            logger.info(f"[保本激活] {code} 止损上移至 {decision.new_stop_loss:.2f}")

        return decision

    def _execute_exit_decision(self, code: str, current_price: float,
                                position, decision: ExitDecision):
        """执行 ExitDecision"""
        # 冷却/坏订单检查 (仅止损需要)
        if decision.is_stop_loss:
            if code in self._bad_orders:
                logger.debug(f"[离场跳过] {code} BAD_ORDER")
                return
            if self._is_in_stop_loss_cooldown(code):
                return
            self._set_stop_loss_cooldown(code)

        lot_qty = self._round_to_lot(position.quantity, decision.close_ratio)
        if lot_qty <= 0:
            return

        # 实盘验证 Futu 持仓
        if not get_trading_env().is_simulate:
            if not self._verify_futu_position(code, lot_qty):
                return

        from app.core.types import Signal, SignalAction
        action = SignalAction.SELL if getattr(position, 'action', 'BUY') == 'BUY' else SignalAction.BUY
        signal = Signal(
            code=code, name=code, action=action,
            price=current_price, quantity=lot_qty,
            reason=decision.reason,
            strategy="DynamicExit",
            extra={
                "exit_decision": True,
                "close_ratio": decision.close_ratio,
                "is_stop_loss": decision.is_stop_loss,
                "sl_type": str(decision.sl_type) if decision.sl_type else None,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0,
            }
        )

        try:
            result = self._te.close_position(
                position=position, signal=signal,
                close_ratio=decision.close_ratio,
                reason=decision.reason,
            )
            if result and result.success:
                tag = "止损" if decision.is_stop_loss else "止盈"
                sl_info = f" [{decision.sl_type}]" if decision.sl_type else ""
                logger.info(f"[{tag}{sl_info}] {code} @ {current_price}: {decision.reason}")

                if decision.is_stop_loss and self._on_stop_loss_executed:
                    self._on_stop_loss_executed(code, current_price, result)
                elif not decision.is_stop_loss and self._on_take_profit_executed:
                    self._on_take_profit_executed(code, current_price, result)

                # Full exit → cleanup
                if decision.close_ratio >= 1.0:
                    self._cleanup_code_state(code)

                # 推送通知
                if self._notifier and hasattr(self._notifier, 'send_close_position_message'):
                    try:
                        self._notifier.send_close_position_message(
                            signal=signal, trade_result=result,
                            position=position, consensus_result=None,
                        )
                    except Exception:
                        pass
            else:
                if decision.is_stop_loss:
                    self._record_bad_order(code, str(result) if result else "unknown")
                    self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
                logger.error(f"[离场失败] {code}: {result}")
        except Exception as e:
            if decision.is_stop_loss:
                self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
            logger.error(f"[离场异常] {code}: {e}")

    # =================================================================
    #  市场状态更新
    # =================================================================

    def update_market_regime(self, regime: str, adx: float = None, atr_pct: float = None):
        """更新市场状态 → DynamicExitManager"""
        self._dem.update_regime(regime, adx, atr_pct)

    def on_consensus_signal(self, result):
        """接收共识信号 (用于信号质量追踪)"""
        code = result.code
        logger.debug(
            f"[持仓信号] {code} 共识得分={result.score:.2f} "
            f"方向={result.action.value if hasattr(result.action, 'value') else str(result.action)}"
        )

    # =================================================================
    #  辅助方法
    # =================================================================

    def _round_to_lot(self, quantity: int, ratio: float) -> int:
        target = max(1, int(quantity * ratio))
        for ls in [2000, 1000, 500, 200, 100]:
            if quantity % ls == 0:
                if target % ls == 0:
                    return target
                return ((target + ls - 1) // ls) * ls
        return target

    def _is_in_stop_loss_cooldown(self, code: str) -> bool:
        fail_count = self._stop_loss_fail_count.get(code, 0)
        cooldown_schedule = [5, 15, 30, 60, 60]
        cooldown = cooldown_schedule[min(fail_count, len(cooldown_schedule) - 1)]
        now = time.time()
        last = self._stop_loss_cooldown.get(code, 0)
        return (now - last) < cooldown

    def _set_stop_loss_cooldown(self, code: str):
        now = time.time()
        expired = [k for k, v in self._stop_loss_cooldown.items() if now - v > 600]
        for k in expired:
            del self._stop_loss_cooldown[k]
        if len(self._stop_loss_cooldown) > self._max_cache_size:
            oldest = min(self._stop_loss_cooldown, key=self._stop_loss_cooldown.get)
            del self._stop_loss_cooldown[oldest]
        self._stop_loss_cooldown[code] = now

    def _verify_futu_position(self, code: str, expected_qty: int) -> bool:
        try:
            futu_pos = None
            if hasattr(self._pm, 'get_futu_position'):
                futu_pos = self._pm.get_futu_position(code)
            elif hasattr(self._pm, '_futu_positions'):
                futu_pos = self._pm._futu_positions.get(code)
            if futu_pos is None or (hasattr(futu_pos, 'quantity') and futu_pos.quantity <= 0):
                logger.warning(f"[离场跳过] {code} Futu 无持仓 (幽灵仓)")
                try:
                    self._pm.close_position(code, 0, datetime.now().isoformat())
                except Exception:
                    pass
                self._cleanup_code_state(code)
                return False
            return True
        except Exception:
            return True

    def _record_bad_order(self, code: str, error: str):
        self._bad_orders[code] = {
            "fail_count": self._bad_orders.get(code, {}).get("fail_count", 0) + 1,
            "error": error, "timestamp": time.time(),
        }

    def _cleanup_code_state(self, code: str):
        with self._lock:
            for d in [self._exit_state, self._peak_price, self._entry_time,
                       self._stop_loss_cooldown, self._stop_loss_fail_count,
                       self._bad_orders]:
                d.pop(code, None)
