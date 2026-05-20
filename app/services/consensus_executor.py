#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConsensusExecutor — 共识执行服务

架构位置:
  DataSubscriber._on_consensus_inner (执行层逻辑)
    ↓ 提取为
  app/services/consensus_executor.py

设计目的:
  将共识结果的执行层逻辑 (交易执行/推送/领域事件/冷却更新) 从 DataSubscriber 中提取，
  保留决策层 (风控 Pipeline/MTF/港股适配) 在 DataSubscriber 中。

职责:
  1. 执行交易 (trade_executor.execute)
  2. 记录信号执行状态 (signal_tracker)
  3. 记录交易评估 (evaluator)
  4. 写入动态止损止盈 (position_manager)
  5. 推送通知 (notifier)
  6. 发送领域事件 (domain_events)
  7. 更新冷却时间 (cooldown)
  8. 记录自适应权重 (adaptive_weight_manager)
  9. 更新共识引擎信号质量 (consensus_engine)

依赖:
  - trade_executor: 交易执行
  - notifier: 消息推送
  - position_manager: 持仓管理/止损止盈更新
  - signal_tracker: 信号状态跟踪
  - evaluator: 交易评估
  - circuit_breaker: 熔断器
  - adaptive_weight_manager: 自适应权重
  - consensus_engine: 共识引擎

