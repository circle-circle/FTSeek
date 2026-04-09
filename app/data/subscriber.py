"""
主订阅管理器
负责行情订阅、回调处理、止盈止损检查
"""
import logging
import threading
import time
from datetime import datetime
from typing import List

from app import config
from app.strategy.base import BaseStrategy, Signal
from app.notifier.dingtalk import DingTalkNotifier
from app.trader.executor import TradeExecutor
from app.trader.position_manager import PositionManager
from app.strategy.hk_specific import get_hk_adapter, HKMarketConfig
from app.risk.regime_params_loader import params_loader as regime_params_loader
from app.data.hsi_data import hsi_fetcher
from app.risk.circuit_breaker import CircuitBreaker
from app.strategy.consensus_engine import ConsensusEngine, AdaptiveWeightManager
from app.strategy.market_regime import MarketRegimeDetector
from app.data.consensus_trigger import ConsensusTrigger
from app.data.kline_processor import get_kline_processor

logger = logging.getLogger("ftseek.data.subscriber")
logger.propagate = False

# 推送冷却
SIGNAL_COOLDOWN = int(getattr(config, "SIGNAL_COOLDOWN", 120))

# 共识引擎参数
CONSENSUS_WINDOW = int(getattr(config, "CONSENSUS_WINDOW", 60))
CONSENSUS_THRESHOLD = float(getattr(config, "CONSENSUS_THRESHOLD", 1.5))
CONSENSUS_ADX_TREND = float(getattr(config, "CONSENSUS_ADX_TREND", 25))
CONSENSUS_ADX_RANGE = float(getattr(config, "CONSENSUS_ADX_RANGE", 20))


