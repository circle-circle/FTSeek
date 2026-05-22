#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ExitExecutor — 退出执行服务

架构位置:
  DataSubscriber._execute_stop_loss / _execute_take_profit / _check_flash_exit
    ↓ 提取为
  app/services/exit_executor.py

设计目的:
  将止损/止盈/闪崩检测逻辑从 DataSubscriber 中提取为独立服务，
  降低 DataSubscriber 行数，提高可测试性。

依赖:
  - position_manager: 持仓查询/更新
  - trade_executor: 交易执行
  - notifier: 消息推送
  - consensus_engine: 信号质量记录
  - config_center: 配置读取

状态 (从 DataSubscriber 迁移):
  - _stop_loss_cooldown: Dict[str, float]  — 止损冷却时间戳
  - _stop_loss_fail_count: Dict[str, int]  — 止损失败次数
  - _stop_loss_inflight: Dict[str, bool]   — 止损执行中标记
  - _stop_loss_inflight_lock: threading.Lock
  - _bad_orders: Dict[str, Dict]           — 失败订单标记
  - soft_stop_warnings: Dict[str, float]   — 软止损预警时间

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取 (Phase 10A)
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Any

logger = logging.getLogger("ftseek.services.exit_executor")


class ExitExecutor:
    """退出执行服务 — 止损/止盈/闪崩检测"""

    def __init__(
        self,
        position_manager,
        trade_executor,
        notifier,
        consensus_engine,
        config_center=None,
        exit_engine=None,
        scenario_cache: Optional[Dict] = None,
    ):
        self._pm = position_manager
        self._executor = trade_executor
        self._notifier = notifier
        self._consensus = consensus_engine
        self._config = config_center
        self._exit_engine = exit_engine
        self._scenario_cache = scenario_cache or {}

        # 迁移状态 (从 DataSubscriber)
        self._stop_loss_cooldown: Dict[str, float] = {}
        self._stop_loss_fail_count: Dict[str, int] = {}
        self._stop_loss_inflight: Dict[str, bool] = {}
        self._stop_loss_inflight_lock = threading.Lock()
        self._bad_orders: Dict[str, Dict] = {}
        self.soft_stop_warnings: Dict[str, float] = {}

        # 缓存
        self._trailing_exit_time: Dict[str, float] = {}
        self._trailing_stop_cache: Dict[str, float] = {}
        self._peak_price_cache: Dict[str, float] = {}
        self._flash_snapshots: Dict[str, tuple] = {}
        self._flash_exit_enabled = False
        self._flash_window_seconds = 60
        self._flash_atr_mult = 2.0

    # ================================================================
    #  止损
    # ================================================================

    def execute_stop_loss(self, code: str, current_price: float) -> None:
        """执行止损平仓"""
        # Phase 6: 配置驱动委托给引擎
        if self._exit_engine is not None:
            position = self._pm.get_position(code)
            if position is not None:
                return self._exit_engine.execute_stop_loss(
                    code=code, current_price=current_price,
                    position=position, signal=None, trade_type="STOP_LOSS",
                )

        # 止损冷却递增
        fail_count = self._stop_loss_fail_count.get(code, 0)
        from config.config_center import get_config_center as _gcc
        COOLDOWN_SCHEDULE = _gcc().get('risk_limits.stop_loss_cooldown_schedule', [30, 60, 120, 300, 300])
        cooldown_seconds = COOLDOWN_SCHEDULE[min(fail_count, len(COOLDOWN_SCHEDULE) - 1)]

        now = time.time()
        last_stop_time = self._stop_loss_cooldown.get(code, 0)
        if now - last_stop_time < cooldown_seconds:
            logger.info(
                f"[止损冷却] {code} 冷却中，距离上次止损 {now - last_stop_time:.1f}s "
                f"< {cooldown_seconds}s (失败次数={fail_count})"
            )
            return

        # 跳过 BAD_ORDER
        if code in self._bad_orders:
            bo = self._bad_orders[code]
            logger.warning(
                f"[止损跳过] {code} 已标记为 BAD_ORDER "
                f"(失败{bo['fail_count']}次: {bo['error'][:60]})"
            )
            return

        # 原子锁保护 inflight
        with self._stop_loss_inflight_lock:
            if self._stop_loss_inflight.get(code, False):
                logger.debug(f"[止损跳过] {code} 已有止损线程正在执行")
                return
            self._stop_loss_inflight[code] = True

        position = self._pm.get_position(code)
        if position is None:
            self._stop_loss_inflight.pop(code, None)
            return

        # 整手对齐
        from app.config import DEFAULT_ORDER_QTY_BY_CODE, HK_LOT_SIZES
        lot_size = HK_LOT_SIZES.get(code) or DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)

        close_qty = position.quantity
        if close_qty < lot_size:
            logger.warning(
                f"[止损碎股处理] {code} 持仓{close_qty}股不足一手({lot_size}股)"
            )
        elif close_qty % lot_size != 0:
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size

        logger.info(
            f"[止损执行] {code} 持仓={position.quantity} | 平仓={close_qty} | "
            f"入场={position.entry_price} | 当前={current_price}"
        )

        # 创建止损信号 (P1 修复 2026-05-21: 传递 trace_id 确保全链路归因)
        from app.strategy.base import Signal, SignalAction
        stop_loss_signal = Signal(
            code=code, name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price, quantity=close_qty,
            reason=f"止损触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止损",
            trace_id=getattr(position, 'trace_id', '') or '',  # 从 position 获取 trace_id
            extra={"stop_loss": True, "entry_price": position.entry_price,
                   "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                   "exit_reason": "stop_loss"}
        )

        # Futu 持仓验证 (模拟盘跳过)
        try:
            from app.config import config
            if config.TRADE_ENV == 1:
                logger.debug(f"[止损验证] {code} 模拟盘跳过 Futu 验证")
            else:
                from app.trader.position_sync import get_position_sync
                ps = get_position_sync()
                futu_pos = ps._futu_positions.get(code)
                if futu_pos is None or futu_pos.quantity <= 0:
                    logger.warning(
                        f"[止损跳过] {code} Futu 无实际持仓，以当前价平仓"
                    )
                    try:
                        self._pm.close_position(code, current_price, datetime.now().isoformat())
                    except Exception as _e:
                        logger.error(f"[持仓修复] {code} 清理失败: {_e}")
                    self._clear_stop_state(code)
                    return
        except Exception as _e:
            logger.debug(f"[止损Futu验证] {code} 检查异常: {_e}")

        # 后台线程执行
        threading.Thread(
            target=self._do_execute_stop_loss,
            args=(code, stop_loss_signal, 'CLOSE'),
            daemon=True, name=f"StopLoss_{code}",
        ).start()

    def _do_execute_stop_loss(self, code: str, signal, trade_type: str):
        """后台线程执行止损"""
        try:
            position = self._pm.get_position(code)
            if position is None:
                logger.warning(f"[止损跳过] {code} 线程启动后持仓已被清除")
                return

            trade_result = self._executor.execute(signal, trade_type)
            current_price = signal.price

            if trade_result.success:
                logger.info(
                    f"[止损成功] {code} | 入场={position.entry_price} | "
                    f"出场={current_price} | 盈亏={trade_result.pnl:.2f}"
                )
                self._clear_stop_state(code)

                # 信号质量记录
                try:
                    current_scenario = None
                    if code in self._scenario_cache:
                        scenario_obj = self._scenario_cache[code]
                        current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)
                    self._consensus.record_signal_outcome(
                        code=code, action=position.action,
                        pnl=trade_result.pnl or 0.0, scenario=current_scenario,
                    )
                except Exception as _e:
                    logger.debug(f"[信号质量] 止损记录异常: {_e}")

                # 通知
                try:
                    self._notifier.send_close_position_message(
                        signal=signal, trade_result=trade_result,
                        position=position, consensus_result=None
                    )
                except Exception as e:
                    logger.warning(f"[止损推送失败] {code}: {e}")

                # 领域事件
                try:
                    from app.core.domain_events import get_event_bus, DomainEvent, EventType, TradeClosedPayload
                    pnl_rate = (current_price - position.entry_price) / position.entry_price if position.entry_price else 0
                    get_event_bus().emit(DomainEvent(
                        type=EventType.TRADE_CLOSED,
                        data=TradeClosedPayload(
                            code=code, market='HK',
                            entry_time=position.entry_time,
                            exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            entry_price=position.entry_price, exit_price=current_price,
                            quantity=position.quantity, pnl=trade_result.pnl or 0.0,
                            pnl_pct=pnl_rate, strategy='止损', action=position.action,
                            exit_reason='stop_loss', tags=['stop_loss', 'risk_exit'],
                            stop_loss=position.stop_loss, take_profit=position.take_profit,
                            highest_price=getattr(position, 'highest_price', None),
                            lowest_price=getattr(position, 'lowest_price', None),
                        ),
                        correlation_id=getattr(position, 'signal_id', None) or None,
                    ))
                except Exception as _e:
                    logger.debug(f"[领域事件] 止损事件发送异常: {_e}")
            else:
                error_msg = trade_result.message
                logger.error(f"[止损失败] {code}: {error_msg}")
                self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
                fail_count = self._stop_loss_fail_count[code]

                if fail_count >= 5:
                    self._bad_orders[code] = {
                        'error': error_msg, 'fail_count': fail_count,
                        'last_fail_time': time.time(),
                        'position_id': getattr(position, 'id', 'unknown'),
                    }
                    logger.error(
                        f"[止损严重警告] {code} 连续{fail_count}次止损失败，"
                        f"已标记为 BAD_ORDER！最后错误: {error_msg}"
                    )
                    try:
                        from app.notification import send_dingtalk_message
                        send_dingtalk_message(
                            f"🚨 P0 止损严重警告: {code} 连续{fail_count}次止损失败，"
                            f"已停止自动重试！错误: {error_msg}\n请立即人工平仓！"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[止损后台执行异常] {code}: {e}")
        finally:
            with self._stop_loss_inflight_lock:
                self._stop_loss_inflight.pop(code, None)
            self._stop_loss_cooldown[code] = time.time()

    def _clear_stop_state(self, code: str):
        """清理止损相关状态"""
        self._stop_loss_fail_count.pop(code, None)
        self._stop_loss_cooldown.pop(code, None)
        self._trailing_stop_cache.pop(code, None)
        self._peak_price_cache.pop(code, None)

    # ================================================================
    #  止盈
    # ================================================================

    def execute_take_profit(self, code: str, current_price: float) -> None:
        """执行止盈平仓"""
        if self._exit_engine is not None:
            position = self._pm.get_position(code)
            if position is not None:
                return self._exit_engine.execute_take_profit(
                    code=code, current_price=current_price,
                    position=position, signal=None,
                )

        position = self._pm.get_position(code)
        if position is None:
            return

        self._trailing_exit_time[code] = time.time()

        from app.config import DEFAULT_ORDER_QTY_BY_CODE
        lot_size = DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        close_qty = position.quantity
        if close_qty % lot_size != 0:
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size

        from app.strategy.base import Signal, SignalAction
        take_profit_signal = Signal(
            code=code, name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price, quantity=close_qty,
            reason=f"止盈触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止盈",
            trace_id=getattr(position, 'trace_id', '') or '',  # 从 position 获取 trace_id
            extra={"take_profit": True, "entry_price": position.entry_price,
                   "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                   "exit_reason": "take_profit"}
        )

        # Futu 验证 (模拟盘跳过)
        try:
            from app.config import config
            if config.TRADE_ENV != 1:
                from app.trader.position_sync import get_position_sync
                ps = get_position_sync()
                futu_pos = ps._futu_positions.get(code)
                if futu_pos is None or futu_pos.quantity <= 0:
                    logger.warning(f"[止盈跳过] {code} Futu 无实际持仓")
                    try:
                        self._pm.close_position(code, current_price, datetime.now().isoformat())
                    except Exception as _e:
                        logger.error(f"[持仓修复] {code} 清理失败: {_e}")
                    return
        except Exception as _e:
            logger.debug(f"[止盈Futu验证] {code} 检查异常: {_e}")

        trade_result = self._executor.execute(take_profit_signal, 'CLOSE')
        if trade_result.success:
            logger.info(
                f"[止盈成功] {code} | 入场={position.entry_price} | "
                f"出场={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            self._trailing_stop_cache.pop(code, None)
            self._peak_price_cache.pop(code, None)

            # 信号质量 + 通知 + 领域事件 (与止损同构)
            self._emit_close_event(
                code, position, current_price, trade_result,
                strategy='止盈', exit_reason='take_profit',
                tags=['take_profit', 'profit_exit'],
            )

            try:
                self._notifier.send_close_position_message(
                    signal=take_profit_signal, trade_result=trade_result,
                    position=position, consensus_result=None
                )
            except Exception as e:
                logger.warning(f"[止盈推送失败] {code}: {e}")

            # signal_tracker 回填
            try:
                from app.signal.core import get_signal_tracker as _gst
                _gst().record_exit_by_code(code=code, exit_price=current_price,
                                           pnl=trade_result.pnl or 0.0, exit_reason='take_profit')
            except Exception as e:
                logger.debug(f"[signal_tracker] 止盈回填失败: {e}")
        else:
            logger.error(f"[止盈失败] {code}: {trade_result.message}")

    def execute_partial_take_profit(self, code: str, current_price: float, ratio: float) -> None:
        """执行分批止盈"""
        position = self._pm.get_position(code)
        if position is None:
            return

        from app.config import DEFAULT_ORDER_QTY_BY_CODE, HK_LOT_SIZES
        lot_size = HK_LOT_SIZES.get(code) or DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        total_qty = position.quantity

        raw_close_qty = int(total_qty * ratio)
        close_qty = (raw_close_qty // lot_size) * lot_size

        if close_qty < lot_size:
            if total_qty >= lot_size * 2:
                close_qty = lot_size
            elif total_qty >= lot_size:
                close_qty = total_qty
                ratio = 1.0
            else:
                close_qty = total_qty
                ratio = 1.0
        else:
            remaining_qty = total_qty - close_qty
            if 0 < remaining_qty < lot_size:
                adjusted_close = total_qty - lot_size
                if adjusted_close >= lot_size:
                    close_qty = (adjusted_close // lot_size) * lot_size
                else:
                    close_qty = total_qty
                    ratio = 1.0

        from app.strategy.base import Signal, SignalAction
        partial_signal = Signal(
            code=code, name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price, quantity=close_qty,
            reason=f"分批止盈 ({ratio:.0%}) 盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%",
            strategy="分批止盈",
            trace_id=getattr(position, 'trace_id', '') or '',
            extra={"partial_take_profit": True, "ratio": ratio,
                   "entry_price": position.entry_price,
                   "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                   "exit_reason": "partial_take_profit"}
        )

        if ratio >= 1.0:
            trade_result = self._executor.execute(partial_signal, 'CLOSE')
        else:
            trade_result = self._executor.execute_partial_close(partial_signal, ratio)

        if trade_result.success:
            logger.info(
                f"[分批止盈成功] {code} | 平仓{close_qty}股({ratio:.0%}) | "
                f"盈亏={trade_result.pnl:.2f}"
            )
            # 保本止损上移
            self._update_breakeven_after_partial(code, position, current_price)

            # 信号质量
            try:
                current_scenario = None
                if code in self._scenario_cache:
                    scenario_obj = self._scenario_cache[code]
                    current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)
                self._consensus.record_signal_outcome(
                    code=code, action=position.action,
                    pnl=trade_result.pnl or 0.0, scenario=current_scenario,
                )
            except Exception as _e:
                logger.debug(f"[信号质量] 分批止盈记录异常: {_e}")

            # 领域事件
            self._emit_close_event(
                code, position, current_price, trade_result,
                strategy=f'分批止盈({ratio:.0%})', exit_reason='partial_take_profit',
                tags=['partial_take_profit', 'profit_exit'],
                quantity_override=close_qty,
            )
        else:
            logger.warning(f"[分批止盈失败] {code}: {trade_result.message}，尝试全仓平仓")
            full_signal = Signal(
                code=code, name=position.code,
                action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
                price=current_price, quantity=position.quantity,
                reason="分批止盈失败后全仓平仓", strategy="分批止盈兜底",
                trace_id=getattr(position, 'trace_id', '') or '',
                extra={"partial_take_profit": True, "fallback": True,
                       "entry_price": position.entry_price,
                       "exit_reason": "partial_take_profit_fallback"}
            )
            retry_result = self._executor.execute(full_signal, 'CLOSE')
            if retry_result.success:
                logger.info(f"[分批止盈兜底成功] {code} | 全仓平仓")
            else:
                logger.error(f"[分批止盈兜底失败] {code}: {retry_result.message}")

    def _update_breakeven_after_partial(self, code: str, position, current_price: float):
        """分批止盈后上移止损到保本价"""
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            be_cfg = _dem.get_breakeven_stop_params()
            fee_buffer = be_cfg.get('fee_buffer', 0.015)
        except Exception:
            fee_buffer = 0.015

        if position.action == 'BUY':
            new_sl = position.entry_price * (1 + fee_buffer)
        else:
            new_sl = position.entry_price * (1 - fee_buffer)

        if position.action == 'BUY' and new_sl > position.stop_loss:
            self._pm.update_stop_loss_take_profit(code, new_sl, position.take_profit)
            logger.info(f"[风控升级] {code} 分批止盈后止损上移至 {new_sl:.2f}")
        elif position.action == 'SELL' and new_sl < position.stop_loss:
            self._pm.update_stop_loss_take_profit(code, new_sl, position.take_profit)
            logger.info(f"[风控升级] {code} 分批止盈后止损下移至 {new_sl:.2f}")

    # ================================================================
    #  Flash Exit
    # ================================================================

    def check_flash_exit(self, code: str, current_price: float, position, get_atr_fn=None) -> None:
        """Flash Exit — 60s 内反向波动 >= ATR * multiplier 时立即平仓"""
        if not self._flash_exit_enabled:
            return

        now_ts = time.time()
        atr = get_atr_fn(code) if get_atr_fn else (current_price * 0.03)
        if atr <= 0:
            atr = current_price * 0.03

        prev_price, prev_ts = self._flash_snapshots.get(code, (current_price, now_ts))
        self._flash_snapshots[code] = (current_price, now_ts)

        if (now_ts - prev_ts) > self._flash_window_seconds:
            return

        direction = position.action
        if direction == 'BUY':
            adverse = prev_price - current_price
        else:
            adverse = current_price - prev_price

        if adverse <= 0:
            return

        threshold = atr * self._flash_atr_mult
        if adverse < threshold:
            return

        logger.critical(
            f"🚨 [Flash Exit] {code} 方向={direction} | "
            f"{self._flash_window_seconds:.0f}s 内反向波动={adverse:.4f} "
            f"({adverse/atr:.1f}x ATR) >= 阈值={threshold:.4f}"
        )

        try:
            self.execute_stop_loss(code, current_price)
        except Exception as e:
            logger.error(f"[Flash Exit] {code} 执行失败: {e}")

        self._peak_price_cache.pop(code, None)
        self._flash_snapshots.pop(code, None)

    # ================================================================
    #  辅助
    # ================================================================

    def _emit_close_event(self, code: str, position, current_price: float,
                          trade_result, strategy: str, exit_reason: str,
                          tags: list, quantity_override: int = None):
        """发送平仓领域事件"""
        try:
            from app.core.domain_events import get_event_bus, DomainEvent, EventType, TradeClosedPayload
            pnl_rate = (current_price - position.entry_price) / position.entry_price if position.entry_price else 0
            qty = quantity_override or position.quantity
            get_event_bus().emit(DomainEvent(
                type=EventType.TRADE_CLOSED,
                data=TradeClosedPayload(
                    code=code, market='HK',
                    entry_time=position.entry_time,
                    exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    entry_price=position.entry_price, exit_price=current_price,
                    quantity=qty, pnl=trade_result.pnl or 0.0,
                    pnl_pct=pnl_rate, strategy=strategy, action=position.action,
                    exit_reason=exit_reason, tags=tags,
                    stop_loss=position.stop_loss, take_profit=position.take_profit,
                    highest_price=getattr(position, 'highest_price', None),
                    lowest_price=getattr(position, 'lowest_price', None),
                ),
                correlation_id=getattr(position, 'signal_id', None) or None,
            ))
        except Exception as _e:
            logger.debug(f"[领域事件] {code} {exit_reason} 事件发送异常: {_e}")

    def get_state(self) -> Dict:
        """获取当前状态 (用于调试/监控)"""
        return {
            "stop_loss_cooldown_count": len(self._stop_loss_cooldown),
            "stop_loss_fail_count": dict(self._stop_loss_fail_count),
            "stop_loss_inflight": list(self._stop_loss_inflight.keys()),
            "bad_orders": list(self._bad_orders.keys()),
            "trailing_exit_time_count": len(self._trailing_exit_time),
        }