状态:
  - _cooldown: Dict[str, float] — 标的冷却时间
  - _cooldown_lock: threading.Lock
  - _open_position_ts: Dict[str, float] — 开仓原子锁
  - _open_lock: threading.Lock
  - _last_global_open_ts: float — 全局最后开仓时间

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取 (Phase 10B)
"""

import dataclasses
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Any

logger = logging.getLogger("ftseek.services.consensus_executor")


class ConsensusExecutor:
    """共识执行服务 — 交易执行/推送/领域事件/冷却更新"""

    def __init__(
        self,
        trade_executor,
        notifier,
        position_manager,
        signal_tracker,
        evaluator,
        circuit_breaker,
        adaptive_weight_manager,
        consensus_engine,
        scenario_cache: Optional[Dict] = None,
    ):
        self._executor = trade_executor
        self._notifier = notifier
        self._pm = position_manager
        self._tracker = signal_tracker
        self._evaluator = evaluator
        self._cb = circuit_breaker
        self._awm = adaptive_weight_manager
        self._consensus = consensus_engine
        self._scenario_cache = scenario_cache or {}

        # 状态管理
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        self._open_position_ts: Dict[str, float] = {}
        self._open_lock = threading.Lock()
        self._last_global_open_ts = 0.0

    # ================================================================
    #  核心执行
    # ================================================================

    def execute_consensus(
        self,
        code: str,
        consensus_signal,
        result,
        position,
        trade_type: str,
        signal_id: str,
        strategies_str: str,
    ) -> Optional[Any]:
        """
        执行共识结果 (交易 + 推送 + 领域事件 + 冷却更新)

        Args:
            code: 股票代码
            consensus_signal: 共识信号
            result: ConsensusResult
            position: 当前持仓 (可能为 None)
            trade_type: 'OPEN' / 'CLOSE' / 'HOLD'
            signal_id: 信号 ID
            strategies_str: 策略字符串

        Returns:
            TradeResult (如果执行了交易), None (如果被拒绝/跳过)
        """
        # 1. 执行交易
        trade_result = self._executor.execute(consensus_signal, trade_type)

        if not trade_result.success:
            logger.warning(f"交易失败：{trade_result.message}")
            try:
                self._tracker.record_execution(signal_id, executed=False, reject_reason=trade_result.message)
            except Exception as _rec_e:
                logger.warning(f"[执行记录] 写入失败状态 signal_tracker 异常：{_rec_e}")
            self._cb.record_reject(trade_result.message)
            return None

        # 2. 准备推送数据
        push_data = {
            'signal': consensus_signal,
            'trade_result': trade_result,
            'position': position,
            'consensus_result': result
        }

        # 3. 记录执行成功
        try:
            self._tracker.record_execution(
                signal_id,
                executed=True,
                execution_price=consensus_signal.price,
                execution_time=datetime.now().isoformat()
            )
        except Exception as _rec_e:
            logger.warning(f"[执行记录] 写入 signal_tracker 失败：{_rec_e}")

        # 4. 记录交易评估
        self._record_evaluation(
            code, result, consensus_signal, trade_type, signal_id,
            position, trade_result, strategies_str
        )

        # 5. 开仓后写入动态止损止盈
        if trade_type == 'OPEN':
            self._write_dynamic_stop_loss(code, consensus_signal, result)

        # 6. 平仓回填 signal_tracker pnl
        if trade_type == 'CLOSE' and trade_result.success and trade_result.pnl is not None:
            try:
                self._tracker.record_exit(
                    signal_id=signal_id,
                    exit_price=consensus_signal.price,
                    exit_time=datetime.now().isoformat(),
                    pnl=trade_result.pnl,
                    pnl_pct=(
                        trade_result.pnl / (position.entry_price * position.quantity)
                        if position and position.entry_price > 0 and position.quantity > 0
                        else None
                    )
                )
            except Exception as _e:
                logger.debug(f"[signal_tracker] 共识平仓回填失败：{_e}")

        # 7. 推送通知 + 领域事件
        self._push_and_emit(
            code, consensus_signal, result, position, trade_type,
            trade_result, push_data, signal_id, strategies_str
        )

        # 8. 更新冷却时间
        self._update_cooldown(code, trade_result)

        return trade_result

    # ================================================================
    #  开仓去重
    # ================================================================

    def check_open_dedup(self, code: str, signal_id: str) -> bool:
        """
        检查开仓去重 (5 秒内同一标的不能重复开仓)

        Returns:
            True = 通过 (可执行), False = 拒绝 (已去重)
        """
        _open_cooldown_sec = 5.0
        with self._open_lock:
            _last_open_ts = self._open_position_ts.get(code, 0)
            _now_ts = time.time()
            if _now_ts - _last_open_ts < _open_cooldown_sec:
                logger.warning(
                    f"[开仓去重] {code} {_open_cooldown_sec:.0f}s 内已执行过开仓 "
                    f"(距上次 {_now_ts - _last_open_ts:.1f}s)，拒绝重复信号"
                )
                self._tracker.record_execution(signal_id, executed=False, reject_reason='open_dedup')
                return False
            self._open_position_ts[code] = _now_ts
        return True

    def check_open_interval(self, code: str, signal_id: str) -> bool:
        """
        检查开仓间隔 (全局冷却，默认 5 分钟)

        Returns:
            True = 通过, False = 拒绝
        """
        try:
            from config.config_center import get_config_center
            _rl = get_config_center().get('risk_limits', {})
            _min_open_interval = _rl.get('min_open_interval_sec', 300)
            _interval_mode = _rl.get('min_open_interval_mode', 'warn')
        except Exception:
            _min_open_interval, _interval_mode = 300, 'warn'

        _now_ts = time.time()
        if self._last_global_open_ts > 0 and (_now_ts - self._last_global_open_ts) < _min_open_interval:
            _elapsed = int(_now_ts - self._last_global_open_ts)
            logger.warning(
                f"[开仓间隔警告] {_min_open_interval}s 内已开仓 (距上次 {_elapsed}s)，"
                f"{code} — {'仅记录' if _interval_mode == 'warn' else '拦截'}"
            )
            if _interval_mode == 'block':
                self._tracker.record_execution(
                    signal_id, executed=False,
                    reject_reason=f'open_interval_blocked ({_elapsed}s < {_min_open_interval}s)'
                )
                return False
        self._last_global_open_ts = _now_ts
        return True

    def check_symbol_trades(self, code: str) -> bool:
        """检查标的日内交易次数 (软拦截，仅警告)"""
        try:
            from config.config_center import get_config_center
            _rl = get_config_center().get('risk_limits', {})
            _max_symbol_trades = _rl.get('max_symbol_trades_per_day', 3)
        except Exception:
            _max_symbol_trades = 3

        if self._pm:
            _today_symbol_count = self._pm.get_today_trade_count(code)
            if _today_symbol_count >= _max_symbol_trades:
                logger.warning(
                    f"[日内频次警告] {code} 今日已交易 {_today_symbol_count} 次 "
                    f"(上限：{_max_symbol_trades} 次/日/股) — 模拟盘放行"
                )
        return True

    # ================================================================
    #  辅助方法
    # ================================================================

    def _record_evaluation(
        self, code, result, consensus_signal, trade_type, signal_id,
        position, trade_result, strategies_str
    ):
        """记录交易评估"""
        try:
            if trade_type == 'OPEN':
                self._evaluator.record_trade({
                    'trade_id': signal_id,
                    'code': code,
                    'action': result.action.value if hasattr(result.action, 'value') else str(result.action),
                    'entry_time': datetime.now().isoformat(),
                    'entry_price': consensus_signal.price,
                    'quantity': consensus_signal.quantity,
                    'trigger_strategy': strategies_str,
                    'trigger_reason': f"共识分数={result.score:+.2f}",
                    'confidence': result.confidence,
                    'consensus_score': result.score,
                    'market_regime': result.market_regime
                })
            else:
                if position:
                    self._evaluator.record_exit(
                        trade_id=signal_id,
                        exit_price=consensus_signal.price,
                        exit_time=datetime.now().isoformat(),
                        realized_pnl=trade_result.pnl or 0
                    )
        except Exception as e:
            logger.warning(f"记录交易评估失败：{e}")

    def _write_dynamic_stop_loss(self, code: str, consensus_signal, result):
        """开仓后写入动态止损止盈"""
        try:
            is_us = code.startswith('US.')
            if is_us:
                from app.risk.dynamic_exit_manager import get_us_exit_params
                us_params = get_us_exit_params()
                dyn = {
                    'stop_loss_pct': -abs(us_params.get('stop_loss', 0.015)),
                    'take_profit_pct': abs(us_params.get('take_profit', 0.04)),
                    **us_params
                }
            else:
                dyn = self._get_dynamic_stop_params(code)

            entry_px = consensus_signal.price
            is_long = (result.action.value if hasattr(result.action, 'value') else str(result.action)) == 'BUY'
            sl_price = round(entry_px * (1 + dyn["stop_loss_pct"]), 4)
            tp_price = round(entry_px * (1 + dyn["take_profit_pct"]), 4)

            if not is_long:
                sl_price = round(entry_px * (1 - dyn["stop_loss_pct"]), 4)
                tp_price = round(entry_px * (1 - dyn["take_profit_pct"]), 4)

            self._pm.update_stop_loss_take_profit(code, sl_price, tp_price)
            logger.info(
                f"[动态止损] {code} 开仓@{entry_px} | "
                f"止损={sl_price}({dyn['stop_loss_pct']:.1%}) | "
                f"止盈={tp_price}({dyn['take_profit_pct']:.1%}) | "
                f"regime={dyn['regime']}"
            )
        except Exception as _dyn_e:
            logger.warning(f"[动态止损] {code} 写入失败：{_dyn_e}")

    def _get_dynamic_stop_params(self, code: str) -> dict:
        """获取动态止损参数 (委托给 DataSubscriber)"""
        # 这里简化处理，实际应通过依赖注入获取
        from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
        return {
            'stop_loss_pct': _dem.get_stop_loss(),
            'take_profit_pct': _dem.get_take_profit(),
            'regime': _dem.current_regime
        }

    def _push_and_emit(
        self, code, consensus_signal, result, position, trade_type,
        trade_result, push_data, signal_id, strategies_str
    ):
        """推送通知 + 发送领域事件"""
        try:
            action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)

            if trade_type == 'OPEN':
                self._notifier.send_open_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 开仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id}"
                )
                self._emit_open_event(code, trade_result, result, signal_id)
            else:
                self._notifier.send_close_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 平仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id} | 盈亏：{trade_result.pnl:.2f}"
                )
                self._emit_close_event(code, position, trade_result, result, signal_id)

                # 平仓后记录自适应权重和信号质量
                if trade_result.pnl is not None:
                    current_scenario = None
                    if code in self._scenario_cache:
                        scenario_obj = self._scenario_cache[code]
                        current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)

                    self._awm.record_trade(
                        agree_strategies=result.agree_strategies,
                        pnl=trade_result.pnl,
                        market_regime=result.market_regime,
                        scenario=current_scenario,
                        adx_value=result.adx_value or 0.0,
                    )
                    self._consensus.record_signal_outcome(
                        code=code,
                        action=result.action,
                        pnl=trade_result.pnl,
                        market_regime=result.market_regime,
                        scenario=current_scenario,
                        adx_value=result.adx_value or 0.0,
                    )
        except Exception as e:
            logger.error(f"推送通知失败：{e}")

    def _emit_open_event(self, code, trade_result, result, signal_id):
        """发送开仓领域事件"""
        try:
            from app.core.domain_events import get_event_bus, DomainEvent, EventType, TradeOpenedPayload
            get_event_bus().emit(DomainEvent(
                type=EventType.TRADE_OPENED,
                data=TradeOpenedPayload(
                    code=code,
                    market='US' if code.startswith('US.') else 'HK',
                    price=getattr(trade_result, 'price', 0) or 0,
                    quantity=getattr(trade_result, 'quantity', 0) or 0,
                    action='BUY' if hasattr(trade_result, 'action') and str(getattr(trade_result, 'action', '')).endswith('BUY') else 'SELL',
                    regime=getattr(result, 'regime', 'consensus') if result else 'consensus',
                    entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    signal_id=str(signal_id) if signal_id else '',
                    signal_trace_id=getattr(signal_id, 'signal_id', '') if signal_id else '',
                ),
                correlation_id=str(signal_id) if signal_id else None,
            ))
        except Exception as _e:
            logger.debug(f"[领域事件] 开仓事件发送异常：{_e}")

    def _emit_close_event(self, code, position, trade_result, result, signal_id):
        """发送平仓领域事件"""
        try:
            from app.core.domain_events import get_event_bus, DomainEvent, EventType, TradeClosedPayload
            entry_px = getattr(position, 'entry_price', 0) if position else 0
            exit_px = getattr(trade_result, 'price', 0) or 0
            qty = getattr(position, 'quantity', 0) if position else 0
            pnl_val = getattr(trade_result, 'pnl', 0) or 0
            pnl_pct = (pnl_val / (entry_px * qty)) if entry_px and qty else 0
            get_event_bus().emit(DomainEvent(
                type=EventType.TRADE_CLOSED,
                data=TradeClosedPayload(
                    code=code,
                    market='US' if code.startswith('US.') else 'HK',
                    entry_time=getattr(position, 'entry_time', '') if position else '',
                    exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    entry_price=entry_px,
                    exit_price=exit_px,
                    quantity=qty,
                    pnl=pnl_val,
                    pnl_pct=pnl_pct,
                    strategy=getattr(result, 'regime', 'consensus') if result else 'consensus',
                    action='BUY' if hasattr(trade_result, 'action') and str(getattr(trade_result, 'action', '')).endswith('BUY') else 'SELL',
                    exit_reason=getattr(trade_result, 'reason', '') if hasattr(trade_result, 'reason') else '',
                    signal_id=str(signal_id) if signal_id else '',
                    signal_trace_id=getattr(signal_id, 'signal_id', '') if signal_id else '',
                    stop_loss=getattr(position, 'stop_loss', None) if position else None,
                    take_profit=getattr(position, 'take_profit', None) if position else None,
                    highest_price=getattr(position, 'highest_price', None) if position else None,
                    lowest_price=getattr(position, 'lowest_price', None) if position else None,
                ),
                correlation_id=str(signal_id) if signal_id else None,
            ))
        except Exception as _e:
            logger.debug(f"[领域事件] 平仓事件发送异常：{_e}")

    def _update_cooldown(self, code: str, trade_result):
        """更新冷却时间"""
        with self._cooldown_lock:
            last_pnl = trade_result.pnl if trade_result and trade_result.pnl is not None else None
            # 简化：使用固定冷却时间，实际应通过依赖注入获取自适应冷却
            adaptive_cd = 60  # 默认 60 秒
            self._cooldown[code] = time.time()
            logger.debug(f"[{code}] 冷却={adaptive_cd}s (pnl={last_pnl})")

    def get_state(self) -> Dict:
        """获取当前状态 (用于调试/监控)"""
        return {
            "cooldown_count": len(self._cooldown),
            "open_position_ts_count": len(self._open_position_ts),
            "last_global_open_ts": self._last_global_open_ts,
        }
