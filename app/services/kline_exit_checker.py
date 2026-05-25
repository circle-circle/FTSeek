#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KlineExitChecker — K 线退出检查服务

从 data_subscriber.py 的 _process_kline_data 方法中提取的退出检查逻辑。
包含：硬止损、软止损预警、止盈、动态退出、HKExitAggregator、时间止损、放量退出。

版本历史:
  2026-05-19 v1: 创建 (Phase 15)
"""

import logging
import time
from datetime import datetime
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger(__name__)


class KlineExitChecker:
    """K 线退出检查器"""

    def __init__(
        self,
        position_manager,
        exit_executor,
        consensus_engine,
        peak_price_cache: Dict[str, float],
        trailing_stop_cache: Dict[str, float],
        soft_stop_warnings: Dict[str, float],
        kline_buffers: Dict[str, Any],
        get_atr_fn: Optional[Callable] = None,
    ):
        """
        初始化 K 线退出检查器

        Args:
            position_manager: 仓位管理器
            exit_executor: 退出执行器服务
            consensus_engine: 共识引擎
            peak_price_cache: 峰值价格缓存
            trailing_stop_cache: 追踪止损缓存
            soft_stop_warnings: 软止损预警时间戳
            kline_buffers: K 线缓冲区
            get_atr_fn: ATR 获取函数
        """
        self._pm = position_manager
        self._exit_executor = exit_executor
        self._consensus = consensus_engine
        self._peak_price_cache = peak_price_cache
        self._trailing_stop_cache = trailing_stop_cache
        self._soft_stop_warnings = soft_stop_warnings
        self._kline_buffers = kline_buffers
        self._get_atr = get_atr_fn or (lambda _: 1.0)

        # P0 修复 2026-05-21: 接入 ExitRuleEngine 双跑模式
        self._exit_rule_engine = None
        try:
            from app.risk.exit_rule_engine import get_exit_rule_engine
            self._exit_rule_engine = get_exit_rule_engine()
            if self._exit_rule_engine.rules:
                logger.info(f"[KlineExitChecker] ExitRuleEngine 双跑模式已接入 ({len(self._exit_rule_engine.rules)} 规则)")
            else:
                self._exit_rule_engine = None
        except Exception as _ere:
            logger.debug(f"[KlineExitChecker] ExitRuleEngine 接入跳过: {_ere}")

    def check_exits(
        self,
        code: str,
        current_price: float,
        k_type: str,
        execute_stop_loss_fn: Callable,
        execute_take_profit_fn: Callable,
        execute_partial_take_profit_fn: Callable,
    ) -> bool:
        """
        执行退出检查

        Args:
            code: 股票代码
            current_price: 当前价格
            k_type: K 线类型 (K_1M, K_5M, etc.)
            execute_stop_loss_fn: 止损执行函数
            execute_take_profit_fn: 止盈执行函数
            execute_partial_take_profit_fn: 分批止盈执行函数

        Returns:
            True 表示已触发退出，调用方应 return
        """
        if current_price <= 0:
            return False

        pos_now = self._pm.get_position(code)
        if not pos_now:
            return False

        # 1. 硬止损 (保留作为最后一道防线)
        if self._pm.check_stop_loss(code, current_price):
            if pos_now.entry_time:
                try:
                    entry_dt = datetime.fromisoformat(pos_now.entry_time.replace(' ', 'T'))
                    elapsed = (datetime.now() - entry_dt).total_seconds()

                    is_hard_risk = False
                    try:
                        from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem_hr
                        struct_stop = abs(_dem_hr.get_stop_loss())
                        pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
                        if pnl_rate <= -struct_stop * 0.8:
                            is_hard_risk = True
                    except Exception:
                        pass

                    if elapsed < 600 and not is_hard_risk:
                        logger.warning(f"[止损跳过] {code} 开仓仅 {elapsed:.0f} 秒(<10min保护期)，跳过非硬风险")
                    else:
                        logger.info(f"[止损触发] {code} 当前价={current_price} {'(硬风险)' if is_hard_risk else ''}")
                        execute_stop_loss_fn(code, current_price)
                        return True
                except Exception:
                    logger.info(f"[止损触发] {code} 当前价={current_price}")
                    execute_stop_loss_fn(code, current_price)
                    return True
            else:
                logger.info(f"[止损触发] {code} 当前价={current_price}")
                execute_stop_loss_fn(code, current_price)
                return True

        # 2. 软止损预警
        if pos_now.stop_loss and pos_now.stop_loss > 0:
            pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
            stop_rate = (pos_now.stop_loss - pos_now.entry_price) / pos_now.entry_price
            soft_threshold = stop_rate * 0.8
            if pnl_rate <= soft_threshold and pnl_rate > stop_rate:
                last_warning = self._soft_stop_warnings.get(code, 0)
                if time.time() - last_warning >= 300:
                    distance_to_stop = abs(pnl_rate - stop_rate)
                    logger.warning(
                        f"[软止损预警] {code} 亏损 {abs(pnl_rate):.2%} "
                        f"接近止损线 {abs(stop_rate):.2%}，距离 {distance_to_stop:.2%}"
                    )
                    self._soft_stop_warnings[code] = time.time()

        # 3. 止盈
        if self._pm.check_take_profit(code, current_price):
            logger.info(f"[止盈触发] {code} 当前价={current_price}")
            execute_take_profit_fn(code, current_price)
            return True

        # 4. 动态退出管理器 (K_1M only)
        if k_type == "K_1M":
            if self._check_dynamic_exit(code, current_price, pos_now, execute_stop_loss_fn,
                                        execute_take_profit_fn, execute_partial_take_profit_fn):
                return True

            # 5. HKExitAggregator 灰度执行
            if self._check_hk_exit_aggregator(code, current_price, pos_now, execute_stop_loss_fn):
                return True

            # 6. 时间止损 + 放量退出
            if self._check_time_and_volume_exit(code, current_price, pos_now, k_type,
                                                  execute_take_profit_fn, execute_partial_take_profit_fn):
                return True

        # P0 修复 2026-05-21: ExitRuleEngine 双跑评估 (仅记录，不执行)
        self._shadow_exit_rule_eval(code, current_price, pos_now)

        return False

    def _shadow_exit_rule_eval(self, code: str, current_price: float, pos_now):
        """P0 修复 2026-05-21: ExitRuleEngine 双跑评估 — 仅记录，不执行交易
        
        构建 ExitContext 并调用 ExitRuleEngine.evaluate()，将结果写入日志。
        目的：收集 Phase 1 规则的实际触发数据，为 Phase 2 (正式切换) 提供对比依据。
        """
        if self._exit_rule_engine is None:
            return
        try:
            from app.risk.exit_rule_engine import ExitContext
            atr = self._get_atr(code)
            entry_price = pos_now.entry_price
            if entry_price <= 0 or atr <= 0:
                return

            # 动态计算 is_same_day
            is_same_day = True
            entry_time_raw = getattr(pos_now, 'entry_time', None)
            if entry_time_raw:
                try:
                    from datetime import datetime as _dt_now
                    if isinstance(entry_time_raw, str):
                        _entry_dt = _dt_now.fromisoformat(entry_time_raw.replace(' ', 'T'))
                    else:
                        _entry_dt = entry_time_raw
                    is_same_day = (_dt_now.now().date() == _entry_dt.date())
                except Exception:
                    is_same_day = True

            ctx = ExitContext(
                code=code,
                entry_price=entry_price,
                current_price=current_price,
                peak_price=self._peak_price_cache.get(code, current_price),
                atr_value=atr,
                entry_time=getattr(pos_now, 'entry_time', None),
                direction=pos_now.action,
                is_same_day=is_same_day,
            )
            decision = self._exit_rule_engine.evaluate(ctx)
            
            # 增强日志：记录评估结果（无论是否触发）
            if decision.should_exit:
                logger.info(
                    f"[ExitRule·双跑] {code} {decision.rule_name} 触发 "
                    f"(P{decision.priority}) | {decision.reason} | "
                    f"pnl={ctx.pnl_pct:.2%} (仅记录，不执行)"
                )
            else:
                # 记录评估状态（用于监控和调试）
                logger.debug(
                    f"[ExitRule·双跑] {code} 评估完成 | "
                    f"pnl={ctx.pnl_pct:.2%} | atr={atr:.4f} | "
                    f"peak={self._peak_price_cache.get(code, current_price):.3f} | "
                    f"未触发 (阈值：-3.5%)"
                )
        except Exception as _sre:
            logger.debug(f"[ExitRule·双跑] {code} 评估异常：{_sre}")

    def _check_dynamic_exit(
        self,
        code: str,
        current_price: float,
        pos_now,
        execute_stop_loss_fn: Callable,
        execute_take_profit_fn: Callable,
        execute_partial_take_profit_fn: Callable,
    ) -> bool:
        """动态退出管理器检查"""
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem_exit
            exit_state = getattr(pos_now, 'exit_state', None)

            # P0 修复 2026-05-26: 动态计算 is_same_day，不再硬编码 True
            is_same_day = True
            entry_time_raw = getattr(pos_now, 'entry_time', None)
            if entry_time_raw:
                try:
                    from datetime import datetime as _dt_now
                    if isinstance(entry_time_raw, str):
                        _entry_dt = _dt_now.fromisoformat(entry_time_raw.replace(' ', 'T'))
                    else:
                        _entry_dt = entry_time_raw
                    is_same_day = (_dt_now.now().date() == _entry_dt.date())
                except Exception:
                    is_same_day = True  # 解析失败 fallback 保守策略

            exit_decision = _dem_exit.check_exit_priority(
                code=code,
                entry_price=pos_now.entry_price,
                current_price=current_price,
                peak_price=self._peak_price_cache.get(code, current_price),
                entry_time=getattr(pos_now, 'entry_time', None),
                atr_value=self._get_atr(code),
                direction=pos_now.action,
                is_same_day=is_same_day,
                exit_state=exit_state,  # P0 修复 2026-05-26: 传递 exit_state 防止分批止盈重复
            )
            if exit_decision.new_state:
                self._pm.update_exit_state(
                    code, exit_decision.new_state, reason=exit_decision.reason
                )
            if exit_decision.new_stop_loss:
                self._pm.update_stop_loss_take_profit(
                    code, exit_decision.new_stop_loss, pos_now.take_profit
                )
                logger.info(
                    f"[状态机] {code} 止损价更新: {pos_now.stop_loss:.2f} -> {exit_decision.new_stop_loss:.2f}"
                )
            if exit_decision.should_exit:
                logger.info(
                    f"[组合退出-P{exit_decision.priority}] {code} "
                    f"{exit_decision.reason} | ratio={exit_decision.close_ratio:.0%}"
                )
                if exit_decision.close_ratio < 1.0:
                    logger.info(f"[组合退出-部分止盈] {code} 平仓比例={exit_decision.close_ratio:.0%}")
                    execute_partial_take_profit_fn(code, current_price, exit_decision.close_ratio)
                elif exit_decision.is_stop_loss:
                    execute_stop_loss_fn(code, current_price)
                else:
                    logger.info(f"[组合退出-止盈] {code}")
                    execute_take_profit_fn(code, current_price)
                self._trailing_stop_cache.pop(code, None)
                self._peak_price_cache.pop(code, None)
                return True
        except Exception as _de:
            # P0 修复 2026-05-26: 异常不再静默吞掉，升级为 warning + fallback 硬止损
            logger.warning(
                f"[动态退出] {code} check_exit_priority 异常: {_de}，"
                f"fallback 到硬止损检查", exc_info=False
            )
            # Fallback: 如果动态退出引擎挂了，回退到基础硬止损
            try:
                if self._pm.check_stop_loss(code, current_price):
                    logger.warning(f"[动态退出·fallback] {code} 硬止损触发")
                    execute_stop_loss_fn(code, current_price)
                    return True
            except Exception:
                pass  # fallback 也失败，不阻塞主循环

        return False

    def _check_hk_exit_aggregator(
        self,
        code: str,
        current_price: float,
        pos_now,
        execute_stop_loss_fn: Callable,
    ) -> bool:
        """HKExitAggregator 灰度执行"""
        try:
            from app.risk.hk_exit_aggregator import get_hk_exit_aggregator
            aggregator = get_hk_exit_aggregator()

            exit_result = aggregator.evaluate(
                code=code,
                entry_price=pos_now.entry_price,
                current_price=current_price,
                peak_price=self._peak_price_cache.get(code, current_price),
                atr_value=self._get_atr(code),
                direction=pos_now.action,
                config={},
            )

            if exit_result.should_exit:
                # 灰度判断: 模拟盘 50% 概率用新退出系统
                _use_new_exit = False
                try:
                    from config.config_center import get_config_center as _gcc_gray
                    _gray_ratio = _gcc_gray().get('base.simulation_mode.exit_gray_ratio', 0.5)
                    import random
                    _use_new_exit = random.random() < _gray_ratio
                except Exception:
                    _use_new_exit = False

                if _use_new_exit:
                    logger.info(
                        f"[HKAggregator·灰度执行] {code} rule={exit_result.rule_name} "
                        f"reason={exit_result.reason} price={exit_result.final_stop_price:.2f} "
                        f"ratio={exit_result.close_ratio:.0%}"
                    )
                    try:
                        execute_stop_loss_fn(code, current_price)
                        self._trailing_stop_cache.pop(code, None)
                        self._peak_price_cache.pop(code, None)
                        return True
                    except Exception as _exec_e:
                        logger.warning(f"[HKAggregator·灰度执行] {code} 执行失败: {_exec_e}，回退旧系统")

                # 旧系统或灰度未命中 → 仅记录对比日志
                logger.debug(
                    f"[HKAggregator·双跑] {code} rule={exit_result.rule_name} "
                    f"gray={'命中' if _use_new_exit else '未命中'} (记录对比)"
                )
        except Exception as _agg_e:
            logger.debug(f"[HKAggregator·双跑] {code} 检查异常: {_agg_e}")

        return False

    def _check_time_and_volume_exit(
        self,
        code: str,
        current_price: float,
        pos_now,
        k_type: str,
        execute_take_profit_fn: Callable,
        execute_partial_take_profit_fn: Callable,
    ) -> bool:
        """时间止损 + 放量退出检查"""
        # 时间止损
        time_stop_enabled = False
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem_ts
            time_stop_cfg = _dem_ts.current_params.get('time_stop', {})
            time_stop_enabled = time_stop_cfg.get('enabled', False)
            if time_stop_enabled:
                max_hold_minutes = time_stop_cfg.get('max_hold_minutes', 480)
                entry_time_str = getattr(pos_now, 'entry_time', None)
                if entry_time_str:
                    et = datetime.fromisoformat(entry_time_str.replace(' ', 'T')) if isinstance(entry_time_str, str) else entry_time_str
                    hold_minutes = (datetime.now() - et).total_seconds() / 60
                    pnl_pct = (current_price - pos_now.entry_price) / pos_now.entry_price
                    if pos_now.action == 'SELL':
                        pnl_pct = -pnl_pct
                    if hold_minutes > max_hold_minutes and abs(pnl_pct) < 0.01:
                        logger.info(f"[时间止损] {code} 持仓{hold_minutes:.0f}分钟超限, 强制平仓")
                        execute_take_profit_fn(code, current_price)
                        self._trailing_stop_cache.pop(code, None)
                        self._peak_price_cache.pop(code, None)
                        return True
        except Exception as _ts_e:
            logger.warning(f"[时间止损] {code} 检查异常: {_ts_e}")

        # 放量异常退出
        if self._kline_buffers[code].size >= 25:
            try:
                vols = list(self._kline_buffers[code].volume)
                if len(vols) >= 20:
                    avg_vol_20 = sum(vols[-21:-1]) / 20
                    current_vol = vols[-1]
                    pnl_pct = (current_price - pos_now.entry_price) / pos_now.entry_price
                    if pos_now.action == 'SELL':
                        pnl_pct = -pnl_pct
                    if current_vol > avg_vol_20 * 3.0 and pnl_pct > 0.03:
                        logger.info(f"[放量止盈] {code} 量比={current_vol/avg_vol_20:.1f}x, 部分止盈 50%")
                        execute_partial_take_profit_fn(code, current_price, 0.5)
                        return True
            except Exception as _vol_e:
                logger.warning(f"[放量止盈] {code} 检查异常: {_vol_e}")

        return False
