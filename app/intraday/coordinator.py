"""
盘中协调器 — 整合行情订阅、信号处理、持仓管理、Worker 池

职责：
1. 组合 intraday 子模块
2. 管理模块间数据流
3. 提供统一的生命周期接口 (start/stop)

使用方式:
    from app.intraday.coordinator import IntradayCoordinator
    
    coordinator = IntradayCoordinator(
        position_manager=...,
        trade_executor=...,
        consensus_engine=...
    )
    coordinator.start(battle_plan)
"""

import logging
import time
import threading
from app.core.event_bus import MarketEvent, StrategyEvent, RiskEvent
from typing import Optional, Dict, Any, List

from app.core.config_loader import cfg
from app.core.stages import BattlePlan

logger = logging.getLogger("ftseek.intraday.coordinator")


class IntradayCoordinator:
    """
    盘中协调器
    
    组合 4 个子模块：
    - MarketSubscriber: 行情订阅
    - SignalProcessor: 信号处理
    - IntradayPositionManager: 持仓管理
    - WorkerPool: Worker 线程池
    """
    
    def __init__(self, position_manager=None, trade_executor=None,
                 consensus_engine=None, notifier=None, strategies=None):
        """
        Args:
            position_manager: 持仓管理器实例 (app.trader.position_manager)
            trade_executor: 交易执行器实例 (app.trader.executor)
            consensus_engine: 共识引擎实例 (app.strategy.consensus_engine)
            notifier: 通知器实例
            strategies: 策略列表 (用于 K 线信号生成)
        """
        self._running = False
        self._battle_plan: Optional[BattlePlan] = None
        
        # 延迟初始化子模块
        self._market_subscriber = None
        self._signal_processor = None
        self._position_manager = None
        self._worker_pool = None
        self._risk_pipeline = None
        self._subscription_mgr = None  # 分时段订阅管理器
        
        # 保存依赖
        self._pm = position_manager
        self._te = trade_executor
        self._consensus = consensus_engine
        self._notifier = notifier
        self._strategies = strategies or []  # P1 修复: 保存策略列表

        # 交易并发控制: 限制同时执行的交易数量，防止 DB 连接池/API 过载
        self._trade_semaphore = threading.BoundedSemaphore(value=5)
        
        # 回调注册
        self._callbacks_registered = False
    
    # ===== 延迟初始化 =====
    
    def _ensure_modules(self):
        """确保所有子模块已初始化"""
        if self._market_subscriber is None:
            from app.intraday.market_subscriber import MarketSubscriber
            self._market_subscriber = MarketSubscriber()
        
        if self._signal_processor is None:
            from app.intraday.signal_processor import SignalProcessor
            self._signal_processor = SignalProcessor(self._consensus)
        
        if self._position_manager is None:
            from app.intraday.position_manager import IntradayPositionManager
            self._position_manager = IntradayPositionManager(
                position_manager=self._pm,
                trade_executor=self._te,
                notifier=self._notifier,
            )
        
        if self._worker_pool is None:
            from app.intraday.worker_pool import WorkerPool
            self._worker_pool = WorkerPool()

        if self._risk_pipeline is None:
            from app.risk.risk_pipeline import RiskPipeline
            self._risk_pipeline = RiskPipeline(deps={
                "position_manager": self._pm,
                "signal_processor": self._signal_processor,
                "consensus_engine": self._consensus,
            })

        # ── 分时段订阅管理器 (2026-05-01 新增) ──
        # 协议驱动: SubscriptionManager 通过 ISubscriptionGateway 与外部交互
        if self._subscription_mgr is None:
            from app.intraday.subscription_manager import (
                SubscriptionManager, MarketSubscriberAdapter,
            )
            # 延迟创建 adapter，需要 MarketSubscriber 实例
            self._subscription_mgr = None  # 在 start() 中创建 adapter
    
    def _register_callbacks(self):
        """注册模块间回调"""
        if self._callbacks_registered:
            return
        
        self._ensure_modules()
        
        # 行情回调 → 信号处理
        self._market_subscriber.set_callbacks(
            on_quote=self._on_quote_data,
            on_kline=self._on_kline_data,
        )
        
        # 信号处理回调 → 持仓管理
        self._signal_processor.set_callbacks(
            on_consensus_result=self._on_consensus_result,
        )

        # P0 修复: 共识引擎回调 → 信号处理器 (缺失导致共识结果无人接收)
        self._consensus.on_consensus = self._signal_processor.on_consensus
        logger.info("共识引擎回调已注册 → SignalProcessor.on_consensus")
        
        # Worker 池回调 → 信号处理
        self._worker_pool.set_callbacks(
            on_quote=self._process_quote_data,
            on_kline=self._process_kline_data,
        )
        
        self._callbacks_registered = True
        logger.info("Intraday 模块间回调已注册")
    
    # ===== 生命周期 =====
    
    def start(self, battle_plan: Optional[BattlePlan] = None):
        """
        启动盘中监控
        
        Args:
            battle_plan: 盘前作战计划（可选）
        """
        if self._running:
            logger.warning("盘中监控已在运行")
            return
        
        self._battle_plan = battle_plan
        self._ensure_modules()
        self._register_callbacks()
        
        # 启动行情连接
        if self._market_subscriber.connect():
            self._market_subscriber.setup_handlers()
        
        # 启动行情 Worker 线程 (解析 rsp_str → 回调)
        self._market_subscriber.start_workers()
        
        # ── 启动预热 — 拉取历史 K 线填充策略 Buffer ──
        codes = self._get_subscribe_codes()
        self._warm_up_strategy_buffers(codes)
        
        # ── 分时段订阅管理器 (2026-05-01 协议驱动) ──
        # 通过 MarketSubscriberAdapter 将 MarketSubscriber 包装为 ISubscriptionGateway
        from app.intraday.subscription_manager import (
            SubscriptionManager, MarketSubscriberAdapter,
        )
        adapter = MarketSubscriberAdapter(self._market_subscriber)
        self._subscription_mgr = SubscriptionManager(gateway=adapter, market="hk")
        
        # 根据当前时段决定：立即订阅 / 启动自动循环
        if self._subscription_mgr.should_be_subscribed():
            # 当前处于盘前或交易时段 → 立即订阅
            logger.info(f"[IntradayCoordinator] 当前为交易/盘前时段，立即订阅 {len(codes)} 只标的...")
            self._subscription_mgr.ensure_subscribed(codes)
        else:
            logger.info("[IntradayCoordinator] 当前非交易时段，暂不订阅 (自动循环将在盘前激活)")
        
        # 启动自动订阅/退订循环 — 盘前自动订阅，收盘自动退订释放槽位
        self._subscription_mgr.start_auto_loop(
            codes_callback=self._get_subscribe_codes,
            interval=60,
        )
        
        # 启动 Worker 线程
        self._worker_pool.start()
        
        self._running = True
        logger.info(f"盘中监控已启动 ({len(codes)} 只标的)")
    
    def stop(self):
        """停止盘中监控"""
        if not self._running:
            return
        
        # 停止分时段订阅自动循环
        if self._subscription_mgr:
            self._subscription_mgr.stop_auto_loop()
            # 收盘后退订释放槽位 (如果还在订阅状态)
            if self._subscription_mgr.subscribed_count > 0:
                logger.info(
                    f"[IntradayCoordinator] 停止时退订 {self._subscription_mgr.subscribed_count} 只标的..."
                )
                self._subscription_mgr.unsubscribe_all()
        
        # 停止行情 Worker 线程
        self._market_subscriber.stop_workers()
        
        # 停止 Worker 线程
        self._worker_pool.stop()
        
        # 断开连接
        self._market_subscriber.disconnect()
        
        self._running = False
        logger.info("盘中监控已停止")
    
    def restart(self):
        """重启盘中监控"""
        plan = self._battle_plan
        self.stop()
        time.sleep(1)
        self.start(plan)
    
    # ===== 数据流 =====
    
    def _on_quote_data(self, code: str, quote: dict):
        """行情数据回调 (已解析) → 入队到 worker_pool"""
        self._worker_pool.push_quote((code, quote))

    def _on_kline_data(self, code: str, kline: dict, k_type: str):
        """K 线数据回调 (已解析) → 入队到 worker_pool"""
        self._worker_pool.push_kline((code, kline, k_type))
    
    def _process_quote_data(self, data):
        """处理行情数据（Worker 线程中执行）"""
        code, quote = data
        if not code or not quote:
            return

        current_price = quote.get('last_price', 0) or quote.get('last_done', 0)
        if current_price <= 0:
            return

        # 1. 更新持仓价格 (仅当有持仓时)
        if self._pm:
            pos = self._pm.get_position(code)
            if pos:
                self._pm.update_price(code, current_price)

        # 2. 统一离场检查 (委托 DynamicExitManager 状态机)
        if self._position_manager and self._position_manager.get_open_positions_count() > 0:
            atr_val = self._signal_processor.get_latest_atr(code) if self._signal_processor else None
            self._position_manager.check_exits(code, current_price, atr_value=atr_val or 0)

    
    def _process_kline_data(self, data):
        """Process kline data (executed in Worker thread)"""
        code, kline, k_type = data
        if not code or not kline:
            return

        # 1. Update multi-timeframe trend
        if self._signal_processor:
            result = self._signal_processor.process_kline(code, kline, k_type)
            
            # Publish events for decoupled module communication
            try:
                from app.core.event_bus import event_bus, MarketEvent
                
                # Publish kline processed event
                event_bus.publish_async(
                    MarketEvent.KLINE_PROCESSED,
                    {
                        "code": code,
                        "k_type": k_type,
                        "close": kline.get("close", 0),
                        "trend_updated": result.get("trend_updated", False),
                        "new_trend": result.get("new_trend"),
                    },
                    source="intraday.coordinator"
                )
                
                # Publish trend update event if trend changed
                if result.get("trend_updated"):
                    event_bus.publish_async(
                        MarketEvent.TREND_UPDATED,
                        {
                            "code": code,
                            "k_type": k_type,
                            "new_trend": result.get("new_trend"),
                            "mtf_alignment": result.get("mtf_alignment"),
                        },
                        source="intraday.coordinator"
                    )
            except Exception as e:
                import logging
                logging.getLogger("ftseek.intraday.coordinator").debug(
                    f"Event publish failed: {e}"
                )

        # 仅在主K线周期驱动策略，避免对同一标的重复提交
        # confluence_v2 在 K_5M 上 return None (仅缓存), 需用 K_1M
        if self._strategies and k_type in ("K_1M",):
            # P0: 共享 K 线缓冲区 — SignalProcessor 为单一数据源, 策略通过引用访问
            shared_kline_buf = self._signal_processor.get_kline_buffer(code, k_type) if self._signal_processor else None

            # ── 港股分时段策略门控 (2026-04-30) ──────────────────
            # 根据当前时段过滤策略列表, 消除9策略全时段投票冗余。
            # 低耦合: TimeGate 独立模块, 只过滤不修改策略/共识逻辑。
            from app.strategy.time_gate import get_current_gate
            _gate = get_current_gate(cfg)
            if _gate.is_trading_hours and _gate.active_strategies:
                _active = [s for s in self._strategies if s.name in _gate.active_strategies]
                _skipped = [s.name for s in self._strategies if s.name not in _gate.active_strategies]
                if _skipped:
                    logger.debug(f"[TimeGate] {_gate.phase_name}: 跳过 {[s for s in _skipped]}")
            else:
                _active = self._strategies  # 无门控/非交易时段 → 全开放

            for strategy in _active:
                try:
                    signal = strategy.on_kline(code, kline, k_type, shared_buffer=shared_kline_buf)
                    if signal:
                        logger.debug(f"[策略信号] {strategy.name} -> {signal.code} {signal.action} @ {signal.price}")
                        if self._signal_processor:
                            self._signal_processor.submit_signal(signal)
                except Exception as e:
                    logger.error(f"[策略异常] {strategy.name}: {e}", exc_info=True)

    
    def _on_consensus_result(self, result):
        """处理共识结果 — 风控检查 + 执行路由"""
        code = result.code
        action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
        trace_id = getattr(result, 'trace_id', 'N/A')

        logger.info(
            f"[共识回调] [{trace_id}] {code} {action_str} | "
            f"得分={result.score:.2f} 阈值={result.threshold:.1f} | "
            f"置信度={result.confidence:.2f} | "
            f"同向={list(result.agree_strategies)} | 市场状态={result.market_regime}"
        )

        # ============================================================
        #  第 1 层: 轻量风控检查 (在 coordinator 层, 执行前)
        # ============================================================

        # 1a. 熔断器检查
        try:
            from app.risk.circuit_breaker import circuit_breaker as _cb
            if _cb.is_paused():
                logger.warning(f"[风控] [{trace_id}] {code} 熔断器已触发，拒绝执行")
                return
        except Exception as e:
            logger.debug(f"[风控] 熔断器检查异常: {e}")

        # 1b. 决策缓冲层检查 (去重 + 冷却 + 置信度)
        try:
            from app.decision_buffer import get_decision_buffer, DecisionStatus
            decision_buffer = get_decision_buffer()
            decision = decision_buffer.process_signal(result)
            if decision.status != DecisionStatus.APPROVED:
                logger.info(
                    f"[决策缓冲] [{trace_id}] {code} 被拒绝: {decision.rejection_reason}"
                )
                return
        except Exception as e:
            logger.warning(f"[决策缓冲] 检查异常，降级拒绝: {e}")
            return

        # 1c. 交易时段检查
        try:
            from app.utils.session_detector import SessionDetector
            from app.core.config_loader import cfg as _cfg
            _ta_cfg = _cfg.get("time_aware", {})
            if _ta_cfg:
                _sd = SessionDetector(_ta_cfg)
                session = _sd.get_current_session()
                if session.name == "CLOSED":
                    logger.warning(f"[风控] [{trace_id}] {code} 非交易时段 ({session.name})，拒绝执行")
                    return
        except Exception as e:
            logger.debug(f"[风控] 时段检查异常: {e}")

        # 1d. 风控 Pipeline (ADX一致性/弱信号/集中度/盈亏比/watchlist/趋势过滤等)
        if self._risk_pipeline:
            try:
                from app.risk.risk_pipeline import SignalContext
                ctx = SignalContext(consensus_result=result, trace_id=trace_id)
                pipeline_result = self._risk_pipeline.evaluate(ctx)
                if not pipeline_result.passed:
                    logger.info(
                        f"[风控Pipeline] [{trace_id}] {code} 被 [{pipeline_result.reject_check}] 拒绝: "
                        f"{pipeline_result.reject_reason}"
                    )
                    return
            except Exception as e:
                logger.warning(f"[风控Pipeline] 检查异常，降级拒绝: {e}")
                return

        # ============================================================
        #  第 2 层: 构建 Signal 并路由到执行层 (异步派发，不阻塞 kline-worker)
        # ============================================================
        try:
            from app.core.types import Signal, SignalAction, SignalSource

            # 从共识结果构造执行信号 (agree_strategies 去重)
            unique_agree = list(dict.fromkeys(result.agree_strategies))
            strategies_str = "+".join(unique_agree)
            consensus_signal = Signal(
                code=code,
                name=f"共识({strategies_str})",
                action=SignalAction.BUY if action_str == "BUY" else SignalAction.SELL,
                price=result.price,
                quantity=1,  # 执行层根据 position_sizer 动态计算
                source=SignalSource.TECHNICAL,
                reason="\n".join(getattr(result, 'reasons', [])),
                strategy=f"共识({strategies_str})",
                trace_id=trace_id,
                extra={
                    "consensus": True,
                    "score": result.score,
                    "threshold": result.threshold,
                    "market_regime": result.market_regime,
                    "adx": result.adx_value,
                    "agree_count": len(unique_agree),
                    "agree_strategies": unique_agree,
                    "confidence": result.confidence,
                    "scenario": getattr(result, 'scenario', 'unknown'),
                },
            )

            # 路由到交易执行器 (executor 内部有第 2 层风控: risk_manager.check_before_trade)
            if self._te:
                logger.info(
                    f"[执行路由] [{trace_id}] {code} {action_str} → TradeExecutor | "
                    f"价格={result.price} 共识分={result.score:.2f} | 策略={unique_agree}"
                )
                _signal = consensus_signal
                _notifier = self._notifier
                _pm = self._position_manager
                threading.Thread(
                    target=self._execute_trade_async,
                    args=(_signal, _notifier, _pm, trace_id),
                    daemon=True,
                    name=f"trade-{code}-{int(time.time())}",
                ).start()
            else:
                logger.warning(f"[执行路由] [{trace_id}] {code} 无 TradeExecutor，跳过执行")

        except Exception as e:
            logger.error(f"[执行路由] [{trace_id}] {code} 构造/执行信号异常: {e}", exc_info=True)

        # ============================================================
        #  第 3 层: 事件总线 + 持仓信号追踪 (轻量，不阻塞)
        # ============================================================
        # 通知持仓管理器 (用于信号追踪)
        if self._position_manager:
            try:
                self._position_manager.on_consensus_signal(result)
            except Exception as e:
                logger.debug(f"[持仓信号] 通知异常: {e}")

        # 发布共识事件到事件总线
        try:
            from app.core.event_bus import event_bus
            event_bus.publish("CONSENSUS_REACHED", {
                "code": code,
                "action": action_str,
                "score": result.score,
                "confidence": result.confidence,
            })
        except Exception as e:
            logger.debug(f"[事件总线] 发布共识事件失败: {e}")
    

    def _execute_trade_async(self, signal, notifier, position_manager, trace_id):
        """
        异步执行交易 + 推送通知 (在独立线程中运行，不阻塞 kline-worker)

        Args:
            signal: 共识信号
            notifier: 通知器实例
            position_manager: 持仓管理器
            trace_id: 链路追踪 ID
        """
        code = signal.code
        acquired = self._trade_semaphore.acquire(timeout=60)
        if not acquired:
            logger.warning(f"[交易并发] [{trace_id}] {code} 等待超时(60s)，跳过执行")
            return
        try:
            trade_result = self._te.execute(signal)
            logger.info(
                f"[执行回报] [{trace_id}] {code} {'成功' if trade_result.success else '失败'}: "
                f"{trade_result.message}"
            )

            # 执行成功后推送通知
            if trade_result.success and notifier:
                try:
                    position = self._pm.get_position(code) if self._pm else None
                    tr_type = getattr(trade_result, 'trade_type', None) or 'OPEN'
                    if tr_type == 'CLOSE':
                        notifier.send_close_position_message(
                            signal=signal,
                            trade_result=trade_result,
                            position=position,
                            consensus_result=None,
                        )
                        logger.info(f"[推送通知] {code} 平仓通知已发送")
                    elif tr_type == 'ADD':
                        notifier.send_open_position_message(
                            signal=signal,
                            trade_result=trade_result,
                            position=position,
                            consensus_result=None,
                        )
                        logger.info(f"[推送通知] {code} 补仓通知已发送")
                    else:
                        notifier.send_open_position_message(
                            signal=signal,
                            trade_result=trade_result,
                            position=position,
                            consensus_result=None,
                        )
                        logger.info(f"[推送通知] {code} 开仓通知已发送")
                except Exception as e:
                    logger.error(f"[推送通知] {code} 发送失败: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[异步执行] [{trace_id}] {code} 异常: {e}", exc_info=True)
        finally:
            self._trade_semaphore.release()

    # ===== 辅助方法 =====
    
    def _get_subscribe_codes(self) -> List[str]:
        """获取订阅标的列表"""
        if self._battle_plan:
            # 使用盘前计划
            return [t["symbol"] for t in self._battle_plan.targets if "symbol" in t]
        return cfg.get("trading.watch_list_hk", [])

    # ===== 状态查询 =====
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    def get_health_status(self) -> Dict[str, Any]:
        """获取健康状态"""
        status = {
            "running": self._running,
            "battle_plan": self._battle_plan.date if self._battle_plan else None,
        }
        
        if self._worker_pool:
            status["worker_pool"] = self._worker_pool.check_health()
        
        if self._market_subscriber:
            status["market_subscriber"] = {
                "connected": self._market_subscriber.is_connected,
                "subscribed": self._market_subscriber.is_subscribed,
            }
        
        # ── 分时段订阅管理器状态 ──
        if self._subscription_mgr:
            status["subscription_manager"] = self._subscription_mgr.get_status()
        
        return status

    def _warm_up_strategy_buffers(self, codes: list):
        """
        P0 修复 2026-04-30: 启动预热 — 从 Futu 拉取历史 K 线填充策略 Buffer

        目标:
            系统重启或隔夜后, 内存中的 K 线 Buffer 为空。
            confluence_v2 需要 52+ 根 K_5M 才能产生信号 (~4.3 小时)。
            本方法拉取最近 3 天的 K 线, 使策略立即可响应。

        策略:
            1. 对每个标的拉取 K_5M (主驱动周期) 和 K_15M/K_60M
            2. 填充 SignalProcessor._kline_buffers (deque)
            3. 填充各 strategy._buffers (IndicatorBuffer)
            4. 触发 MTF 趋势计算
        """
        import time
        from datetime import datetime, timedelta
        from futu import KLType

        if not codes:
            logger.warning("[预热] 无标的, 跳过")
            return

        logger.info(f"[预热] 开始拉取 {len(codes)} 只股票历史数据...")

        # 获取 quote context
        try:
            ctx = self._market_subscriber.get_quote_context()
        except Exception as e:
            logger.error(f"[预热] 获取行情上下文失败: {e}")
            return

        # 只拉取关键周期
        warmup_targets = [
            (KLType.K_5M, "K_5M"),
            (KLType.K_15M, "K_15M"),
            (KLType.K_60M, "K_60M"),
        ]

        success_count = 0
        fail_count = 0
        start_time = time.time()

        for code in codes:
            try:
                end_time = datetime.now().strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

                for kl_type, key in warmup_targets:
                    ret, data, _ = ctx.request_history_kline(
                        code, start=start_date, end=end_time, ktype=kl_type, autype="qfq"
                    )
                    if ret != 0 or data is None or data.empty:
                        continue

                    # 推入 SignalProcessor._kline_buffers (deque)
                    if self._signal_processor:
                        for _, row in data.iterrows():
                            kline_dict = {
                                "time_key": str(row.get("time_key", "")),
                                "open": float(row.get("open", 0)),
                                "high": float(row.get("high", 0)),
                                "low": float(row.get("low", 0)),
                                "close": float(row.get("close", 0)),
                                "volume": float(row.get("volume", 0)),
                            }
                            self._signal_processor.update_mtf_trend(code, key, kline_dict)

                    # 推入各策略的 IndicatorBuffer
                    if self._strategies:
                        for strategy in self._strategies:
                            if hasattr(strategy, "_buffers") and hasattr(strategy, "on_kline"):
                                for _, row in data.iterrows():
                                    kline_dict = {
                                        "time_key": str(row.get("time_key", "")),
                                        "open": float(row.get("open", 0)),
                                        "high": float(row.get("high", 0)),
                                        "low": float(row.get("low", 0)),
                                        "close": float(row.get("close", 0)),
                                        "volume": float(row.get("volume", 0)),
                                    }
                                    # 直接推入 buffer (不触发 on_kline, 避免重复信号)
                                    buf = strategy._buffers[code]
                                    if hasattr(buf, "push_kline"):
                                        buf.push_kline(kline_dict)

                                # 单独处理 5M 周期一致性 buffer (confluence_v2)
                                if key == "K_5M" and hasattr(strategy, "_buffers_5m"):
                                    for _, row in data.iterrows():
                                        kline_dict = {
                                            "time_key": str(row.get("time_key", "")),
                                            "open": float(row.get("open", 0)),
                                            "high": float(row.get("high", 0)),
                                            "low": float(row.get("low", 0)),
                                            "close": float(row.get("close", 0)),
                                            "volume": float(row.get("volume", 0)),
                                        }
                                        buf5 = strategy._buffers_5m[code]
                                        if hasattr(buf5, "push_kline"):
                                            buf5.push_kline(kline_dict)

                    success_count += 1
                    time.sleep(0.15)  # 限流, 避免 OpenD 限频

            except Exception as e:
                fail_count += 1
                logger.warning(f"[预热] {code} 失败: {e}")

        elapsed = time.time() - start_time
        logger.info(
            f"[预热] 完成: 成功填充 {success_count} 组数据, {fail_count} 组失败, "
            f"耗时 {elapsed:.1f}s"
        )

        # 报告预热后 buffer 状态
        if self._signal_processor and codes:
            sample_code = codes[0]
            for key in ["K_5M", "K_15M", "K_60M"]:
                buf = self._signal_processor._kline_buffers.get(sample_code, {}).get(key, [])
                logger.debug(f"[预热] {sample_code} {key} buffer size = {len(buf)}")