class DataSubscriber:
    """富途行情订阅管理器 (加权共识模式)"""
    
    def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier):
        """
        初始化数据订阅器
        
        Args:
            strategies: 策略列表
            notifier: 通知器
        """
        self.strategies = strategies
        self.notifier = notifier
        self._quote_ctx = None
        self._running = False
        
        # 行情心跳
        self._last_quote_time = float(time.time())
        self._reconnect_lock = threading.Lock()
        
        # 交易执行器和持仓管理器
        self.trade_executor = TradeExecutor()
        self.position_manager = PositionManager()
        
        # 加权共识引擎
        self._consensus = ConsensusEngine(
            window_seconds=CONSENSUS_WINDOW,
            threshold=CONSENSUS_THRESHOLD,
            adx_trend=CONSENSUS_ADX_TREND,
            adx_range=CONSENSUS_ADX_RANGE,
            on_consensus=self._on_consensus_callback,
        )
        
        # 自适应权重管理器
        self._adaptive_weight_manager = AdaptiveWeightManager(self._consensus)
        logger.info("自适应权重管理器已初始化")
        
        # 市场状态检测器
        self._regime_detector = MarketRegimeDetector()
        logger.info("MarketRegimeDetector 已初始化")
        
        # 港股特性适配器
        self.hk_adapter = get_hk_adapter(HKMarketConfig(
            t0_trading=True,
            no_price_limit=True,
            short_bias_factor=1.2,
        ))
        logger.info("港股特性适配器已初始化")
        
        # 熔断器
        self.circuit_breaker = CircuitBreaker(
            reject_limit=5,
            pause_duration=600,
            drawdown_limit=0.15,
        )
        logger.info("熔断器已初始化")
        
        # 市场状态参数加载器
        self.params_loader = regime_params_loader
        self.hsi_fetcher = hsi_fetcher
        self._current_regime = 'default'
        self._last_regime_check = 0
        logger.info("市场状态参数加载器已初始化")
        
        # K 线处理器 (单例)
        self.kline_processor = get_kline_processor()
        
        # 初始化共识触发器
        self.consensus_trigger = ConsensusTrigger(
            position_manager=self.position_manager,
            trade_executor=self.trade_executor,
            notifier=self.notifier,
            consensus_engine=self._consensus,
            adaptive_weight_manager=self._adaptive_weight_manager,
            regime_detector=self._regime_detector,
            circuit_breaker=self.circuit_breaker,
            hk_adapter=self.hk_adapter,
            params_loader=self.params_loader,
            hsi_fetcher=self.hsi_fetcher,
        )
        logger.info("共识触发器已初始化")
    
    def _get_quote_ctx(self):
        """获取或创建行情上下文"""
        if self._quote_ctx is None:
            import logging as stdlib_logging
            
            # 配置 futu 日志
            futu_logger = stdlib_logging.getLogger("futu")
            futu_logger.setLevel(stdlib_logging.ERROR)
            
            for handler in futu_logger.handlers[:]:
                if isinstance(handler, stdlib_logging.FileHandler):
                    futu_logger.removeHandler(handler)
            
            try:
                from futu import OpenQuoteContext
                self._quote_ctx = OpenQuoteContext(
                    host=config.OPEND_HOST, port=config.OPEND_PORT
                )
                logger.info("行情上下文已创建")
            except Exception as e:
                logger.error(f"创建行情上下文失败：{e}")
                raise
        
        return self._quote_ctx
    
    def _on_consensus_callback(self, result):
        """共识回调 - 委托给共识触发器处理"""
        self.consensus_trigger.on_consensus(result)
    
    def _process_signal(self, signal: Signal):
        """处理策略信号"""
        self._consensus.add_vote(signal)
    
    def subscribe(self):
        """订阅行情"""
        from futu import SubType
        
        ctx = self._get_quote_ctx()
        
        sub_type_map = {
            "QUOTE": SubType.QUOTE,
            "K_1M": SubType.K_1M,
            "K_3M": SubType.K_3M,
            "K_5M": SubType.K_5M,
            "K_15M": SubType.K_15M,
            "K_30M": SubType.K_30M,
            "K_60M": SubType.K_60M,
            "K_DAY": SubType.K_DAY,
        }
        
        sub_types = [sub_type_map[st] for st in config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]
        
        for code in config.WATCH_LIST:
            for st in sub_types:
                ret, msg = ctx.subscribe([code], [st])
                if ret == 0:
                    logger.info(f"订阅成功：{code} -> {st}")
                else:
                    if "无权限" in str(msg) or "no permission" in str(msg).lower():
                        logger.warning(f"订阅跳过 (无权限): {code} -> {st} | {msg}")
                    else:
                        logger.error(f"订阅失败：{code} -> {st}: {msg}")
    
    def _setup_handlers(self):
        """设置行情回调处理器"""
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase
        
        strategies = self.strategies
        process_signal = self._process_signal
        subscriber = self
        
        class MyQuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_str):
                ret, data = super().on_recv_rsp(rsp_str)
                if ret != 0:
                    return
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    quote = row.to_dict()
                    
                    # 更新行情心跳时间
                    subscriber._last_quote_time = time.time()
                    
                    # 止盈止损检查
                    current_price = quote.get('last_done', 0)
                    if current_price > 0:
                        pos_now = subscriber.position_manager.get_position(code)
                        
                        # 更新峰值价格缓存
                        if pos_now:
                            if code not in subscriber._peak_price_cache:
                                subscriber._peak_price_cache[code] = current_price
                            else:
                                if pos_now.action == 'BUY':
                                    subscriber._peak_price_cache[code] = max(
                                        subscriber._peak_price_cache[code], current_price
                                    )
                                else:
                                    subscriber._peak_price_cache[code] = min(
                                        subscriber._peak_price_cache[code], current_price
                                    )
                        else:
                            subscriber._peak_price_cache.pop(code, None)
                        
                        # 组合式退出引擎检查
                        if pos_now:
                            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem_exit
                            exit_decision = _dem_exit.check_exit_priority(
                                code=code,
                                entry_price=pos_now.entry_price,
                                current_price=current_price,
                                peak_price=subscriber._peak_price_cache.get(code, current_price),
                                entry_time=getattr(pos_now, 'entry_time', None),
                                atr_value=subscriber._get_atr(code),
                                direction=pos_now.action,
                            )
                            if exit_decision.should_exit:
                                logger.info(
                                    f"[组合退出-P{exit_decision.priority}] {code} "
                                    f"{exit_decision.reason} | ratio={exit_decision.close_ratio:.0%}"
                                )
                                if exit_decision.close_ratio < 1.0:
                                    subscriber._execute_partial_take_profit(code, current_price, exit_decision.close_ratio)
                                elif exit_decision.is_stop_loss:
                                    subscriber._execute_stop_loss(code, current_price)
                                    subscriber.consensus_trigger._trailing_stop_cache.pop(code, None)
                                    subscriber._peak_price_cache.pop(code, None)
                                else:
                                    subscriber._execute_take_profit(code, current_price)
                                    subscriber.consensus_trigger._trailing_stop_cache.pop(code, None)
                                    subscriber._peak_price_cache.pop(code, None)
                                continue
                        
                        # 保本止损检查
                        if subscriber.position_manager.check_breakeven_stop(code, current_price):
                            subscriber.position_manager.update_breakeven_stop(code)
                        
                        # 分批止盈检查
                        partial_ratio = subscriber.position_manager.check_partial_take_profit(code, current_price)
                        if partial_ratio > 0:
                            logger.info(f"[分批止盈] {code} 当前价={current_price} 平仓比例={partial_ratio:.0%}")
                            subscriber._execute_partial_take_profit(code, current_price, partial_ratio)
                            if partial_ratio >= 1.0:
                                continue
                        
                        # 止损检查
                        if subscriber.position_manager.check_stop_loss(code, current_price):
                            logger.info(f"[止损触发] {code} 当前价={current_price}")
                            subscriber._execute_stop_loss(code, current_price)
                            continue
                        
                        # 止盈检查
                        if subscriber.position_manager.check_take_profit(code, current_price):
                            logger.info(f"[止盈触发] {code} 当前价={current_price}")
                            subscriber._execute_take_profit(code, current_price)
                            continue
                    
                    # 策略计算
                    for strategy in strategies:
                        if strategy.name in ('price_break', 'fusion_v3'):
                            sig = strategy.on_quote(code, quote)
                            if sig:
                                process_signal(sig)
        
        class MyKlineHandler(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_str):
                ret, data = super().on_recv_rsp(rsp_str)
                if ret != 0:
                    return
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    kline = row.to_dict()
                    k_type = kline.get("k_type", "K_1M")
                    
                    # 多周期趋势更新
                    subscriber.kline_processor.update_multi_timeframe_trend(code, k_type, kline)
                    
                    # MarketRegimeDetector 接入多周期数据
                    if k_type in ("K_15M", "K_30M", "K_60M"):
                        try:
                            subscriber._regime_detector.update_data(code, kline)
                        except Exception as _re:
                            logger.debug(f"[regime 多周期] {code} {k_type} 更新失败：{_re}")
                    
                    # 止盈止损检查
                    current_price = kline.get('close', 0)
                    if current_price > 0:
                        pos_now = subscriber.position_manager.get_position(code)
                        
                        # 追踪止盈检查
                        if pos_now:
                            trail_price = subscriber.consensus_trigger._trailing_stop_cache.get(code, 0)
                            if trail_price > 0:
                                triggered = (
                                    (pos_now.action == 'BUY' and current_price < trail_price) or
                                    (pos_now.action == 'SELL' and current_price > trail_price)
                                )
                                if triggered:
                                    pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
                                    if pos_now.action == 'SELL':
                                        pnl_rate = -pnl_rate
                                    
                                    if pnl_rate > 0:
                                        logger.info(
                                            f"[追踪止盈触发] {code} 价格={current_price} "
                                            f"穿越追踪线={trail_price:.3f} | 盈利={pnl_rate:.2%}"
                                        )
                                        subscriber._execute_partial_take_profit(code, current_price, ratio=0.6)
                                        subscriber.position_manager.update_breakeven_stop(code)
                                        logger.info(
                                            f"[追踪止盈] {code} 已平仓 60% 锁利，剩余 40% 保本止损已激活"
                                        )
                                        subscriber.consensus_trigger._trailing_stop_cache.pop(code, None)
                                        subscriber._peak_price_cache.pop(code, None)
                                        subscriber.consensus_trigger._trailing_exit_time[code] = time.time()
                                    continue
                        
                        # 保本止损检查
                        if subscriber.position_manager.check_breakeven_stop(code, current_price):
                            subscriber.position_manager.update_breakeven_stop(code)
                        
                        # 分批止盈检查
                        partial_ratio = subscriber.position_manager.check_partial_take_profit(code, current_price)
                        if partial_ratio > 0:
                            logger.info(f"[分批止盈] {code} 当前价={current_price} 平仓比例={partial_ratio:.0%}")
                            subscriber._execute_partial_take_profit(code, current_price, partial_ratio)
                            if partial_ratio >= 1.0:
                                continue
                        
                        # 止损检查
                        if subscriber.position_manager.check_stop_loss(code, current_price):
                            logger.info(f"[止损触发] {code} 当前价={current_price}")
                            subscriber._execute_stop_loss(code, current_price)
                            continue
                        
                        # 止盈检查
                        if subscriber.position_manager.check_take_profit(code, current_price):
                            logger.info(f"[止盈触发] {code} 当前价={current_price}")
                            subscriber._execute_take_profit(code, current_price)
                            continue
                    
                    # 更新 ADX
                    subscriber._update_adx(code, kline)
                    
                    # 策略计算 (仅在 K_1M 上触发)
                    if k_type == "K_1M":
                        for strategy in strategies:
                            sig = strategy.on_kline(code, kline, k_type)
                            if sig:
                                process_signal(sig)
        
        ctx = self._get_quote_ctx()
        ctx.set_handler(MyQuoteHandler())
        ctx.set_handler(MyKlineHandler())
        logger.info("行情回调 handler 已设置")
    
    def _update_adx(self, code: str, kline: dict):
        """更新 ADX 值"""
        self.kline_processor.push_kline(code, kline, "K_1M")
        self.kline_processor.calculate_adx(code)
    
    def _get_atr(self, code: str) -> float:
        """获取 ATR 值"""
        return self.kline_processor.calculate_atr(code)
    
    def _execute_stop_loss(self, code: str, current_price: float):
        """执行止损"""
        # 委托给 exit_manager 处理
        from app.data.exit_manager import create_exit_manager
        exit_manager = create_exit_manager(self.position_manager, self.trade_executor)
        exit_manager.execute_stop_loss(code, current_price)
    
    def _execute_take_profit(self, code: str, current_price: float):
        """执行止盈"""
        from app.data.exit_manager import create_exit_manager
        exit_manager = create_exit_manager(self.position_manager, self.trade_executor)
        exit_manager.execute_take_profit(code, current_price)
    
    def _execute_partial_take_profit(self, code: str, current_price: float, ratio: float):
        """执行分批止盈"""
        from app.data.exit_manager import create_exit_manager
        exit_manager = create_exit_manager(self.position_manager, self.trade_executor)
        exit_manager.execute_partial_take_profit(code, current_price, ratio)
    
    def _reconnect(self):
        """断线重连"""
        if not self._reconnect_lock.acquire(blocking=False):
            logger.warning("[重连] 已有重连任务进行中，跳过")
            return
        try:
            logger.info("[重连] 检测到行情断线，开始自动重连...")
            
            # 关闭旧连接
            try:
                if self._quote_ctx is not None:
                    self._quote_ctx.close()
            except Exception:
                pass
            finally:
                self._quote_ctx = None
            
            # 等待后重建
            time.sleep(2)
            
            # 重新连接 (指数退避)
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    self.subscribe()
                    self._setup_handlers()
                    self._last_quote_time = time.time()
                    logger.info(f"[重连] 第 {attempt} 次重连成功，行情已恢复")
                    return
                except Exception as e:
                    logger.error(f"[重连] 第 {attempt} 次重连失败：{e}")
                    try:
                        if self._quote_ctx is not None:
                            self._quote_ctx.close()
                    except Exception:
                        pass
                    self._quote_ctx = None
                    time.sleep(min(5 * attempt, 30))
            
            logger.error("[重连] 达到最大重试次数，行情断线未恢复，请人工检查")
        finally:
            self._reconnect_lock.release()
    
    def start(self):
        """启动订阅"""
        self._running = True
        try:
            self.subscribe()
            self._setup_handlers()
            logger.info(
                f"数据订阅已启动 | 共识窗口={CONSENSUS_WINDOW}s | "
                f"触发阈值={CONSENSUS_THRESHOLD} | "
                f"ADX 趋势>{CONSENSUS_ADX_TREND} 震荡<{CONSENSUS_ADX_RANGE} | "
                f"推送冷却={SIGNAL_COOLDOWN}s"
            )
        except Exception as e:
            logger.error(f"数据订阅启动异常：{e}")
            raise
        
        # 启动清理循环
        def cleanup_loop():
            HEARTBEAT_TIMEOUT = 180
            while self._running:
                time.sleep(60)
                from app.signal_store import signal_store
                signal_store.cleanup_expired()
                self._cleanup_cooldown()
                
                # 心跳检测
                if self._is_trading_hours():
                    elapsed = time.time() - self._last_quote_time
                    if elapsed > HEARTBEAT_TIMEOUT:
                        logger.warning(
                            f"[心跳] {elapsed:.0f}s 未收到行情数据，"
                            f"疑似断线，启动重连..."
                        )
                        threading.Thread(
                            target=self._reconnect, daemon=True
                        ).start()
        
        t = threading.Thread(target=cleanup_loop, daemon=True)
        t.start()
    
    def _is_trading_hours(self) -> bool:
        """检查是否为交易时间"""
        from app.data.trading_hours import get_trading_hours_manager
        manager = get_trading_hours_manager()
        return manager.is_trading_hours()
    
    def _cleanup_cooldown(self):
        """清理过期冷却记录"""
        from app.data.trading_hours import get_trading_hours_manager
        manager = get_trading_hours_manager()
        manager.cleanup_cooldown()
    
    def _resubscribe(self):
        """重新订阅行情"""
        try:
            if self._quote_ctx is None:
                logger.error("[重新订阅] 行情上下文为 None")
                return
            
            from futu import SubType
            sub_type_map = {
                "QUOTE": SubType.QUOTE,
                "K_1M": SubType.K_1M,
                "K_3M": SubType.K_3M,
                "K_5M": SubType.K_5M,
                "K_15M": SubType.K_15M,
                "K_30M": SubType.K_30M,
                "K_60M": SubType.K_60M,
                "K_DAY": SubType.K_DAY,
            }
            
            sub_types = [sub_type_map[st] for st in config.SUBSCRIBE_TYPES if st in sub_type_map]
            if not sub_types:
                sub_types = [SubType.QUOTE]
            
            for code in config.WATCH_LIST:
                for st in sub_types:
                    ret, msg = self._quote_ctx.subscribe([code], [st])
                    if ret == 0:
                        logger.info(f"重新订阅成功：{code} -> {st}")
                    else:
                        logger.error(f"重新订阅失败：{code} -> {st}: {msg}")
            
            logger.info("[重新订阅] 完成")
        except Exception as e:
            logger.error(f"[重新订阅] 异常：{e}")
    
    def stop(self):
        """停止订阅"""
        self._running = False
        if self._quote_ctx is not None:
            try:
                self._quote_ctx.close()
            except Exception:
                pass
            self._quote_ctx = None
        logger.info("数据订阅已停止")
    
    def get_snapshot(self, codes=None):
        """获取行情快照"""
        if self._quote_ctx is None:
            return None
        
        target_codes = codes or config.WATCH_LIST
        try:
            ret, data = self._quote_ctx.get_market_snapshot(target_codes)
            if ret == 0:
                return data
            else:
                logger.error(f"获取快照失败：{data}")
                return None
        except Exception as e:
            logger.error(f"获取快照异常：{e}")
            return None


# 工厂函数
def create_subscriber(strategies: List[BaseStrategy], notifier: DingTalkNotifier) -> DataSubscriber:
    """
    创建数据订阅器实例
    
    Args:
        strategies: 策略列表
        notifier: 通知器
    
    Returns:
        DataSubscriber 实例
    """
    return DataSubscriber(strategies, notifier)
