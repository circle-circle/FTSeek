"""
KlineProcessor — K 线数据处理引擎

职责:
- 处理 K 线数据（多周期趋势更新、止损/止盈检查、策略计算、场景分类）
- 采用依赖注入模式，解耦 data_subscriber

从 data_subscriber.py 提取，降低耦合，提升可测试性

依赖注入:
- position_manager: 持仓管理器
- cache: 缓存管理器
- strategies: 策略列表
- consensus_engine: 共识引擎
- kline_exit_checker: K 线退出检查器
- kline_buffers: K 线缓冲区管理器
- regime_detector: 市场状态检测器
- scenario_classifier: 场景分类器
"""
import logging
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
import time

logger = logging.getLogger("ftseek.kline_processor")


class KlineProcessor:
    """
    K 线数据处理引擎
    
    依赖注入模式：所有外部依赖通过 __init__ 注入，便于单元测试
    """
    
    def __init__(
        self,
        position_manager,
        cache,  # CacheManager 实例
        strategies: List[Any],
        consensus_engine,
        kline_exit_checker,
        kline_buffers,
        regime_detector,
        scenario_classifier=None,
        scenario_memory=None,
        layered_manager=None,
        get_atr_fn: Optional[Callable] = None,
        execute_stop_loss_fn: Optional[Callable] = None,
        execute_take_profit_fn: Optional[Callable] = None,
        execute_partial_take_profit_fn: Optional[Callable] = None,
        soft_stop_warnings: Optional[Dict[str, float]] = None,
        # Phase 3c: 多周期趋势相关依赖
        mtf_engine=None,
        kline_buffers_by_period: Optional[Dict] = None,
        multi_timeframe_trends: Optional[Dict] = None,
        trends_updated: Optional[set] = None,
        sync_trend_change_fn: Optional[Callable] = None,
    ):
        """
        初始化 K 线处理器
        
        Args:
            position_manager: 持仓管理器
            cache: 缓存管理器（包含 peak_price_cache, trailing_stop_cache 等）
            strategies: 策略列表
            consensus_engine: 共识引擎
            kline_exit_checker: K 线退出检查器
            kline_buffers: K 线缓冲区管理器
            regime_detector: 市场状态检测器
            scenario_classifier: 场景分类器（可选）
            scenario_memory: 场景记忆（可选）
            layered_manager: 分层策略管理器（可选）
            get_atr_fn: ATR 获取函数（可选）
            execute_stop_loss_fn: 止损执行回调
            execute_take_profit_fn: 止盈执行回调
            execute_partial_take_profit_fn: 部分止盈执行回调
            soft_stop_warnings: 软止损预警缓存（可选，向后兼容）
            mtf_engine: 多周期趋势引擎（可选）
            kline_buffers_by_period: 按周期分组的 K 线缓冲区（可选）
            multi_timeframe_trends: 多周期趋势状态（可选）
            trends_updated: 已更新的趋势集合（可选）
            sync_trend_change_fn: 趋势变化同步回调（可选）
        """
        self.position_manager = position_manager
        self.cache = cache
        self.strategies = strategies
        self.consensus = consensus_engine
        self.kline_exit_checker = kline_exit_checker
        self.kline_buffers = kline_buffers
        self.regime_detector = regime_detector
        self.scenario_classifier = scenario_classifier
        self.scenario_memory = scenario_memory
        self.layered_manager = layered_manager
        self.get_atr = get_atr_fn or (lambda code: 0.0)
        self.execute_stop_loss = execute_stop_loss_fn or (lambda code, price: None)
        self.execute_take_profit = execute_take_profit_fn or (lambda code, price: None)
        self.execute_partial_take_profit = execute_partial_take_profit_fn or (lambda code, price, ratio: None)
        
        # 内部状态
        # P0 2026-05-21: soft_stop_warnings 支持注入（向后兼容 data_subscriber）
        self.soft_stop_warnings = soft_stop_warnings if soft_stop_warnings is not None else {}
        
        # Phase 3c: 多周期趋势相关依赖
        self.mtf_engine = mtf_engine
        self.kline_buffers_by_period = kline_buffers_by_period or {}
        self.multi_timeframe_trends = multi_timeframe_trends or {}
        self.trends_updated = trends_updated or set()
        self.sync_trend_change_fn = sync_trend_change_fn
        
        logger.info("[KlineProcessor] 初始化完成（依赖注入模式）")
    
    def process(self, code: str, kline: dict, k_type: str) -> bool:
        """
        处理单个 K 线数据
        
        Args:
            code: 股票代码
            kline: K 线数据字典
            k_type: K 线类型 (K_1M, K_5M, K_15M, etc.)
        
        Returns:
            True 如果触发了退出（止损/止盈），False 否则
        """
        # 提取当前价格 (用于止损/止盈检查)
        current_price = kline.get('close', 0) or kline.get('last_price', 0)
        
        # 1. 多周期趋势更新
        self._update_multi_timeframe_trend(code, k_type, kline)
        
        # 2. 更新 K 线 buffer
        self.kline_buffers[code].push_kline(kline)
        
        # 3. 分层策略管理器
        if self.layered_manager:
            try:
                self.layered_manager.update_kline_cache(code, k_type, kline)
            except Exception as e:
                logger.debug(f"[分层策略] K 线缓存更新异常：{e}")
        
        # 4. MarketRegimeDetector 接入多周期数据
        if k_type in ("K_15M", "K_30M", "K_60M"):
            try:
                self.regime_detector.update_data(code, kline)
            except Exception as e:
                logger.debug(f"[regime 多周期] {code} {k_type} 更新失败：{e}")
        
        # 5. KlineExitChecker 委托处理退出逻辑
        if self.kline_exit_checker is not None:
            if self.kline_exit_checker.check_exits(
                code=code,
                current_price=current_price,
                k_type=k_type,
                execute_stop_loss_fn=self.execute_stop_loss,
                execute_take_profit_fn=self.execute_take_profit,
                execute_partial_take_profit_fn=self.execute_partial_take_profit,
            ):
                return True  # 触发了退出
        
        # 6. 软止损预警 (仅记录日志，不执行交易)
        if current_price > 0:
            pos_now = self.position_manager.get_position(code)
            if pos_now and pos_now.stop_loss and pos_now.stop_loss > 0:
                self._check_soft_stop_warning(code, current_price, pos_now)
        
        # 7. 动态退出管理器 (每根 K 线检查一次)
        if k_type == "K_1M" and pos_now:
            if self._check_dynamic_exit(code, current_price, pos_now):
                return True  # 触发了退出
        
        # 8. 更新 ADX
        self._update_adx(code, kline)
        
        # 9. 场景分类（多周期）
        if k_type in ("K_5M", "K_15M") and self.scenario_classifier is not None:
            self._classify_scenario(code, k_type, kline)
        
        # 10. 策略计算（仅 K_1M）
        if k_type == "K_1M":
            self._process_strategies(code, kline, k_type)
        
        return False
    
    # ----------------------------------------------------------
    #  内部方法
    # ----------------------------------------------------------
    
    def _update_multi_timeframe_trend(self, code: str, k_type: str, kline: dict):
        """
        更新多周期趋势 — 委托给 MultiTimeframeEngine
        
        迁移自 data_subscriber.py L1109-1138
        
        依赖注入：mtf_engine, kline_buffers_by_period, multi_timeframe_trends, 
                 consensus, trends_updated, sync_trend_change_fn
        """
        if self.mtf_engine is None:
            return  # 未注入引擎，跳过
        
        try:
            # 委托给引擎计算趋势方向
            trend = self.mtf_engine.update_trend(
                code=code,
                k_type=k_type,
                kline=kline,
                kline_buffers=self.kline_buffers_by_period,
                trends=self.multi_timeframe_trends,
                consensus=self.consensus,
                trend_filter=None,  # trend_filter sync handled below
            )
            if trend is None:
                return  # 数据不足
            
            old_trend = self.multi_timeframe_trends[code][k_type]
            self.multi_timeframe_trends[code][k_type] = trend
            self.trends_updated.add((code, k_type))
            
            if trend != old_trend:
                # 趋势变化时的同步逻辑（保留原有 side effects）
                if self.sync_trend_change_fn:
                    self.sync_trend_change_fn(code, k_type, trend)
                
        except Exception as e:
            logger.debug(f"[多周期趋势] {code} {k_type} 更新失败：{e}")
    
    def _check_soft_stop_warning(self, code: str, current_price: float, pos_now):
        """软止损预警检查"""
        try:
            pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
            stop_rate = (pos_now.stop_loss - pos_now.entry_price) / pos_now.entry_price
            soft_threshold = stop_rate * 0.8
            if pnl_rate <= soft_threshold and pnl_rate > stop_rate:
                last_warning = self.soft_stop_warnings.get(code, 0)
                if time.time() - last_warning >= 300:
                    distance_to_stop = abs(pnl_rate - stop_rate)
                    logger.warning(
                        f"[软止损预警] {code} 亏损 {abs(pnl_rate):.2%} "
                        f"接近止损线 {abs(stop_rate):.2%}，距离 {distance_to_stop:.2%}"
                    )
                    self.soft_stop_warnings[code] = time.time()
        except Exception as e:
            logger.debug(f"[软止损预警] {code} 计算异常：{e}")
    
    def _check_dynamic_exit(self, code: str, current_price: float, pos_now) -> bool:
        """
        动态退出检查 — 组合退出状态机 + HKExitAggregator 灰度
        
        迁移自 data_subscriber.py L2930-3025
        """
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
                    is_same_day = True

            exit_decision = _dem_exit.check_exit_priority(
                code=code,
                entry_price=pos_now.entry_price,
                current_price=current_price,
                peak_price=self.cache.peak_price_cache.get(code, current_price),
                entry_time=getattr(pos_now, 'entry_time', None),
                atr_value=self.get_atr(code),
                direction=pos_now.action,
                is_same_day=is_same_day,
                exit_state=exit_state,  # P0 修复 2026-05-26: 传递 exit_state
            )
            if exit_decision.new_state:
                self.position_manager.update_exit_state(
                    code, exit_decision.new_state, reason=exit_decision.reason
                )
            if exit_decision.new_stop_loss:
                self.position_manager.update_stop_loss_take_profit(
                    code, exit_decision.new_stop_loss, pos_now.take_profit
                )
                logger.info(
                    f"[状态机] {code} 止损价更新：{pos_now.stop_loss:.2f} -> {exit_decision.new_stop_loss:.2f}"
                )
            if exit_decision.should_exit:
                logger.info(
                    f"[组合退出-P{exit_decision.priority}] {code} "
                    f"{exit_decision.reason} | ratio={exit_decision.close_ratio:.0%}"
                )
                if exit_decision.close_ratio < 1.0:
                    logger.info(f"[组合退出 - 部分止盈] {code} 平仓比例={exit_decision.close_ratio:.0%}")
                    self.execute_partial_take_profit(code, current_price, exit_decision.close_ratio)
                elif exit_decision.is_stop_loss:
                    self.execute_stop_loss(code, current_price)
                else:
                    logger.info(f"[组合退出 - 止盈] {code}")
                    self.execute_take_profit(code, current_price)
                self.cache.trailing_stop_cache.pop(code, None)
                self.cache.peak_price_cache.pop(code, None)
                return True
        except Exception as _de:
            # P0 修复 2026-05-26: 异常升级为 warning + fallback 硬止损
            logger.warning(
                f"[动态退出] {code} check_exit_priority 异常: {_de}，fallback 硬止损"
            )
            try:
                if self.position_manager.check_stop_loss(code, current_price):
                    logger.warning(f"[动态退出·fallback] {code} 硬止损触发")
                    self.execute_stop_loss(code, current_price)
                    return True
            except Exception:
                pass

        # ── P1 (2026-05-17): HKExitAggregator 灰度执行 ──
        # 从双跑模式升级为 50% 灰度执行
        # 50% 仓位用新退出系统 (ATR 动态止损)，50% 用旧系统 (固定百分比)
        # 目标：对比数据后决定是否全面切换
        try:
            from app.risk.hk_exit_aggregator import get_hk_exit_aggregator
            aggregator = get_hk_exit_aggregator()

            exit_result = aggregator.evaluate(
                code=code,
                entry_price=pos_now.entry_price,
                current_price=current_price,
                peak_price=self.cache.peak_price_cache.get(code, current_price),
                atr_value=self.get_atr(code),
                direction=pos_now.action,
                config={},  # 使用规则默认值
            )

            if exit_result.should_exit:
                # 灰度判断：模拟盘 50% 概率用新退出系统
                _use_new_exit = False
                try:
                    from config.config_center import get_config_center as _gcc_gray
                    _gray_ratio = _gcc_gray().get('base.simulation_mode.exit_gray_ratio', 0.5)
                    import random
                    _use_new_exit = random.random() < _gray_ratio
                except Exception:
                    _use_new_exit = False

                if _use_new_exit:
                    # 新退出系统执行 — 直接调用 execute_stop_loss (HKExitAggregator 本质是动态止损)
                    logger.info(
                        f"[HKAggregator·灰度执行] {code} rule={exit_result.rule_name} "
                        f"reason={exit_result.reason} price={exit_result.final_stop_price:.2f} "
                        f"ratio={exit_result.close_ratio:.0%}"
                    )
                    try:
                        self.execute_stop_loss(code, current_price)
                        self.cache.trailing_stop_cache.pop(code, None)
                        self.cache.peak_price_cache.pop(code, None)
                        return True
                    except Exception as _exec_e:
                        logger.warning(f"[HKAggregator·灰度执行] {code} 执行失败：{_exec_e}，回退旧系统")

                # 旧系统或灰度未命中 → 仅记录对比日志
                logger.debug(
                    f"[HKAggregator·双跑] {code} rule={exit_result.rule_name} "
                    f"gray={'命中' if _use_new_exit else '未命中'} (记录对比)"
                )
        except Exception as _agg_e:
            logger.debug(f"[HKAggregator·双跑] {code} 检查异常：{_agg_e}")
        
        return False
    
    def _update_adx(self, code: str, kline: dict):
        """更新 ADX 指标"""
        # TODO: 从 data_subscriber 迁移 _update_adx 逻辑
        pass
    
    def _classify_scenario(self, code: str, k_type: str, kline: dict):
        """场景分类"""
        # TODO: 从 data_subscriber 迁移场景分类逻辑
        pass
    
    def _process_strategies(self, code: str, kline: dict, k_type: str):
        """处理所有策略"""
        # TODO: 从 data_subscriber 迁移策略处理逻辑
        pass
