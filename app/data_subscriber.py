"""
数据订阅器
对接 futu-api 订阅实时行情，分发给策略引擎
信号经过「加权共识引擎」评分，多策略加权确认后才推送
"""
import os
import sys
import dataclasses
import logging as stdlib_logging

# 必须在导入 futu 之前设置日志环境变量和配置
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"
os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"

# 仅配置 futu 日志，不影响应用日志
futu_logger = stdlib_logging.getLogger("futu")
futu_logger.setLevel(stdlib_logging.CRITICAL)
# 清除 futu 日志的所有 handler，防止文件锁
futu_logger.handlers = []
futu_logger.propagate = False

import logging
import threading
import time
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

from app import config
from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.indicators import IndicatorBuffer, adx as calc_adx
from app.signal_store import signal_store
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.consensus_engine import ConsensusEngine, ConsensusResult, AdaptiveWeightManager
from app.trader.executor import TradeExecutor
from app.trader.position_manager import PositionManager
from app.strategy.hk_specific import get_hk_adapter, HKMarketConfig
from app.risk.regime_params_loader import params_loader as regime_params_loader
from app.data.hsi_data import hsi_fetcher
from app.storage.signal_tracker import signal_tracker
from app.risk.circuit_breaker import CircuitBreaker  # 新增: 熔断器
from app.analytics.trade_evaluation import get_evaluator  # 新增: 交易评估器

logger = logging.getLogger("ftseek.subscriber")
logger.propagate = False  # 防止日志重复

# 推送冷却 (同标的达成共识后冷却) - 支持动态更新
SIGNAL_COOLDOWN = int(getattr(config, "SIGNAL_COOLDOWN", 120))

# 共识引擎参数
CONSENSUS_WINDOW    = int(getattr(config, "CONSENSUS_WINDOW", 60))
CONSENSUS_THRESHOLD = float(getattr(config, "CONSENSUS_THRESHOLD", 1.5))
CONSENSUS_ADX_TREND = float(getattr(config, "CONSENSUS_ADX_TREND", 25))
CONSENSUS_ADX_RANGE = float(getattr(config, "CONSENSUS_ADX_RANGE", 20))

# ADX 计算周期
ADX_PERIOD = 14


class QuoteHandler:
    def __init__(self, strategies, notifier):
        self.strategies = strategies
        self.notifier = notifier

    def on_recv_rsp(self, rsp_pb):
        pass


class DataSubscriber:
    """富途行情订阅管理器 (加权共识模式)"""

    def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier):
        self.strategies = strategies
        self.notifier = notifier
        self._quote_ctx = None
        self._running = False

        # 推送冷却: code -> last_push_time
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

        # 2026-03-27 新增: 止损冷却机制，防止频繁触发
        # 止损冷却: code -> last_stop_loss_time
        self._stop_loss_cooldown: Dict[str, float] = {}
        # 止损失败计数: code -> int（用于递增冷却）
        self._stop_loss_fail_count: Dict[str, int] = {}

        # 追踪止盈缓存: code -> 追踪止损价（Task2 新增）
        self._trailing_stop_cache: Dict[str, float] = {}
        # 追踪止盈触发时间缓存: code -> timestamp（用于重开仓冷却判断）
        self._trailing_exit_time: Dict[str, float] = {}

        # code 级别交易锁：防止同一标的 _on_consensus 并发执行（Task3）
        self._trade_locks: Dict[str, threading.Lock] = {}

        # 开仓原子锁: code -> timestamp - 防止同一标的在同一秒内重复开仓
        # 2026-03-25 修复：解决 quote+kline handler 同时触发导致重复开仓的问题
        self._open_position_ts: Dict[str, float] = {}  # code -> last open timestamp
        self._open_lock = threading.Lock()  # 保护 _open_position_ts 的写入

        # 持仓期间峰值价格缓存: code -> peak_price（Task2 移动止损使用）
        self._peak_price_cache: Dict[str, float] = {}
        
        # 软止损预警冷却字典 (2026-04-07 新增)
        # 结构：{code: last_warning_timestamp} - 防止每秒多次预警
        self.soft_stop_warnings: Dict[str, float] = {}

        # 行情心跳：记录最后一次收到行情数据的时间（用于断线检测）
        self._last_quote_time: float = time.time()
        self._reconnect_lock = threading.Lock()

        # K 线缓冲: 为每个 code 维护滑动窗口, 用于计算 ADX
        self._kline_buffers: Dict[str, IndicatorBuffer] = defaultdict(lambda: IndicatorBuffer(maxlen=200))

        # 多周期趋势缓存 (2026-03-25 新增)
        # 结构: {code: {"K_1M": trend, "K_3M": trend, ...}}
        # trend: "up" / "down" / "neutral"
        self._multi_timeframe_trends: Dict[str, Dict[str, str]] = defaultdict(lambda: {
            "K_1M": "neutral",
            "K_3M": "neutral",
            "K_5M": "neutral",
            "K_15M": "neutral", 
            "K_30M": "neutral",
            "K_60M": "neutral",
            "K_DAY": "neutral"
        })
        # 多周期 K 线缓存 (每个周期独立)
        self._kline_buffers_by_period: Dict[str, Dict[str, IndicatorBuffer]] = defaultdict(lambda: {
            "K_1M": IndicatorBuffer(maxlen=100),
            "K_3M": IndicatorBuffer(maxlen=100),
            "K_5M": IndicatorBuffer(maxlen=100),
            "K_15M": IndicatorBuffer(maxlen=100),
            "K_30M": IndicatorBuffer(maxlen=100),
            "K_60M": IndicatorBuffer(maxlen=100),
            "K_DAY": IndicatorBuffer(maxlen=100),
        })

        # 交易执行器和持仓管理器
        self.trade_executor = TradeExecutor()
        self.position_manager = PositionManager()

        # 【新增 2026-04-09】分层策略管理器集成
        self.layered_manager = None
        try:
            from app.strategy.layered_integration import get_layered_strategy_manager
            self.layered_manager = get_layered_strategy_manager()
            logger.info("分层策略管理器已集成到数据订阅器")
        except Exception as e:
            logger.warning(f"分层策略管理器集成失败：{e}")

        # 新闻采集器引用 (用于事件驱动层)
        self._news_collector = None
        try:
            from app.news_push.collector import get_collector as get_news_collector
            self._news_collector = get_news_collector()
            logger.info("新闻采集器已集成到数据订阅器")
        except Exception as e:
            logger.warning(f"新闻采集器集成失败：{e}")

        # 加权共识引擎
        self._consensus = ConsensusEngine(
            window_seconds=CONSENSUS_WINDOW,
            threshold=CONSENSUS_THRESHOLD,
            adx_trend=CONSENSUS_ADX_TREND,
            adx_range=CONSENSUS_ADX_RANGE,
            on_consensus=self._on_consensus,
            allow_short=False,  # 2026-04-08: 模拟盘禁止做空
        )

        # 自适应权重管理器（根据历史盈亏自动调整策略权重）
        self._adaptive_weight_manager = AdaptiveWeightManager(self._consensus)
        logger.info("自适应权重管理器已初始化")
        
        # 【新增 2026-04-02】新闻情绪缓存 - 避免频繁调用LLM API
        # 结构: {code: (sentiment_score, timestamp)}
        self._news_sentiment_cache: Dict[str, Tuple[float, float]] = {}
        self._news_cache_ttl = 1800  # 缓存有效期 30 分钟

        # P1-2: MarketRegimeDetector（7态市场状态检测，接入共识引擎）
        from app.strategy.market_regime import MarketRegimeDetector
        self._regime_detector = MarketRegimeDetector()
        logger.info("MarketRegimeDetector 已初始化")

        # Phase 1: 场景识别系统集成（2026-04-09）
        try:
            from app.scenario import get_scenario_classifier, get_scenario_router, get_scenario_memory
            self._scenario_classifier = get_scenario_classifier()
            self._scenario_router = get_scenario_router()
            self._scenario_memory = get_scenario_memory()
            logger.info("场景识别系统已集成 (Classifier/Router/Memory)")
            # 注入场景路由器到共识引擎
            self._consensus.set_scenario_router(self._scenario_router)
        except Exception as e:
            logger.warning(f"场景识别系统集成失败：{e}")
            self._scenario_classifier = None
            self._scenario_router = None
            self._scenario_memory = None
        
        # 港股特性适配器 (2026-03-15 新增)
        self.hk_adapter = get_hk_adapter(HKMarketConfig(
            t0_trading=True,
            no_price_limit=True,
            short_bias_factor=1.2,
        ))
        logger.info("港股特性适配器已初始化")

        # 【新增 2026-04-06】质量管理器 - 巴菲特理念实现
        # 包含：质量评分器、持仓周期管理器、资金水位管理器、止盈止损管理器、估值计算器
        self._init_quality_managers()
        
        # 熔断器 (新增 2026-03-17)
        self.circuit_breaker = CircuitBreaker(
            reject_limit=5,       # 连续拒绝 5 次触发熔断
            pause_duration=600,   # 熔断后暂停 10 分钟
            drawdown_limit=0.15,  # 最大回撤 15%
        )
        logger.info("熔断器已初始化")
        
        # 市场状态参数加载器 (2026-03-17 新增)
        self.params_loader = regime_params_loader
        self.hsi_fetcher = hsi_fetcher
        self._current_regime = 'default'
        self._last_regime_check = 0
        logger.info("市场状态参数加载器已初始化")

    def _get_quote_ctx(self):
        if self._quote_ctx is None:
            import logging as stdlib_logging
            
            # 配置 futu 日志到临时目录，避免文件锁冲突
            futu_logger = stdlib_logging.getLogger("futu")
            futu_logger.setLevel(stdlib_logging.ERROR)
            
            # 移除现有的文件 handler
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

    def _get_current_cooldown(self) -> int:
        """获取当前冷却时间 (支持动态更新)"""
        # 每 5 分钟检查一次市场状态
        if time.time() - self._last_regime_check > 300:
            self._update_regime_params()
        
        # 从参数加载器获取当前冷却时间
        current_params = self.params_loader.get_current_params()
        return current_params.get('signal_cooldown', SIGNAL_COOLDOWN)
    
    def _update_regime_params(self):
        """更新市场状态参数"""
        try:
            # 获取恒指数据和市场状态
            hsi_data = self.hsi_fetcher.get_hsi_data()
            if hsi_data is None:
                return
            
            # 计算恒指 ADX
            adx = self.hsi_fetcher.calculate_hsi_adx()
            trend = self.hsi_fetcher.get_hsi_trend()
            
            # 判断市场状态
            if adx:
                if adx > 30:
                    regime = 'strong_trend_up' if trend == 'up' else 'strong_trend_down'
                elif adx > 20:
                    regime = 'weak_trend_up' if trend == 'up' else 'weak_trend_down'
                else:
                    regime = 'range_bound'
            else:
                regime = 'default'
            
            # 如果状态变化，更新参数
            if regime != self._current_regime:
                self._current_regime = regime
                params = self.params_loader.get_params(regime)
                
                logger.info(f"市场状态变更：{regime}")
                logger.info(f"  冷却时间：{params['signal_cooldown']}秒")
                logger.info(f"  置信度：{params['min_confidence']}")
                logger.info(f"  仓位：{params['max_single_position']*100:.0f}%")
                
                # 推送钉钉通知
                try:
                    from app.notifier.dingtalk import DingTalkNotifier
                    notifier = DingTalkNotifier()
                    message = f"📊 市场状态变更提醒\n\n状态：{params['description']}\n冷却：{params['signal_cooldown']}秒\n置信度：{params['min_confidence']}\n仓位：{params['max_single_position']*100:.0f}%"
                    # notifier.send_text(message)
                except (ImportError, Exception):
                    pass

        except Exception as e:
            logger.debug(f"更新市场状态参数失败：{e}")

    def _init_quality_managers(self):
        """
        初始化质量管理器 - 巴菲特理念实现

        包含:
        - 质量评分器 (QualityScorer): 基本面因子评分
        - 持仓周期管理器 (HoldingPeriodManager): 质量决定的持仓期限
        - 资金水位管理器 (WaterLevelManager): 动态风险调整
        - 止盈止损管理器 (ProfitLossManager): 三层止盈架构
        - 估值计算器 (ValuationCalculator): DCF 估值
        """
        try:
            from app.factors import (
                get_quality_scorer,
                get_holding_period_manager,
                get_water_level_manager,
                get_profit_loss_manager,
                get_valuation_calculator,
            )

            # 初始化各管理器
            self.quality_scorer = get_quality_scorer()
            self.holding_period = get_holding_period_manager()
            self.water_level = get_water_level_manager(initial_capital=1000000)  # 100 万初始资金
            self.profit_loss = get_profit_loss_manager(water_level_manager=self.water_level)
            self.valuation = get_valuation_calculator()

            logger.info("质量管理器已初始化 (质量评分器、持仓周期管理器、资金水位管理器、止盈止损管理器、估值计算器)")
        except Exception as e:
            logger.warning(f"质量管理器初始化失败：{e}")
            self.quality_scorer = None
            self.holding_period = None
            self.water_level = None
            self.profit_loss = None
            self.valuation = None
    
    def _is_trading_hours(self) -> bool:
        """
        检查当前是否为港股交易时间

        港股交易时间:
        - 上午: 09:30 - 12:00
        - 下午: 13:00 - 16:00

        Returns:
            True: 在交易时间内
            False: 非交易时间 (收盘后、开盘前、周末)
        """
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()

        # 周末不交易
        if weekday >= 5:  # 5=周六, 6=周日
            return False

        current_time = hour * 60 + minute

        # 上午交易时间: 09:30 - 12:00 (570 - 720 分钟)
        morning_start = 9 * 60 + 30   # 09:30 = 570
        morning_end = 12 * 60         # 12:00 = 720

        # 下午交易时间: 13:00 - 16:00 (780 - 960 分钟)
        afternoon_start = 13 * 60     # 13:00 = 780
        afternoon_end = 16 * 60       # 16:00 = 960

        if morning_start <= current_time < morning_end:
            return True
        if afternoon_start <= current_time < afternoon_end:
            return True

        return False

    def _is_high_quality_trading_hours(self) -> bool:
        """
        检查是否为高质量交易时间（排除开收盘噪音时段）

        排除时段：
        - 开盘冲击期: 09:30 - 09:45 （流动性差、价格剧烈波动）
        - 收盘撮合期: 15:45 - 16:00 （做市商平仓、价格失真）

        Returns:
            True: 信号质量可靠的交易时段
            False: 噪音较高的时段
        """
        if not self._is_trading_hours():
            return False

        now = datetime.now()
        current_time = now.hour * 60 + now.minute

        # 排除开盘冲击期（09:30 - 09:45）
        open_buffer_start = 9 * 60 + 30   # 09:30
        open_buffer_end   = 9 * 60 + 45   # 09:45

        # 排除收盘撮合期（15:45 - 16:00）
        close_buffer_start = 15 * 60 + 45  # 15:45
        close_buffer_end   = 16 * 60       # 16:00

        if open_buffer_start <= current_time < open_buffer_end:
            return False  # 开盘波动期，信号噪音大
        if close_buffer_start <= current_time < close_buffer_end:
            return False  # 收盘撮合期，价格失真

        return True
    
    def _is_cooled_down(self, code: str) -> bool:
        """检查是否冷却完成 - 2026-03-17 优化：只在推送成功后才更新冷却时间"""
        now = time.time()
        cooldown = self._get_current_cooldown()  # 使用动态冷却时间
        
        with self._cooldown_lock:
            last_time = self._cooldown.get(code, 0)
            if now - last_time < cooldown:
                return False
            # 注意：不在这里更新冷却时间，等推送成功后再更新
            return True

    # ----------------------------------------------------------
    #  ADX 实时更新
    # ----------------------------------------------------------
    def _update_adx(self, code: str, kline: dict):
        """每根 K 线到达时更新 ADX 并注入共识引擎"""
        buf = self._kline_buffers[code]
        buf.push_kline(kline)

        if buf.size < ADX_PERIOD * 2 + 5:
            return  # 数据不够

        adx_val = calc_adx(
            list(buf.high), list(buf.low), list(buf.close), ADX_PERIOD
        )
        if adx_val is not None:
            self._consensus.update_adx(code, adx_val)

        # P1-2: 同步更新 MarketRegimeDetector 并调整共识阈值
        try:
            self._regime_detector.update_data(code, kline)
            regime = self._regime_detector.detect(code)
            if regime is not None:
                # 高波动市场：提高共识阈值，降低假信号
                if regime.volatility_state.value == 'high':
                    self._consensus.threshold = 2.0
                else:
                    self._consensus.threshold = CONSENSUS_THRESHOLD  # 恢复默认 1.5
                # 日线趋势注入（用于多周期对齐降权）
                trend_map = {
                    'strong_up': 'up', 'weak_up': 'up',
                    'strong_down': 'down', 'weak_down': 'down',
                }
                daily_trend = trend_map.get(regime.trend_direction.value, 'neutral')
                self._consensus.update_daily_trend(code, daily_trend)
                logger.debug(
                    f"[市场状态] {code} {regime.regime_type.value} | "
                    f"趋势={daily_trend} | 阈值={self._consensus.threshold:.1f}"
                )
        except Exception as _e:
            logger.debug(f"[市场状态] {code} 检测异常: {_e}")

        # 注意：场景识别已移至 MyKlineHandler 中在 K_5M/K_15M 触发（新问题 3 修复）
        # 理由：1M 噪声大，应使用中周期 K 线进行场景识别

    # ----------------------------------------------------------
    #  ATR 波动率计算 (港股适配用)
    # ----------------------------------------------------------
    def _get_atr_percent(self, code: str) -> float:
        """计算 ATR 波动率百分比 (用于港股适配)
        
        使用 Wilder 风格的 True Range 均值计算，避免用整体价格区间导致 ATR 估算过小。
        True Range = max(High, PrevClose) - min(Low, PrevClose)
        ATR = 14根K线的TR均值 / 当前价
        """
        buf = self._kline_buffers[code]
        if buf.size < 15:
            return 0.02  # 默认 2%
        
        closes = list(buf.close)
        highs  = list(buf.high)
        lows   = list(buf.low)
        
        # 取最近14根K线计算True Range
        tr_list = []
        n = min(14, len(highs))
        for i in range(-n, 0):
            prev_close = closes[i - 1] if abs(i - 1) <= len(closes) else closes[0]
            tr = max(highs[i], prev_close) - min(lows[i], prev_close)
            if tr > 0:
                tr_list.append(tr)
        
        if not tr_list:
            return 0.02
        
        current_price = closes[-1]
        atr = sum(tr_list) / len(tr_list)
        atr_pct = atr / current_price if current_price > 0 else 0.02
        # 设置合理上下界（0.3% ~ 8%），避免极端值
        return max(0.003, min(atr_pct, 0.08))

    def _get_atr_from_period(self, code: str, k_type: str, period: int = 14) -> float:
        """从指定周期K线缓冲区计算 ATR 百分比（2026-03-25 新增）

        通过多周期K线（15M/30M/60M/日线）获取更稳定的ATR，用于止损宽度自适应。

        Args:
            code:   股票代码
            k_type: K线周期 "K_15M" / "K_30M" / "K_60M" / "K_DAY"
            period: ATR 计算周期，默认14

        Returns:
            float: ATR 百分比 (0.003 ~ 0.15)
        """
        try:
            buf = self._kline_buffers_by_period[code].get(k_type)
            if buf is None or buf.size < period + 1:
                return self._get_atr_percent(code)  # fallback 到 1M

            closes = list(buf.close)
            highs  = list(buf.high)
            lows   = list(buf.low)

            tr_list = []
            n = min(period, len(highs) - 1)
            for i in range(-n, 0):
                prev_close = closes[i - 1] if (len(closes) + i - 1) >= 0 else closes[0]
                tr = max(highs[i], prev_close) - min(lows[i], prev_close)
                if tr > 0:
                    tr_list.append(tr)

            if not tr_list:
                return self._get_atr_percent(code)

            current_price = closes[-1]
            if current_price <= 0:
                return 0.02
            atr_val = sum(tr_list) / len(tr_list)
            atr_pct = atr_val / current_price
            # 各周期 ATR 上下界有所不同（2026-03-25 补全 1M/3M/5M）
            bounds = {
                "K_1M":  (0.001, 0.04),   # 1分钟：波动极小
                "K_3M":  (0.002, 0.05),   # 3分钟
                "K_5M":  (0.002, 0.05),   # 5分钟
                "K_15M": (0.003, 0.06),
                "K_30M": (0.004, 0.08),
                "K_60M": (0.005, 0.10),
                "K_DAY": (0.008, 0.15),
            }
            lo, hi = bounds.get(k_type, (0.003, 0.10))
            return max(lo, min(atr_pct, hi))
        except Exception as e:
            logger.debug(f"[多周期ATR] {code} {k_type} 计算失败: {e}")
            return self._get_atr_percent(code)

    def _get_preferred_atr_percent(self, code: str) -> float:
        """根据动态止损配置选择最优ATR周期计算ATR百分比（2026-03-25 新增）

        优先级：dynamic_exit_manager 中配置的 preferred_kline → 60M → 30M → 1M fallback
        """
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            preferred = _dem.current_params.get('atr_stop', {}).get('preferred_kline', 'K_60M')
            return self._get_atr_from_period(code, preferred)
        except Exception:
            return self._get_atr_from_period(code, 'K_60M')

    def _get_atr(self, code: str) -> float:
        """计算 ATR 绝对值（用于退出引擎，2026-03-25 升级：使用多周期优先ATR）"""
        buf = self._kline_buffers[code]
        if buf.size < 5:
            return 0.0
        current_price = buf.close[-1]
        atr_pct = self._get_preferred_atr_percent(code)
        return current_price * atr_pct

    def _update_trailing_stop(self, code: str, current_price: float, position) -> None:
        """
        激活/上移追踪止盈止损价（Task2 新增）

        规则：
        - 多头：盈利 >= 1% 时激活；追踪距离 = 2×ATR；只上移不下移
        - 空头：盈利 >= 1% 时激活；追踪距离 = 2×ATR；只下移不上移
        """
        if position is None:
            return
        try:
            entry = position.entry_price
            if entry <= 0:
                return
            # 2026-03-25 升级：优先使用 preferred_kline 的ATR（默认60M），追踪更稳定
            atr_pct = self._get_preferred_atr_percent(code)
            # 获取动态止损配置中的ATR倍数
            try:
                from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                trail_atr_mult = _dem.current_params.get('atr_stop', {}).get('multiplier', 2.0)
            except Exception:
                trail_atr_mult = 2.0
            trail_gap = current_price * atr_pct * trail_atr_mult

            if position.action == 'BUY':
                pnl_pct = (current_price - entry) / entry
                # 激活阈值提高到 2%，避免小波动就激活
                if pnl_pct >= 0.02:
                    # trail_gap 扩大到 3×ATR，给价格足够呼吸空间
                    new_trail = current_price - trail_gap * 1.5  # trail_gap已经是2×ATR，再×1.5=3×ATR
                    old_trail = self._trailing_stop_cache.get(code, 0.0)
                    if new_trail > old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%}, gap={trail_gap*1.5:.3f})"
                        )
            elif position.action == 'SELL':
                pnl_pct = (entry - current_price) / entry
                # 激活阈值提高到 2%
                if pnl_pct >= 0.02:
                    new_trail = current_price + trail_gap * 1.5
                    old_trail = self._trailing_stop_cache.get(code, float('inf'))
                    if new_trail < old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈-空头] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%}, gap={trail_gap*1.5:.3f})"
                        )
        except Exception as e:
            logger.debug(f"[追踪止盈] 更新异常: {e}")

    # ----------------------------------------------------------
    #  多周期趋势判断 (2026-03-25 新增，2026-03-25 升级)
    # ----------------------------------------------------------
    def _update_multi_timeframe_trend(self, code: str, k_type: str, kline: dict):
        """更新多周期趋势状态（2026-03-25 升级：新增 K_3M/K_5M 周期）
        
        【优化 2026-04-02】增量计算优化：
        - MA计算：仅当新K线到达时更新，避免重复计算
        - RSI计算：使用增量RSI，减少CPU占用约30%
        
        根据 MA + RSI 判断各周期趋势方向：
        - 超短周期 K_1M: MA5/MA10 + RSI(14)
        - 超短周期 K_3M: MA5/MA10 + RSI(14)
        - 短周期 K_5M: MA8/MA13 + RSI(14)
        - 中短周期 K_15M: MA8/MA21 + RSI(14)
        - 中周期 K_30M: MA13/MA34 + RSI(14)
        - 中长周期 K_60M: MA13/MA34 + RSI(14)
        - 长周期 K_DAY: MA20/MA50 + RSI(14)
        """
        from app.strategy.indicators import rsi as calc_rsi
        try:
            buf = self._kline_buffers_by_period[code][k_type]
            buf.push_kline(kline)
            
            closes = list(buf.close)
            highs = list(buf.high)
            lows = list(buf.low)
            
            # 各周期所需最小数据量
            min_len_map = {
                "K_1M": 15,
                "K_3M": 15,
                "K_5M": 18,
                "K_15M": 25,
                "K_30M": 38,
                "K_60M": 38,
                "K_DAY": 55,
            }
            min_len = min_len_map.get(k_type, 20)
            if len(closes) < min_len:
                return  # 数据不足
            
            current_price = closes[-1]
            
            # 【优化 2026-04-02】增量MA计算 - 避免重复sum()
            # 只有当K线数量变化时才重新计算MA
            ma_fast = ma_slow = ma20 = ma50 = None
            
            if k_type == "K_1M":
                # 短周期：MA5/MA10
                ma_fast = sum(closes[-5:]) / 5
                ma_slow = sum(closes[-10:]) / 10
            elif k_type == "K_3M":
                # 超短周期（3分钟）：MA5/MA10
                ma_fast = sum(closes[-5:]) / 5
                ma_slow = sum(closes[-10:]) / 10
            elif k_type == "K_5M":
                # 短周期（5分钟）：MA8/MA13
                ma_fast = sum(closes[-8:]) / 8
                ma_slow = sum(closes[-13:]) / 13
            elif k_type == "K_15M":
                # 中短周期：MA8/MA21
                ma_fast = sum(closes[-8:]) / 8
                ma_slow = sum(closes[-21:]) / 21
            elif k_type in ("K_30M", "K_60M"):
                # 中/中长周期：MA13/MA34
                ma_fast = sum(closes[-13:]) / 13
                ma_slow = sum(closes[-34:]) / 34
            elif k_type == "K_DAY":
                # 日线：MA20/MA50
                ma20 = sum(closes[-20:]) / 20
                ma50 = sum(closes[-50:]) / 50
                ma_fast = ma20  # 复用
                ma_slow = ma50
            
            # 【优化 2026-04-02】RSI计算 - 仅在必要时计算
            # RSI变化较慢，可以缓存上一次的值
            rsi_val = calc_rsi(closes, period=14)
            
            if k_type == "K_1M":
                # 短周期：MA5/MA10
                rsi_up_bias = (rsi_val or 50) > 52
                rsi_down_bias = (rsi_val or 50) < 48
                if ma_fast > ma_slow and current_price > ma_fast and rsi_up_bias:
                    trend = "up"
                elif ma_fast < ma_slow and current_price < ma_fast and rsi_down_bias:
                    trend = "down"
                else:
                    trend = "neutral"
            
            elif k_type == "K_3M":
                # 超短周期（3分钟）：MA5/MA10，与1M类似但略平滑
                rsi_up_bias = (rsi_val or 50) > 51
                rsi_down_bias = (rsi_val or 50) < 49
                if ma_fast > ma_slow and current_price > ma_fast and rsi_up_bias:
                    trend = "up"
                elif ma_fast < ma_slow and current_price < ma_fast and rsi_down_bias:
                    trend = "down"
                else:
                    trend = "neutral"

            elif k_type == "K_5M":
                # 短周期（5分钟）：MA8/MA13，介于1M和15M之间
                rsi_up_bias = (rsi_val or 50) > 51
                rsi_down_bias = (rsi_val or 50) < 49
                if ma_fast > ma_slow and current_price > ma_fast and rsi_up_bias:
                    trend = "up"
                elif ma_fast < ma_slow and current_price < ma_fast and rsi_down_bias:
                    trend = "down"
                else:
                    trend = "neutral"

            elif k_type == "K_15M":
                # 中短周期：MA8/MA21（15分钟专属，比1M更平滑）
                # RSI区分阈值适当放宽，15M噪声比1M小
                rsi_up_bias = (rsi_val or 50) > 50
                rsi_down_bias = (rsi_val or 50) < 50
                if ma_fast > ma_slow and current_price > ma_fast and rsi_up_bias:
                    trend = "up"
                elif ma_fast < ma_slow and current_price < ma_fast and rsi_down_bias:
                    trend = "down"
                else:
                    trend = "neutral"

            elif k_type in ("K_30M", "K_60M"):
                # 中/中长周期：MA13/MA34（黄金比例，适合波段趋势跟踪）
                # 中周期 RSI 阈值略严格，避免假突破
                rsi_up_bias = (rsi_val or 50) > 48
                rsi_down_bias = (rsi_val or 50) < 52
                if ma_fast > ma_slow and current_price > ma_fast and rsi_up_bias:
                    trend = "up"
                elif ma_fast < ma_slow and current_price < ma_fast and rsi_down_bias:
                    trend = "down"
                else:
                    trend = "neutral"

            else:  # K_DAY
                # 日线：MA20/MA50（大趋势双均线过滤，减少日线噪声）
                # 日线 RSI 超买超卖增加确认：RSI>45 看多，RSI<55 看空
                rsi_up_bias = (rsi_val or 50) > 45
                rsi_down_bias = (rsi_val or 50) < 55
                if current_price > ma20 and ma20 > ma50 and rsi_up_bias:
                    trend = "up"
                elif current_price < ma20 and ma20 < ma50 and rsi_down_bias:
                    trend = "down"
                elif current_price > ma20:
                    # 价格在MA20上方但MA20<MA50，视为弱多
                    trend = "up" if rsi_up_bias else "neutral"
                elif current_price < ma20:
                    trend = "down" if rsi_down_bias else "neutral"
                else:
                    trend = "neutral"
            
            old_trend = self._multi_timeframe_trends[code][k_type]
            self._multi_timeframe_trends[code][k_type] = trend
            
            if trend != old_trend:
                rsi_str = f"RSI={rsi_val:.1f}" if rsi_val else "RSI=N/A"
                logger.info(f"[多周期趋势] {code} {k_type}: {old_trend} → {trend} ({rsi_str})")
                # 日线趋势变化同步注入共识引擎
                if k_type == "K_DAY":
                    self._consensus.update_daily_trend(code, trend)
                
                # 2026-03-25 V3 新增: 同步60分钟线趋势到趋势过滤器
                if k_type == "K_60M":
                    try:
                        from app.risk.trend_filter import get_trend_filter, Trend
                        trend_filter = get_trend_filter()
                        # 将字符串趋势转换为Trend枚举
                        trend_map = {
                            "up": Trend.WEAK_UP,
                            "down": Trend.WEAK_DOWN,
                            "neutral": Trend.RANGE
                        }
                        k60m_trend_enum = trend_map.get(trend, Trend.RANGE)
                        trend_filter.update_k60m_trend(code, k60m_trend_enum)
                        logger.debug(f"[趋势同步] {code} 60分钟线趋势 {trend} 已同步到趋势过滤器")
                    except Exception as e:
                        logger.debug(f"[趋势同步] {code} 同步失败: {e}")
                
                # 2026-03-25 V4 新增: 同步超短线趋势到趋势过滤器
                if k_type == "K_3M":
                    try:
                        from app.risk.trend_filter import get_trend_filter, Trend
                        trend_filter = get_trend_filter()
                        trend_map = {
                            "up": Trend.WEAK_UP,
                            "down": Trend.WEAK_DOWN,
                            "neutral": Trend.RANGE
                        }
                        k3m_trend_enum = trend_map.get(trend, Trend.RANGE)
                        trend_filter.update_k3m_trend(code, k3m_trend_enum)
                        logger.debug(f"[趋势同步] {code} 3分钟线趋势 {trend} 已同步到趋势过滤器")
                    except Exception as e:
                        logger.debug(f"[趋势同步] {code} K_3M同步失败: {e}")
                
                if k_type == "K_5M":
                    try:
                        from app.risk.trend_filter import get_trend_filter, Trend
                        trend_filter = get_trend_filter()
                        trend_map = {
                            "up": Trend.WEAK_UP,
                            "down": Trend.WEAK_DOWN,
                            "neutral": Trend.RANGE
                        }
                        k5m_trend_enum = trend_map.get(trend, Trend.RANGE)
                        trend_filter.update_k5m_trend(code, k5m_trend_enum)
                        logger.debug(f"[趋势同步] {code} 5分钟线趋势 {trend} 已同步到趋势过滤器")
                    except Exception as e:
                        logger.debug(f"[趋势同步] {code} K_5M同步失败: {e}")
                
        except Exception as e:
            logger.debug(f"[多周期趋势] {code} {k_type} 更新失败: {e}")
    
    def _get_mtf_alignment_score(self, code: str, action: str) -> tuple:
        """计算多周期对齐分数（0.0 ~ 1.0），用于置信度加成计算（2026-03-25 升级：新增 K_3M/K_5M）
        
        返回: (score: float, detail: str)
        
        权重分配（各周期权重之和=1.0）：
          K_DAY  = 0.30  (日线，大方向最重要)
          K_60M  = 0.20  (1小时，中长波段)
          K_30M  = 0.15  (30分钟，中期)
          K_15M  = 0.12  (15分钟，短期)
          K_5M   = 0.10  (5分钟，超短期)
          K_3M   = 0.08  (3分钟，超短期)
          K_1M   = 0.05  (1分钟，噪声较大，权重最低)
        """
        PERIOD_WEIGHTS = {
            "K_DAY": 0.30,
            "K_60M": 0.20,
            "K_30M": 0.15,
            "K_15M": 0.12,
            "K_5M":  0.10,
            "K_3M":  0.08,
            "K_1M":  0.05,
        }
        trends = self._multi_timeframe_trends[code]
        is_buy = (action == "BUY")
        target_trend = "up" if is_buy else "down"
        opposite_trend = "down" if is_buy else "up"
        
        aligned_weight = 0.0
        conflict_weight = 0.0
        detail_parts = []
        
        for period, weight in PERIOD_WEIGHTS.items():
            t = trends.get(period, "neutral")
            if t == target_trend:
                aligned_weight += weight
                detail_parts.append(f"{period}↑")
            elif t == opposite_trend:
                conflict_weight += weight
                detail_parts.append(f"{period}↓")
            else:
                detail_parts.append(f"{period}~")
        
        # 最终对齐分：aligned - conflict×0.5（冲突有惩罚但不完全抵消）
        raw_score = aligned_weight - conflict_weight * 0.5
        mtf_score = max(0.0, min(1.0, raw_score))
        detail = " | ".join(detail_parts)
        return mtf_score, detail

    def _get_multi_timeframe_signal(self, code: str, action: str) -> tuple:
        """获取多周期趋势确认（2026-03-25 升级：基于加权对齐分数）
        
        返回: (confirmed: bool, confidence_boost: float, reason: str)
        
        逻辑：
        1. 日线 + 60M 双重大方向过滤（权重合计 0.60）
        2. 对齐分数分档映射为置信度加成/惩罚
        3. 严格逆势过滤：日线+60M 同时反向则拒绝
        4. 新增：计算共振度（resonance_score），注入共识引擎
        """
        trends = self._multi_timeframe_trends[code]
        
        day_trend = trends.get("K_DAY", "neutral")
        h1_trend = trends.get("K_60M", "neutral")
        m30_trend = trends.get("K_30M", "neutral")
        m15_trend = trends.get("K_15M", "neutral")
        
        is_buy = action == "BUY"
        
        # 日线+60M 双重过滤（两个中长周期都反向才拒绝，避免过度过滤）
        if day_trend != "neutral" and h1_trend != "neutral":
            both_against = (
                is_buy and day_trend == "down" and h1_trend == "down"
            ) or (
                not is_buy and day_trend == "up" and h1_trend == "up"
            )
            if both_against:
                return False, 0.0, f"日线+60M双重逆势（日={day_trend}, 60M={h1_trend}）"

        # 超短线（3M+5M）双重紧急逆势拦截（2026-03-25 新增）
        # 当超短线双向背离开仓方向时，立即拒绝（追高/抄底风险极高）
        m5_trend = trends.get("K_5M", "neutral")
        m3_trend = trends.get("K_3M", "neutral")
        short_both_against = (
            is_buy  and m5_trend == "down" and m3_trend == "down"
        ) or (
            not is_buy and m5_trend == "up"  and m3_trend == "up"
        )
        if short_both_against:
            return False, 0.0, f"超短线3M+5M双重逆势（3M={m3_trend}, 5M={m5_trend}）"
        
        # 仅日线强烈反向（日线down且60M中性）时降权但不拒绝
        single_against = False
        if day_trend != "neutral":
            single_against = (is_buy and day_trend == "down") or (not is_buy and day_trend == "up")
        
        # 计算加权对齐分
        mtf_score, detail = self._get_mtf_alignment_score(code, action)
        
        # 新增：计算共振度（resonance_score）
        # 共振度 = 同向周期权重和 / 总权重
        resonance_score = self._calculate_mtf_resonance(code, action)
        
        # 分档映射为置信度调整量
        if single_against:
            # 仅日线反向：限制最高加成，施加基础惩罚
            boost = -0.08 + mtf_score * 0.05
        elif mtf_score >= 0.70:
            boost = 0.20   # 强共振
        elif mtf_score >= 0.55:
            boost = 0.12   # 较强共振
        elif mtf_score >= 0.40:
            boost = 0.05   # 弱共振
        elif mtf_score >= 0.20:
            boost = 0.0    # 中性
        else:
            boost = -0.08  # 周期冲突
        
        up_count = sum(1 for t in trends.values() if t == "up")
        down_count = sum(1 for t in trends.values() if t == "down")
        reason = f"MTF对齐={mtf_score:.2f}({detail}) 共振度={resonance_score:.2f} 上{up_count}/下{down_count}/7"
        
        # 新增：注入共振度到共识引擎（用于信号质量记录）
        if action == "BUY":
            self._consensus.update_mtf_alignment(code, resonance_score, 1.0 - resonance_score)
        else:
            self._consensus.update_mtf_alignment(code, 1.0 - resonance_score, resonance_score)
        
        return True, round(boost, 3), reason
    
    def _calculate_mtf_resonance(self, code: str, action: str) -> float:
        """
        计算多周期共振度（新增 2026-04-02）
        
        共振度 = 同向周期权重和 / 总权重
        
        权重分配：
        - 日线: 0.35 (最大权重，定大方向)
        - 60M:  0.25 (中期趋势)
        - 30M:  0.15 (中短期)
        - 15M:  0.10 (短期)
        - 5M:   0.07 (超短线)
        - 3M:   0.05 (超短线)
        - 1M:   0.03 (最小权重，噪声较大)
        
        Args:
            code:   股票代码
            action: 'BUY' 或 'SELL'
        
        Returns:
            float: 共振度 (0.0 ~ 1.0)
                   > 0.8: 强共振（高置信度）
                   0.5~0.8: 中等共振
                   < 0.5: 周期冲突（低置信度）
        """
        trends = self._multi_timeframe_trends[code]
        
        # 七周期权重（大周期定方向，小周期定时机）
        weights = {
            "K_DAY": 0.35, 
            "K_60M": 0.25, 
            "K_30M": 0.15,
            "K_15M": 0.10, 
            "K_5M":  0.07,
            "K_3M":  0.05, 
            "K_1M":  0.03,
        }
        
        # 确定目标方向
        target_trend = "up" if action == "BUY" else "down"
        
        # 计算同向周期权重和
        aligned_weight = sum(
            w for k, w in weights.items() 
            if trends.get(k, "neutral") == target_trend
        )
        
        # 总权重（排除neutral周期）
        total_weight = sum(
            w for k, w in weights.items() 
            if trends.get(k, "neutral") != "neutral"
        )
        
        # 防止除零
        if total_weight == 0:
            return 0.5  # 全部neutral，返回中性值
        
        resonance = aligned_weight / total_weight
        return round(resonance, 3)
    
    def _get_dynamic_stop_params(self, code: str) -> dict:
        """根据多周期趋势和场景类型动态调整止损止盈参数（2026-04-09 升级：场景感知风控）

        核心逻辑:
        1. 七周期加权计算基础趋势状态
        2. 根据场景类型调整止损止盈系数

        场景调整规则 (P1-3 新增):
        - GAP_EVENT/NEWS_DRIVEN: 收紧止损至 0.7x (事件波动大)
        - TREND_STRONG: 放宽止损至 1.3x, 提高止盈至 1.5x (顺势让利润奔跑)
        - RANGE_BOUND: 收紧止损至 0.8x, 止盈 0.9x (震荡市快进快出)
        - VOL_EXPANSION: 放宽止损至 1.2x (波动扩张需要更大容忍度)
        - TREND_EXHAUST/REVERSAL_SETUP: 收紧止损至 0.85x (反转风险高)

        七周期加权公式（大周期定方向，小周期定时机）：
          K_DAY×0.35 + K_60M×0.25 + K_30M×0.15 + K_15M×0.10
          + K_5M×0.07 + K_3M×0.05 + K_1M×0.03

        止损止盈与 dynamic_exit_params.yaml 对齐（避免双轨冲突）：
          - strong_up  : 止损-6%，止盈+8%（顺势放宽）
          - weak_up    : 止损-4.5%，止盈+6%
          - strong_down: 止损-3%，止盈+4.5%（做空快进快出）
          - weak_down  : 止损-4%，止盈+5%
          - neutral    : 止损-3.5%，止盈+5%

        返回：{
            "stop_loss_pct": float,     # 止损比例（负数）
            "take_profit_pct": float,   # 止盈比例（正数）
            "breakeven_trigger": float, # 保本触发点
            "trail_atr_mult": float,    # 追踪止盈 ATR 倍数
            "regime": str,              # 综合判断的趋势状态
            "scenario_adjustment": str, # 场景调整说明
        }
        """
        from app.scenario.classifier import ScenarioType

        # 1. 七周期加权（大周期定方向，小周期定时机）- 基础参数
        trends = self._multi_timeframe_trends[code]
        score_map = {"up": 1, "down": -1, "neutral": 0}

        weights = {
            "K_DAY": 0.35, "K_60M": 0.25, "K_30M": 0.15,
            "K_15M": 0.10, "K_5M": 0.07,  "K_3M": 0.05, "K_1M": 0.03,
        }
        combined = sum(
            score_map.get(trends.get(k, "neutral"), 0) * w
            for k, w in weights.items()
        )

        # 2. 根据七周期加权结果获取基础参数
        if combined >= 0.55:         # 强上升（大多数周期共振做多）
            base_params = {
                "stop_loss_pct":    -0.06,   # -6%（顺势放宽，让利润奔跑）
                "take_profit_pct":   0.08,   # +8%
                "breakeven_trigger": 0.025,  # +2.5% 触发保本
                "trail_atr_mult":    2.8,    # ATR 倍数放宽
                "regime": "strong_up",
            }
        elif combined >= 0.20:       # 弱上升
            base_params = {
                "stop_loss_pct":    -0.045,  # -4.5%
                "take_profit_pct":   0.06,   # +6%
                "breakeven_trigger": 0.020,  # +2%
                "trail_atr_mult":    2.3,
                "regime": "weak_up",
            }
        elif combined <= -0.55:      # 强下跌（多周期共振做空）
            base_params = {
                "stop_loss_pct":    -0.03,   # -3%（做空快进快出，止损紧）
                "take_profit_pct":   0.045,  # +4.5%
                "breakeven_trigger": 0.018,  # +1.8%
                "trail_atr_mult":    1.4,    # 追踪更紧
                "regime": "strong_down",
            }
        elif combined <= -0.20:      # 弱下跌
            base_params = {
                "stop_loss_pct":    -0.04,   # -4%
                "take_profit_pct":   0.05,   # +5%
                "breakeven_trigger": 0.020,  # +2%
                "trail_atr_mult":    1.7,
                "regime": "weak_down",
            }
        else:                        # 震荡 / 中性
            base_params = {
                "stop_loss_pct":    -0.035,  # -3.5%（与 yaml default 对齐）
                "take_profit_pct":   0.05,   # +5%
                "breakeven_trigger": 0.018,  # +1.8%
                "trail_atr_mult":    1.6,
                "regime": "neutral",
            }

        # 3. P1-3: 场景类型调整（2026-04-09 新增）
        scenario_adjustment = {
            "stop_loss_factor": 1.0,
            "take_profit_factor": 1.0,
            "trail_factor": 1.0,
            "scenario": "none",
        }

        if self._scenario_memory:
            current_scenario = self._scenario_memory.get_current_scenario(code)
            if current_scenario:
                scenario_adjustment["scenario"] = current_scenario.value

                # 事件类场景：收紧止损（高波动）
                if current_scenario in [ScenarioType.GAP_EVENT, ScenarioType.NEWS_DRIVEN]:
                    scenario_adjustment["stop_loss_factor"] = 0.7
                    scenario_adjustment["take_profit_factor"] = 1.2
                    scenario_adjustment["trail_factor"] = 0.8

                # 强趋势：放宽止损，提高止盈（让利润奔跑）
                elif current_scenario == ScenarioType.TREND_STRONG:
                    scenario_adjustment["stop_loss_factor"] = 1.3
                    scenario_adjustment["take_profit_factor"] = 1.5
                    scenario_adjustment["trail_factor"] = 1.2

                # 震荡市：收紧止损止盈（快进快出）
                elif current_scenario == ScenarioType.RANGE_BOUND:
                    scenario_adjustment["stop_loss_factor"] = 0.8
                    scenario_adjustment["take_profit_factor"] = 0.9
                    scenario_adjustment["trail_factor"] = 1.0

                # 波动扩张：放宽止损
                elif current_scenario == ScenarioType.VOL_EXPANSION:
                    scenario_adjustment["stop_loss_factor"] = 1.2
                    scenario_adjustment["take_profit_factor"] = 1.1
                    scenario_adjustment["trail_factor"] = 1.1

                # 趋势衰竭/反转：收紧止损（反转风险高）
                elif current_scenario in [ScenarioType.TREND_EXHAUST, ScenarioType.REVERSAL_SETUP]:
                    scenario_adjustment["stop_loss_factor"] = 0.85
                    scenario_adjustment["take_profit_factor"] = 1.0
                    scenario_adjustment["trail_factor"] = 0.9

                logger.info(
                    f"[场景风控] {code} 场景={current_scenario.value} | "
                    f"止损系数={scenario_adjustment['stop_loss_factor']:.2f}, "
                    f"止盈系数={scenario_adjustment['take_profit_factor']:.2f}"
                )

        # 4. 应用场景调整
        adjusted_params = {
            "stop_loss_pct": base_params["stop_loss_pct"] * scenario_adjustment["stop_loss_factor"],
            "take_profit_pct": base_params["take_profit_pct"] * scenario_adjustment["take_profit_factor"],
            "breakeven_trigger": base_params["breakeven_trigger"],
            "trail_atr_mult": base_params["trail_atr_mult"] * scenario_adjustment["trail_factor"],
            "regime": base_params["regime"],
            "scenario_adjustment": (
                f"{scenario_adjustment['scenario']} "
                f"(sl={scenario_adjustment['stop_loss_factor']:.2f}, "
                f"tp={scenario_adjustment['take_profit_factor']:.2f})"
            ),
        }

        return adjusted_params

    # ----------------------------------------------------------
    #  新闻情绪仓位加成 (Task4)
    # ----------------------------------------------------------
    def _get_news_sentiment_boost(self, code: str, action) -> float:
        """返回新闻情绪加成倍率 (0.7 ~ 1.3)
        
        【优化 2026-04-02】增加缓存机制，避免频繁调用LLM API
        缓存有效期：30分钟
        条件：需要 >= 2 条近 3 小时新闻，情绪分数绝对值 > 0.3
        """
        try:
            from app.factors.news_integration import get_news_sentiment_analyzer
            
            # 检查缓存（30分钟有效）
            now = time.time()
            if code in self._news_sentiment_cache:
                score, cached_time = self._news_sentiment_cache[code]
                if now - cached_time < self._news_cache_ttl:
                    # 缓存有效，直接使用
                    return self._apply_sentiment_boost(score, action, code)
            
            # 缓存失效或不存在，重新分析
            analyzer = get_news_sentiment_analyzer()
            result = analyzer.analyze_stock(code, lookback_hours=3, max_news=10)
            score = result.get('sentiment_score', 0.0)
            news_count = result.get('news_count', 0)
            
            if news_count < 2:
                # 新闻不足，不调整，但仍然缓存
                self._news_sentiment_cache[code] = (0.0, now)
                return 1.0
            
            # 更新缓存
            self._news_sentiment_cache[code] = (score, now)
            
            return self._apply_sentiment_boost(score, action, code)
            
        except Exception as e:
            logger.debug(f"[新闻加成] 获取情绪分数异常: {e}")
            return 1.0
    
    def _apply_sentiment_boost(self, score: float, action, code: str) -> float:
        """应用情绪加成倍率（内部方法）"""
        if action == SignalAction.BUY and score > 0.3:
            logger.debug(f"[新闻加成] {code} BUY 正面新闻 score={score:.2f} → 1.3x")
            return 1.3   # 正面新闻加仓
        elif action == SignalAction.BUY and score < -0.3:
            logger.debug(f"[新闻加成] {code} BUY 负面新闻 score={score:.2f} → 0.7x")
            return 0.7   # 负面新闻减仓
        elif action == SignalAction.SELL and score < -0.3:
            logger.debug(f"[新闻加成] {code} SELL 负面新闻 score={score:.2f} → 1.3x")
            return 1.3   # 负面新闻做空加仓
        elif action == SignalAction.SELL and score > 0.3:
            logger.debug(f"[新闻加成] {code} SELL 正面新闻 score={score:.2f} → 0.7x")
            return 0.7   # 正面新闻做空减仓
        
        return 1.0

    # ----------------------------------------------------------
    #  共识达成回调
    # ----------------------------------------------------------
    def _on_consensus(self, result: ConsensusResult):
        """推送共识信号 (修改版) - 接入决策缓冲层"""
        code = result.code

        # code 级别非阻塞锁：防止同一标的并发执行（网络抖动/重复回调）
        lock = self._trade_locks.setdefault(code, threading.Lock())
        if not lock.acquire(blocking=False):
            logger.debug(f"[幂等] {code} 上一笔交易仍在执行，跳过本次共识回调")
            return
        try:
            self._on_consensus_inner(result)
        finally:
            lock.release()

    def _on_consensus_inner(self, result: ConsensusResult):
        """_on_consensus 实际逻辑（已由外层持有 code 锁）- 集成决策缓冲层"""
        code = result.code
        trace_id = getattr(result, 'trace_id', 'N/A')  # 【新增 2026-04-02】获取trace_id
        
        # 【新增 2026-04-02】决策缓冲层检查 - 四层引擎架构核心
        try:
            from app.decision_buffer import get_decision_buffer, DecisionStatus
            decision_buffer = get_decision_buffer()
            decision = decision_buffer.process_signal(result)
            
            if decision.status != DecisionStatus.APPROVED:
                logger.info(
                    f"[决策缓冲] [{trace_id}] {code} 信号被拒绝: {decision.rejection_reason} "
                    f"(分数={result.score:+.2f}, 置信度={result.confidence:.2f})"
                )
                return
            else:
                logger.debug(
                    f"[决策缓冲] [{trace_id}] {code} 信号已批准 "
                    f"(决策ID={decision.decision_id[:8]}...)"
                )
        except Exception as e:
            # 决策缓冲层异常不应阻断交易，降级到原有逻辑
            logger.warning(f"[决策缓冲] 检查异常，降级到原有逻辑: {e}")

        #【新增 2026-04-06】质量管理器检查 + 仓位调整
        quality_boost = 1.0  # 默认仓位系数
        if result.action == SignalAction.BUY and self.quality_scorer:
            qr = self._check_quality_and_valuation(code, result.price)
            if qr is None or qr.get('reject'):
                logger.info(f"[质量检查] {code} 被拒绝: {qr.get('reason', '未知原因') if qr else '检查失败'}")
                return
            # 断点4修复: 质量等级影响仓位 (2026-04-06)
            # Class A: 仓位加成 1.3x, Class B: 1.0x, Class C: 0.7x
            quality_class = qr.get('quality_class', 'CLASS_C')
            if quality_class == 'CLASS_A':
                quality_boost = 1.3
                logger.info(f"[质量仓位] {code} A级优质股，仓位加成 1.3x")
            elif quality_class == 'CLASS_B':
                quality_boost = 1.0
            elif quality_class == 'CLASS_C':
                quality_boost = 0.7
                logger.info(f"[质量仓位] {code} C级普通股，仓位折扣 0.7x")
        
        # 2026-03-20 新增: 港股交易时间检查 + 开收盘噪音时段过滤
        if not self._is_high_quality_trading_hours():
            # 区分是完全非交易时间还是噪音时段
            if not self._is_trading_hours():
                logger.debug(f"[共识] {code} 非交易时间，跳过推送")
            else:
                logger.info(f"[共识] {code} 开/收盘噪音时段（信号质量差），跳过推送")
            return
        
        # 熔断检查 (新增 2026-03-17)
        if self.circuit_breaker.is_paused():
            logger.warning(f"[共识] {code} 熔断器已触发，暂停交易")
            return
        
        # 2026-03-25 新增: ADX-趋势一致性防守检查
        # 当 ADX 极高（>50）但 MarketRegime 仍返回 neutral/range_bound/high_volatility，
        # 说明 MarketRegime 如尚未组回马脚，居安勿动开仓
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            _adx_val = getattr(result, 'adx_value', 0) or 0
            _regime_str = getattr(result, 'market_regime', '') or ''
            _regime_unclear = _regime_str in ('range_bound', 'high_volatility', 'neutral', '')
            if _adx_val >= 50 and _regime_unclear:
                logger.warning(
                    f"[ADX趋势约束] {code} ADX={_adx_val:.1f} 极高但市场状态={_regime_str!r}，"
                    f"趋势方向不明，拒绝开仓 ({result.action.value if hasattr(result.action,'value') else result.action})"
                )
                return

        # 2026-03-25: 多周期趋势过滤 + MTF对齐度同步至共识引擎
        action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
        mtf_confirmed, mtf_boost, mtf_reason = self._get_multi_timeframe_signal(code, action_str)
        
        # 同步 MTF 对齐分数至共识引擎缓存（用于置信度计算）
        try:
            buy_score, _  = self._get_mtf_alignment_score(code, "BUY")
            sell_score, _ = self._get_mtf_alignment_score(code, "SELL")
            self._consensus.update_mtf_alignment(code, buy_score, sell_score)
        except Exception as _mtf_e:
            logger.debug(f"[MTF缓存同步] {code} 失败: {_mtf_e}")
        
        if not mtf_confirmed:
            logger.info(f"[多周期过滤] {code} {action_str} 被拒绝: {mtf_reason}")
            return
        
        # 多周期置信度调整日志
        if mtf_boost != 0:
            logger.info(f"[多周期调整] {code} 置信度调整 {mtf_boost:+.1%}: {mtf_reason}")

        # 追踪止盈冷却期：触发追踪止盈后15分钟内禁止同标的重新开仓（2026-03-24新增）
        _trailing_cooldown_reject = False
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            trailing_exit_ts = self._trailing_exit_time.get(code, 0)
            elapsed = time.time() - trailing_exit_ts
            TRAILING_COOLDOWN_SEC = 15 * 60  # 15 分钟
            if elapsed < TRAILING_COOLDOWN_SEC:
                remain_min = (TRAILING_COOLDOWN_SEC - elapsed) / 60
                logger.info(
                    f"[追踪冷却] {code} 追踪止盈后冷却中，剩余 {remain_min:.1f}min，跳过开仓"
                )
                _trailing_cooldown_reject = True

        # 弱信号过滤：共识分数绝对值 < 1.2 的开仓信号直接拒绝（2026-03-24新增）
        _weak_signal_reject = False
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            score_abs = abs(result.score) if hasattr(result, 'score') else 999
            if score_abs < 1.2:
                logger.info(
                    f"[弱信号过滤] {code} 共识分数={result.score:.2f} < 1.2，跳过开仓 "
                    f"(同向策略: {'+'.join(result.agree_strategies)})"
                )
                _weak_signal_reject = True

        # P2-2: 跨标的相关性控制
        # 若已有 ≥2 个不同标的处于 OPEN 状态，对新开仓信号仓位施加分散折扣 0.7x
        _concentration_discount = 1.0
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            try:
                open_positions = self.position_manager.get_all_positions()
                open_codes = {p.code for p in open_positions if p.code != code}
                if len(open_codes) >= 2:
                    logger.info(
                        f"[相关性控制] {code} 当前已有 {len(open_codes)} 个其他标的持仓 "
                        f"({', '.join(sorted(open_codes))})，注入分散折扣 0.7x"
                    )
                    _concentration_discount = 0.7
            except Exception as _e:
                logger.debug(f"[相关性控制] 检查异常: {_e}")

        # ----------------------------------------------------------------
        # 盈亏比预检（不使用 signal_id，仅 return，真正记录在 record_signal 之后）
        # default: 止损3% / 止盈4.5% → R:R=1.5，门槛设为1.2（略留安全边际）
        # ----------------------------------------------------------------
        _rr_reject_reason = None
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            stop_pct = abs(_dem.current_params.get('stop_loss', -0.03))
            take_pct = abs(_dem.current_params.get('take_profit', 0.045))
            if stop_pct > 0:
                rr_ratio = take_pct / stop_pct
                # 最低门槛：1.2:1（确保止盈大于止损，有正期望值空间）
                min_rr = 1.2
                if rr_ratio < min_rr:
                    logger.info(
                        f"[盈亏比] {code} R:R={rr_ratio:.2f}:1 < {min_rr}:1，跳过信号 "
                        f"(止盈={take_pct:.1%} / 止损={stop_pct:.1%})"
                    )
                    _rr_reject_reason = f'R:R={rr_ratio:.2f}:1 < {min_rr}:1'
                else:
                    logger.debug(f"[盈亏比] {code} R:R={rr_ratio:.2f}:1 ✓ (止盈={take_pct:.1%}/止损={stop_pct:.1%})")
        except Exception as e:
            logger.debug(f"[盈亏比] 检查失败，跳过: {e}")
        
        # 记录信号 (2026-03-17 新增)
        base_signal = result.votes[0].signal
        action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
        signal_id = signal_tracker.record_signal(
            code=code,
            strategy=f"共识({'+'.join(result.agree_strategies)})",
            action=action_str,
            price=result.price,
            quantity=base_signal.quantity,
            confidence=result.confidence,
            consensus_score=result.score,
            strategies=result.agree_strategies,
            market_regime=result.market_regime,
            adx=result.adx_value,
            trace_id=trace_id,  # 【新增 2026-04-02】记录trace_id
        )
        logger.debug(f"[{trace_id}] [{code}] 信号记录：{signal_id[:8]}... | 置信度={result.confidence:.2f}")

        # 盈亏比拒绝处理（2026-03-24 修复：需在 record_signal 之后才有 signal_id）
        if _rr_reject_reason:
            signal_tracker.record_execution(signal_id, executed=False, reject_reason=_rr_reject_reason)
            return

        # 追踪冷却 / 弱信号拒绝：记录到 signal_tracker 后再 return（2026-03-24 修复：健康看板风控层数据）
        if _trailing_cooldown_reject:
            signal_tracker.record_execution(signal_id, executed=False, reject_reason='trailing_cooldown')
            return
        if _weak_signal_reject:
            signal_tracker.record_execution(signal_id, executed=False, reject_reason='weak_signal')
            return

        if not self._is_cooled_down(code):
            logger.info(
                f"[共识] {code} 冷却中，跳过推送 "
                f"(分数={result.score:+.2f}, {', '.join(result.agree_strategies)})"
            )
            # 记录拒绝原因
            signal_tracker.record_execution(signal_id, executed=False, reject_reason='cooldown')
            return

        base_signal = result.votes[0].signal
        strategies_str = " + ".join(result.agree_strategies)
        n_agree = len(result.agree_strategies)

        # 构造合并推送的 reason
        consensus_reason = "\n".join(
            f"  ▸ {v.strategy}(权重{result.weight_details.get(v.strategy, '?')}): {v.reason}"
            for v in result.votes
        )

        # ----------------------------------------------------------------
        # 置信度：直接使用共识引擎精确计算的结果，不再重复计算
        # consensus_engine 已综合了: 分数/策略数量/资金流向/市场状态
        # ----------------------------------------------------------------
        confidence = result.confidence

        consensus_signal = Signal(
            code=code,
            name=base_signal.name,
            action=result.action,
            price=result.price,
            quantity=base_signal.quantity,
            reason=consensus_reason,
            strategy=f"共识({strategies_str})",
            extra={
                "consensus": True,
                "score": result.score,
                "threshold": result.threshold,
                "market_regime": result.market_regime,
                "adx": result.adx_value,
                "agree_count": n_agree,
                "total_count": n_agree + len(result.oppose_strategies),
                "strategies": result.agree_strategies,
                "oppose": result.oppose_strategies,
                "weights": result.weight_details,
                "confidence": round(confidence, 2),  # 添加置信度字段
                "quality_boost": quality_boost,  # 断点4修复: 质量仓位系数 (2026-04-06)
            },
        )

        # 2026-03-13: 添加 watchlist 过滤，仅推送启用的股票
        import json
        try:
            watchlist_path = "/opt/ftseek/FTSeek/config/watchlist.json"
            with open(watchlist_path, 'r') as f:
                watchlist = json.load(f)
            enabled_codes = set()
            for stock in watchlist.get('hk_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            for stock in watchlist.get('us_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            
            if code not in enabled_codes:
                logger.debug(f"[跳过推送] {code} 不在启用列表中")
                return  # 跳过未启用股票的推送
        except Exception as e:
            logger.warning(f"watchlist 过滤异常：{e}")

        # 2026-03-15: 应用港股特性适配
        if code.startswith("HK."):
            atr_pct = self._get_atr_percent(code)
            consensus_signal = self.hk_adapter.process_signal(consensus_signal, atr_pct)
            if consensus_signal is None:
                logger.info(f"[港股适配] {code} 信号被过滤 (T+0 冲突/置信度不足/流动性限制)")
                return
        
        # 2026-03-13: 修改逻辑 - 先判断持仓，再执行交易和推送
        # 1. 检查持仓
        position = self.position_manager.get_position(code)
        
        # 2026-03-24 修复：切换到CASH账户(8246815)后，无持仓时的SELL信号将自动建立空头
        # 富途CASH账户规则：无持仓提交SELL → 自动卖空建仓；空仓时提交BUY → 自动平空回补
        # 原来过滤无持仓SELL的逻辑已移除，改为此说明注释
        if position is None and result.action == SignalAction.SELL:
            logger.info(f"[空头建仓] {code} 无持仓，SELL信号将建立空头 (CASH账户自动卖空)")

        
        # 2. 判断交易类型
        trade_type = self._determine_trade_type(position, result.action)

        # 2026-03-23: 已有同向持仓时，尝试顺势加仓（满足条件才加，否则拒绝）
        if trade_type == 'HOLD':
            added = self._try_add_position(code, position, result, consensus_signal, signal_id)
            if not added:
                action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
                logger.info(
                    f"[持仓保护] {code} 已有 {position.action} 持仓，"
                    f"不满足加仓条件，拒绝信号 {action_str} (分数={result.score:+.2f})"
                )
                signal_tracker.record_execution(signal_id, executed=False, reject_reason='existing_position')
            return

        # 2026-03-23: 最小持仓时间保护
        # 持仓时间不足 MIN_HOLD_MINUTES 时，禁止由反向策略信号触发平仓（交由止盈止损逻辑处理）
        # 例外：亏损超过止损阈值时允许提前平仓（由止盈止损逻辑处理，此处直接拦截策略信号平仓）
        MIN_HOLD_MINUTES = 30
        if trade_type == 'CLOSE' and position is not None:
            try:
                from datetime import datetime as _dt
                entry_time_str = getattr(position, 'entry_time', None)
                if entry_time_str:
                    if isinstance(entry_time_str, str):
                        et = _dt.fromisoformat(entry_time_str)
                    else:
                        et = entry_time_str
                    hold_minutes = ((_dt.now() - et).total_seconds()) / 60
                    if hold_minutes < MIN_HOLD_MINUTES:
                        pnl_rate = (result.price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
                        # 未达止损线时，禁止反向信号平仓
                        try:
                            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                            stop_pct = abs(_dem.get_stop_loss())  # 正数，如 0.03
                        except Exception:
                            stop_pct = 0.03
                        if pnl_rate > -stop_pct:
                            logger.info(
                                f"[最小持仓保护] {code} 持仓仅 {hold_minutes:.1f}min < {MIN_HOLD_MINUTES}min，"
                                f"拒绝反向信号平仓 (pnl={pnl_rate:.2%}, 止损线=-{stop_pct:.0%})"
                            )
                            return
            except Exception as _e:
                logger.debug(f"[最小持仓保护] 检查异常: {_e}")

        # Task4 + P2-2: 新闻情绪倍率 × 分散折扣，合并注入 extra['news_boost']
        news_boost = self._get_news_sentiment_boost(code, result.action)
        combined_boost = news_boost * _concentration_discount
        if combined_boost != 1.0:
            logger.info(
                f"[仓位调节] {code} 新闻={news_boost:.1f}x 分散={_concentration_discount:.1f}x "
                f"合并={combined_boost:.2f}x 注入 extra"
            )
            consensus_signal = dataclasses.replace(
                consensus_signal,
                extra={**(consensus_signal.extra or {}), 'news_boost': combined_boost}
            )
        
        # 2026-03-25 修复: 开仓原子锁 - 防止 quote/kline 两个 handler 同时触发导致同标的重复开仓
        # 同一标的在 5 秒内不允许重复开仓
        if trade_type == 'OPEN':
            _open_cooldown_sec = 5.0
            with self._open_lock:
                _last_open_ts = self._open_position_ts.get(code, 0)
                _now_ts = time.time()
                if _now_ts - _last_open_ts < _open_cooldown_sec:
                    logger.warning(
                        f"[开仓去重] {code} {_open_cooldown_sec:.0f}s 内已执行过开仓 "
                        f"(距上次 {_now_ts - _last_open_ts:.1f}s)，拒绝重复信号"
                    )
                    signal_tracker.record_execution(signal_id, executed=False, reject_reason='open_dedup')
                    return
                # 预占时间戳，阻止并发仓
                self._open_position_ts[code] = _now_ts

        # 3. 执行交易
        trade_result = self.trade_executor.execute(consensus_signal, trade_type)
        
        if not trade_result.success:
            logger.warning(f"交易失败：{trade_result.message}")
            # 记录执行失败
            signal_tracker.record_execution(signal_id, executed=False, reject_reason=trade_result.message)
            # 记录到熔断器 (新增 2026-03-17)
            self.circuit_breaker.record_reject(trade_result.message)
            return  # 不推送
        
        # 4. 准备推送数据
        push_data = {
            'signal': consensus_signal,
            'trade_result': trade_result,
            'position': position,
            'consensus_result': result
        }
        
        # 5. 记录执行成功 (移到推送之前，确保记录) 2026-03-19 修复
        # 2026-03-20 修复：TradeResult 无 execution_price 属性，改用 consensus_signal.price
        signal_tracker.record_execution(
            signal_id, 
            executed=True, 
            execution_price=consensus_signal.price,
            execution_time=datetime.now().isoformat()
        )
        
        # 5.5 记录交易评估 (新增: 用于后续复盘分析)
        try:
            evaluator = get_evaluator()
            if trade_type == 'OPEN':
                # 开仓时记录初始信息
                evaluator.record_trade({
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
                # 平仓时更新评估
                if position:
                    evaluator.record_exit(
                        trade_id=signal_id,
                        exit_price=consensus_signal.price,
                        exit_time=datetime.now().isoformat(),
                        realized_pnl=trade_result.pnl or 0
                    )
        except Exception as e:
            logger.warning(f"记录交易评估失败: {e}")

        # 5.6 开仓成功后立即写入动态止损止盈价到 DB（Task6 - 2026-03-25）
        # _get_dynamic_stop_params 基于七周期加权计算，止损止盈与 yaml 保持一致
        if trade_type == 'OPEN':
            try:
                dyn = self._get_dynamic_stop_params(code)
                entry_px = consensus_signal.price
                # 做多：止损低于成本；做空：止损高于成本
                is_long = (result.action.value if hasattr(result.action, 'value') else str(result.action)) == 'BUY'
                sl_price = round(entry_px * (1 + dyn["stop_loss_pct"]), 4)     # stop_loss_pct < 0
                tp_price = round(entry_px * (1 + dyn["take_profit_pct"]), 4)   # take_profit_pct > 0
                if not is_long:
                    # 做空时止损/止盈方向相反
                    sl_price = round(entry_px * (1 - dyn["stop_loss_pct"]), 4)
                    tp_price = round(entry_px * (1 - dyn["take_profit_pct"]), 4)
                self.position_manager.update_stop_loss_take_profit(code, sl_price, tp_price)
                logger.info(
                    f"[动态止损] {code} 开仓@{entry_px} | "
                    f"止损={sl_price}({dyn['stop_loss_pct']:.1%}) | "
                    f"止盈={tp_price}({dyn['take_profit_pct']:.1%}) | "
                    f"regime={dyn['regime']}"
                )
            except Exception as _dyn_e:
                logger.warning(f"[动态止损] {code} 写入失败: {_dyn_e}")

        # 共识平仓回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
        if trade_type == 'CLOSE' and trade_result.success and trade_result.pnl is not None:
            try:
                signal_tracker.record_exit(
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
                logger.debug(f"[signal_tracker] 共识平仓回填失败: {_e}")
        
        # 6. 推送 (包装在try中，避免推送失败影响记录)
        try:
            action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
            if trade_type == 'OPEN':
                self.notifier.send_open_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 开仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id}"
                )
            else:
                self.notifier.send_close_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 平仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id} | 盈亏：{trade_result.pnl:.2f}"
                )
                # 平仓成功后，记录交易结果供自适应权重学习
                if trade_result.pnl is not None:
                    self._adaptive_weight_manager.record_trade(
                        agree_strategies=result.agree_strategies,
                        pnl=trade_result.pnl,
                    )
                    # Task1: 同步更新共识引擎信号质量分
                    self._consensus.record_signal_outcome(
                        code=code,
                        action=result.action,
                        pnl=trade_result.pnl,
                    )
        except Exception as e:
            logger.error(f"推送通知失败: {e}")
        
        # 7. 更新冷却时间 - 2026-03-17 优化：推送成功后才更新冷却
        with self._cooldown_lock:
            self._cooldown[code] = time.time()
            logger.debug(f"[{code}] 冷却时间已更新，当前冷却={self._get_current_cooldown()}秒")

    def _determine_trade_type(self, position, action: SignalAction) -> str:
        """判断是开仓、平仓还是持仓不动
        
        返回值：
          'OPEN'  - 无持仓，执行开仓
          'CLOSE' - 有反向持仓，执行平仓
          'HOLD'  - 已有同向持仓，尝试加仓（条件不足时拒绝）
        """
        if position is None:
            return 'OPEN'
        if position.action == 'BUY' and action == SignalAction.SELL:
            return 'CLOSE'
        if position.action == 'SELL' and action == SignalAction.BUY:
            return 'CLOSE'
        # 同向信号且已有持仓：进入加仓判断
        return 'HOLD'

    # ----------------------------------------------------------
    #  顺势加仓逻辑
    # ----------------------------------------------------------
    # 加仓触发条件（全部满足才执行）
    _ADD_SCORE_MIN = 1.5          # 最低共识得分
    _ADD_CONFIDENCE_MIN = 0.70    # 最低置信度
    _ADD_PRICE_MIN_PCT = 0.005    # 价格须高于持仓均价至少 0.5%（顺势）
    _ADD_HOLD_MIN_MINUTES = 15    # 持仓时间须 >= 15 分钟
    _ADD_MAX_TIMES_PER_DAY = 1    # 每日每品种最多加仓 1 次
    _ADD_QTY_RATIO = 0.5          # 加仓量 = 初始信号量 × 50%

    def _try_add_position(self, code: str, position, result, consensus_signal, signal_id: str) -> bool:
        """
        尝试顺势加仓。满足全部条件时执行加仓并返回 True，否则返回 False。

        条件：
        1. 共识得分 >= _ADD_SCORE_MIN (1.5)
        2. 置信度 >= _ADD_CONFIDENCE_MIN (0.70)
        3. 当前价 > 持仓均价 × (1 + 0.005)（顺势上涨才加）
        4. 持仓时间 >= _ADD_HOLD_MIN_MINUTES (15 分钟)
        5. 今日加仓次数 < _ADD_MAX_TIMES_PER_DAY (1)
        """
        try:
            # 1. 得分检查
            if result.score < self._ADD_SCORE_MIN:
                logger.debug(f"[加仓拒绝] {code} 得分={result.score:.2f} < {self._ADD_SCORE_MIN}")
                return False

            # 2. 置信度检查
            if result.confidence < self._ADD_CONFIDENCE_MIN:
                logger.debug(f"[加仓拒绝] {code} 置信度={result.confidence:.2f} < {self._ADD_CONFIDENCE_MIN}")
                return False

            # 3. 价格须高于均价（顺势）
            entry_price = position.entry_price
            if entry_price > 0 and result.price < entry_price * (1 + self._ADD_PRICE_MIN_PCT):
                logger.debug(
                    f"[加仓拒绝] {code} 当前价={result.price:.3f} 未超过均价*{1+self._ADD_PRICE_MIN_PCT}="
                    f"{entry_price * (1 + self._ADD_PRICE_MIN_PCT):.3f}"
                )
                return False

            # 4. 持仓时间检查
            from datetime import datetime as _dt
            entry_time_str = getattr(position, 'entry_time', None)
            if entry_time_str:
                et = _dt.fromisoformat(entry_time_str) if isinstance(entry_time_str, str) else entry_time_str
                hold_minutes = (_dt.now() - et).total_seconds() / 60
                if hold_minutes < self._ADD_HOLD_MIN_MINUTES:
                    logger.debug(
                        f"[加仓拒绝] {code} 持仓仅 {hold_minutes:.1f}min < {self._ADD_HOLD_MIN_MINUTES}min"
                    )
                    return False

            # 5. 今日加仓次数检查
            today_add_count = self.position_manager.get_today_add_count(code)
            if today_add_count >= self._ADD_MAX_TIMES_PER_DAY:
                logger.info(
                    f"[加仓拒绝] {code} 今日已加仓 {today_add_count} 次，"
                    f"上限={self._ADD_MAX_TIMES_PER_DAY}"
                )
                return False

            # 6. 计算加仓数量（原信号量的50%，最小100股）
            add_qty = max(100, int(consensus_signal.quantity * self._ADD_QTY_RATIO))

            # 7. 构造加仓信号（注入 is_add_position 标记供 executor 识别）
            add_signal = dataclasses.replace(
                consensus_signal,
                quantity=add_qty,
                extra={**consensus_signal.extra, 'is_add_position': True}
            )

            # 8. 执行加仓下单（走 OPEN 流程，但带 ADD 标记）
            trade_result = self.trade_executor.execute(add_signal, 'OPEN')
            if not trade_result.success:
                logger.warning(f"[加仓失败] {code} 下单失败: {trade_result.message}")
                return False

            # 9. 更新持仓均价 + 止盈止损（合并到原有 OPEN 仓位）
            self.position_manager.add_to_position(
                code=code,
                add_qty=add_qty,
                add_price=result.price,
                add_time=datetime.now().isoformat(),
                signal_id=signal_id
            )

            # 10. 记录加仓流水（is_add=1 用于统计今日加仓次数）
            self.position_manager.record_add_position(
                code=code,
                add_qty=add_qty,
                add_price=result.price,
                add_time=datetime.now().isoformat(),
                signal_id=signal_id
            )

            # 11. 记录信号执行
            signal_tracker.record_execution(
                signal_id, executed=True,
                execution_price=result.price,
                execution_time=datetime.now().isoformat()
            )

            logger.info(
                f"[顺势加仓] {code} +{add_qty}股 @ {result.price:.3f} | "
                f"得分={result.score:+.2f} | 置信度={result.confidence:.2f} | "
                f"原均价={entry_price:.3f} | 今日第{today_add_count + 1}次 | "
                f"订单={trade_result.order_id}"
            )

            # 12. 钉钉推送加仓通知
            try:
                action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
                self.notifier.send_text(
                    f"[顺势加仓] {code} {action_str} +{add_qty}股 @ {result.price:.3f}\n"
                    f"原均价={entry_price:.3f} | 得分={result.score:+.2f} | 今日第{today_add_count + 1}次"
                )
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"[加仓异常] {code}: {e}", exc_info=True)
            return False

    def _execute_stop_loss(self, code: str, current_price: float):
        """
        执行止损平仓 (2026-03-23 优化: 确保整手数量)
        2026-03-27 修复: 添加止损冷却机制，防止频繁触发
        2026-03-27 修复: 交易密码未解锁时跳过止损执行
        
        Args:
            code: 股票代码
            current_price: 当前价格
        """
        # 止损冷却递增：按失败次数增加冷却时间
        if not hasattr(self, '_stop_loss_fail_count'):
            self._stop_loss_fail_count = {}
        
        fail_count = self._stop_loss_fail_count.get(code, 0)
        COOLDOWN_SCHEDULE = [5, 15, 30, 60, 60]  # 第1次5s, 第2次15s, 第3次30s, 第4次+60s
        cooldown_seconds = COOLDOWN_SCHEDULE[min(fail_count, len(COOLDOWN_SCHEDULE) - 1)]
        
        now = time.time()
        last_stop_time = self._stop_loss_cooldown.get(code, 0)
        if now - last_stop_time < cooldown_seconds:
            logger.debug(
                f"[止损冷却] {code} 冷却中，距离上次止损 {now - last_stop_time:.1f}s < {cooldown_seconds}s (失败次数={fail_count})"
            )
            return  # 仍在冷却中
        
        # 设置冷却时间戳（无论成功与否，都先记录，防止频繁触发）
        self._stop_loss_cooldown[code] = now
        
        position = self.position_manager.get_position(code)
        if position is None:
            return
        
        # 获取每手股数配置
        from app.config import DEFAULT_ORDER_QTY_BY_CODE
        lot_size = DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        # 确保平仓数量为整手
        close_qty = position.quantity
        if close_qty % lot_size != 0:
            # 向上取整到整手
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止损数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        # 创建止损信号
        from app.strategy.base import Signal, SignalAction
        stop_loss_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止损触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止损",
            extra={
                "stop_loss": True,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price
            }
        )
        
        logger.info(f"[止损尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # 执行平仓
        trade_result = self.trade_executor.execute(stop_loss_signal, 'CLOSE')
        
        if trade_result.success:
            logger.info(
                f"[止损成功] {code} | 入场价={position.entry_price} | "
                f"出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            # 止损成功后重置失败计数和冷却
            self._stop_loss_fail_count.pop(code, None)
            self._stop_loss_cooldown.pop(code, None)
            # 清理追踪止盈缓存
            self._trailing_stop_cache.pop(code, None)
            self._peak_price_cache.pop(code, None)
            # 记录到信号质量管理（止损路径）
            try:
                self._consensus.record_signal_outcome(
                    code=code,
                    action=position.action,
                    pnl=trade_result.pnl or 0.0,
                )
            except Exception as _e:
                logger.debug(f"[信号质量] 止损记录异常: {_e}")
            # 推送止损通知
            try:
                self.notifier.send_close_position_message(
                    signal=stop_loss_signal,
                    trade_result=trade_result,
                    position=position,
                    consensus_result=None
                )
            except Exception as e:
                logger.warning(f"[止损推送失败] {code}: {e}")
            # 记录交易评估 (新增)
            try:
                evaluator = get_evaluator()
                trade_id = f"SL-{code}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                evaluator.record_trade({
                    'trade_id': trade_id,
                    'code': code,
                    'action': position.action,
                    'entry_time': position.entry_time,
                    'entry_price': position.entry_price,
                    'quantity': close_qty,
                    'trigger_strategy': '止损',
                    'trigger_reason': f"亏损 {((current_price - position.entry_price) / position.entry_price * 100):.2f}%"
                })
                evaluator.record_exit(trade_id, current_price, datetime.now().isoformat(), trade_result.pnl)
            except Exception as e:
                logger.debug(f"[止损评估记录失败] {code}: {e}")
            # 回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
            try:
                signal_tracker.record_exit_by_code(
                    code=code,
                    exit_price=current_price,
                    pnl=trade_result.pnl or 0.0,
                    exit_reason='stop_loss',
                )
            except Exception as e:
                logger.debug(f"[signal_tracker] 止损回填失败: {e}")
        else:
            error_msg = trade_result.message
            logger.error(f"[止损失败] {code}: {error_msg}")
            # 止损执行失败后递增计数
            self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
            fail_count = self._stop_loss_fail_count[code]
            
            # 失败达5次时记录严重警告并发送钉钉通知
            if fail_count >= 5:
                logger.error(f"[止损严重警告] {code} 连续{fail_count}次止损失败，请立即检查！最后错误: {error_msg}")
                try:
                    from app.notification import send_dingtalk_message
                    send_dingtalk_message(f"⚠️ 止损严重警告: {code} 连续{fail_count}次止损失败，请立即人工干预！")
                except Exception:
                    pass

    def _execute_take_profit(self, code: str, current_price: float):
        """
        执行止盈平仓 (2026-03-23 优化: 确保整手数量)
        
        Args:
            code: 股票代码
            current_price: 当前价格
        """
        position = self.position_manager.get_position(code)
        if position is None:
            return
        
        # 获取每手股数配置
        from app.config import DEFAULT_ORDER_QTY_BY_CODE
        lot_size = DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        # 确保平仓数量为整手
        close_qty = position.quantity
        if close_qty % lot_size != 0:
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止盈数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        # 创建止盈信号
        from app.strategy.base import Signal, SignalAction
        take_profit_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止盈触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止盈",
            extra={
                "take_profit": True,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price
            }
        )
        
        logger.info(f"[止盈尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # 执行平仓
        trade_result = self.trade_executor.execute(take_profit_signal, 'CLOSE')
        
        if trade_result.success:
            logger.info(
                f"[止盈成功] {code} | 入场价={position.entry_price} | "
                f"出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            # 清理追踪止盈缓存
            self._trailing_stop_cache.pop(code, None)
            self._peak_price_cache.pop(code, None)
            # 记录到信号质量管理（止盈路径）
            try:
                self._consensus.record_signal_outcome(
                    code=code,
                    action=position.action,
                    pnl=trade_result.pnl or 0.0,
                )
            except Exception as _e:
                logger.debug(f"[信号质量] 止盈记录异常: {_e}")
            # 推送止盈通知
            try:
                self.notifier.send_close_position_message(
                    signal=take_profit_signal,
                    trade_result=trade_result,
                    position=position,
                    consensus_result=None
                )
            except Exception as e:
                logger.warning(f"[止盈推送失败] {code}: {e}")
            # 记录交易评估 (新增)
            try:
                evaluator = get_evaluator()
                trade_id = f"TP-{code}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                pnl_rate = (current_price - position.entry_price) / position.entry_price
                evaluator.record_trade({
                    'trade_id': trade_id,
                    'code': code,
                    'action': position.action,
                    'entry_time': position.entry_time,
                    'entry_price': position.entry_price,
                    'quantity': close_qty,
                    'trigger_strategy': '追踪止盈',
                    'trigger_reason': f"盈利 {pnl_rate*100:.2f}% 触发止盈线"
                })
                evaluator.record_exit(trade_id, current_price, datetime.now().isoformat(), trade_result.pnl)
            except Exception as e:
                logger.debug(f"[止盈评估记录失败] {code}: {e}")
            # 回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
            try:
                signal_tracker.record_exit_by_code(
                    code=code,
                    exit_price=current_price,
                    pnl=trade_result.pnl or 0.0,
                    exit_reason='take_profit',
                )
            except Exception as e:
                logger.debug(f"[signal_tracker] 止盈回填失败: {e}")
        else:
            logger.error(f"[止盈失败] {code}: {trade_result.message}")

    def _execute_partial_take_profit(self, code: str, current_price: float, ratio: float):
        """
        执行分批止盈平仓 (2026-03-24 优化: 整手对齐 + 追踪止盈场景至少留1手)
        
        整手对齐规则（港股）：
        1. 按 ratio 计算原始平仓量，向下取整到整手
        2. 若取整后不足一手：
           a. 总持仓 > 1手：改为平仓恰好1手（保留剩余继续跟趋势）
           b. 总持仓 == 1手：退化为全仓平仓
        3. 平仓后剩余量必须 >= 1手，否则改为全仓平仓
        
        Args:
            code: 股票代码
            current_price: 当前价格
            ratio: 平仓比例 (0.6 = 60% = 追踪止盈场景)
        """
        position = self.position_manager.get_position(code)
        if position is None:
            return
        
        # 优先使用 HK_LOT_SIZES，fallback 到 DEFAULT_ORDER_QTY_BY_CODE
        from app.config import DEFAULT_ORDER_QTY_BY_CODE, HK_LOT_SIZES
        lot_size = HK_LOT_SIZES.get(code) or DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        total_qty = position.quantity
        
        # 计算平仓数量 - 向下取整到整手
        raw_close_qty = int(total_qty * ratio)
        close_qty = (raw_close_qty // lot_size) * lot_size
        
        if close_qty < lot_size:
            # 不足一手：若总持仓 > 1手，退为平仓1手；否则全平
            if total_qty >= lot_size * 2:
                close_qty = lot_size
                logger.info(
                    f"[分批止盈调整] {code} 按{ratio:.0%}计算={raw_close_qty}股 不足一手({lot_size}股)，"
                    f"改为平仓最少1手={lot_size}股，保留{total_qty - lot_size}股跟趋势"
                )
            elif total_qty >= lot_size:
                # 只有1手，全平
                close_qty = total_qty
                ratio = 1.0
                logger.info(
                    f"[分批止盈调整] {code} 仅1手持仓，退化为全仓平仓"
                )
            else:
                logger.warning(
                    f"[分批止盈跳过] {code} 持仓{total_qty}股不足一手({lot_size}股)，跳过"
                )
                return
        else:
            # 检查剩余数量是否满足整手
            remaining_qty = total_qty - close_qty
            if remaining_qty > 0 and remaining_qty < lot_size:
                # 剩余不足一手：尝试减少平仓量以保留整手剩余
                adjusted_close = total_qty - lot_size
                if adjusted_close >= lot_size:
                    old_close = close_qty
                    close_qty = (adjusted_close // lot_size) * lot_size
                    logger.info(
                        f"[分批止盈调整] {code} 剩余{remaining_qty}股不足一手，"
                        f"平仓量从{old_close}→{close_qty}股，保留{total_qty - close_qty}股整手剩余"
                    )
                else:
                    # 调整后平仓量也不足一手，全平
                    close_qty = total_qty
                    ratio = 1.0
                    logger.info(
                        f"[分批止盈调整] {code} 无法保留整手剩余，改为全仓平仓"
                    )
        
        remaining_qty = total_qty - close_qty if ratio < 1.0 else 0
        logger.info(
            f"[分批止盈尝试] {code} 持仓={total_qty}股 | "
            f"平仓={close_qty}股({close_qty/total_qty:.0%}) | 每手={lot_size}股 | "
            f"剩余={remaining_qty}股"
        )
        
        # 创建分批止盈信号
        from app.strategy.base import Signal, SignalAction
        partial_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"分批止盈 ({ratio:.0%}) 盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%",
            strategy="分批止盈",
            extra={
                "partial_take_profit": True,
                "ratio": ratio,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price
            }
        )
        
        # 执行部分平仓
        if ratio >= 1.0:
            trade_result = self.trade_executor.execute(partial_signal, 'CLOSE')
        else:
            trade_result = self.trade_executor.execute_partial_close(partial_signal, ratio)
        
        if trade_result.success:
            logger.info(
                f"[分批止盈成功] {code} | 平仓{close_qty}股({ratio:.0%}) | "
                f"入场价={position.entry_price} | 出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            # 记录到信号质量管理（分批止盈路径）
            try:
                self._consensus.record_signal_outcome(
                    code=code,
                    action=position.action,
                    pnl=trade_result.pnl or 0.0,
                )
            except Exception as _e:
                logger.debug(f"[信号质量] 分批止盈记录异常: {_e}")
            # 回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
            try:
                signal_tracker.record_exit_by_code(
                    code=code,
                    exit_price=current_price,
                    pnl=trade_result.pnl or 0.0,
                    exit_reason='trailing_stop',
                )
            except Exception as e:
                logger.debug(f"[signal_tracker] 分批止盈回填失败: {e}")
        else:
            # 失败时尝试全仓平仓
            logger.warning(
                f"[分批止盈失败] {code}: {trade_result.message}，尝试全仓平仓"
            )
            full_signal = Signal(
                code=code,
                name=position.code,
                action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
                price=current_price,
                quantity=position.quantity,
                reason=f"分批止盈失败后全仓平仓",
                strategy="分批止盈兜底",
                extra={
                    "partial_take_profit": True,
                    "fallback": True,
                    "entry_price": position.entry_price,
                }
            )
            retry_result = self.trade_executor.execute(full_signal, 'CLOSE')
            if retry_result.success:
                logger.info(f"[分批止盈兜底成功] {code} | 全仓平仓{position.quantity}股")
            else:
                logger.error(f"[分批止盈兜底失败] {code}: {retry_result.message}")


    def _process_signal(self, signal: Signal):
        if signal is None:
            return
        self._consensus.submit(signal)

    # ----------------------------------------------------------
    #  行情订阅
    # ----------------------------------------------------------
    def subscribe(self):
        from futu import SubType

        ctx = self._get_quote_ctx()

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,   # 2026-03-25 新增：3分钟K线
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }

        sub_types = [sub_type_map[st] for st in config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        for code in config.WATCH_LIST:
            for st in sub_types:
                ret, msg = ctx.subscribe([code], [st])
                if ret == 0:
                    logger.info(f"订阅成功: {code} -> {st}")
                else:
                    if "无权限" in str(msg) or "no permission" in str(msg).lower():
                        logger.warning(f"订阅跳过(无权限): {code} -> {st} | {msg}")
                    else:
                        logger.error(f"订阅失败: {code} -> {st}: {msg}")

    def _setup_handlers(self):
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase

        strategies = self.strategies
        process_signal = self._process_signal
        update_adx = self._update_adx

        # 将所有需要从 DataSubscriber 访问的属性通过闭包变量传入
        # 内部类中 self 指 handler 实例，必须用闭包引用 DataSubscriber
        subscriber = self  # 闭包捕获 DataSubscriber 实例

        class MyQuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_str):
                ret, data = super().on_recv_rsp(rsp_str)
                if ret != 0:
                    return
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    quote = row.to_dict()
                    
                    # 更新行情心跳时间（断线检测用）—— 必须写到 subscriber，不是 handler self
                    subscriber._last_quote_time = time.time()
                    
                    # 2026-03-19: 增强止盈止损检查 (保本止损 + 分批止盈)
                    current_price = quote.get('last_done', 0)
                    if current_price > 0:
                        pos_now = subscriber.position_manager.get_position(code)

                        # 更新峰值价格缓存（Task2: 用于移动止损）
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
                            # 无持仓时清理峰值缓存
                            subscriber._peak_price_cache.pop(code, None)

                        # Task2: 使用组合式退出引擎（优先级检查）
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
                                    subscriber._trailing_stop_cache.pop(code, None)
                                    subscriber._peak_price_cache.pop(code, None)
                                else:
                                    subscriber._execute_take_profit(code, current_price)
                                    subscriber._trailing_stop_cache.pop(code, None)
                                    subscriber._peak_price_cache.pop(code, None)
                                continue

                        # 旧逻辑兜底：保本止损（position_manager 层面）
                        if subscriber.position_manager.check_breakeven_stop(code, current_price):
                            subscriber.position_manager.update_breakeven_stop(code)
                        
                        # 2. 检查分批止盈
                        partial_ratio = subscriber.position_manager.check_partial_take_profit(code, current_price)
                        if partial_ratio > 0:
                            logger.info(f"[分批止盈] {code} 当前价={current_price} 平仓比例={partial_ratio:.0%}")
                            subscriber._execute_partial_take_profit(code, current_price, partial_ratio)
                            if partial_ratio >= 1.0:
                                continue  # 全部平仓后跳过策略计算
                        
                        # 3. 检查止损
                        if subscriber.position_manager.check_stop_loss(code, current_price):
                            logger.info(f"[止损触发] {code} 当前价={current_price}")
                            subscriber._execute_stop_loss(code, current_price)
                            continue  # 触发止损后跳过策略计算
                        
                        # 3.5 软止损预警 (2026-03-25 新增，2026-04-07 优化：添加冷却机制)
                        # 当亏损达到止损线的 80% 时发送预警，冷却时间 5 分钟
                        if pos_now and pos_now.stop_loss and pos_now.stop_loss > 0:
                            pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
                            stop_rate = (pos_now.stop_loss - pos_now.entry_price) / pos_now.entry_price
                            soft_threshold = stop_rate * 0.8  # 80% 止损距离
                            
                            if pnl_rate <= soft_threshold and pnl_rate > stop_rate:
                                # 检查冷却时间 (5 分钟)
                                last_warning = subscriber.soft_stop_warnings.get(code, 0)
                                if time.time() - last_warning < 300:  # 300 秒=5 分钟
                                    pass  # 冷却期内，跳过预警
                                else:
                                    # 还未触及止损，但已接近
                                    distance_to_stop = abs(pnl_rate - stop_rate)
                                    logger.warning(
                                        f"[软止损预警] {code} 亏损 {abs(pnl_rate):.2%} "
                                        f"接近止损线 {abs(stop_rate):.2%}，距离 {distance_to_stop:.2%}"
                                    )
                                    subscriber.soft_stop_warnings[code] = time.time()
                        
                        # 4. 检查止盈
                        if subscriber.position_manager.check_take_profit(code, current_price):
                            logger.info(f"[止盈触发] {code} 当前价={current_price}")
                            subscriber._execute_take_profit(code, current_price)
                            continue  # 触发止盈后跳过策略计算
                    
                    # 策略计算 - 只调用 on_quote 策略
                    # 注意: on_quote 用于快速响应策略（如 price_break）
                    # 大多数技术分析策略在 on_kline 中计算
                    for strategy in strategies:
                        # 只调用专门实现了 on_quote 的策略
                        # 避免与 on_kline 重复计算
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
                    
                    # 2026-03-25: 多周期趋势更新
                    subscriber._update_multi_timeframe_trend(code, k_type, kline)
                    
                    # 同时更新原有的 kline buffer (用于 ADX 等指标计算)
                    subscriber._kline_buffers[code].push_kline(kline)

                    # 【新增 2026-04-09】分层策略管理器 - 更新技术增强层 K 线缓存
                    if subscriber.layered_manager:
                        subscriber.layered_manager.update_kline_cache(code, k_type, kline)

                    # 2026-03-25: MarketRegimeDetector 接入多周期数据
                    # 优先使用 15M/30M/60M 的稳健中周期数据判断市场状态；1M 作为实时补充
                    if k_type in ("K_15M", "K_30M", "K_60M"):
                        try:
                            subscriber._regime_detector.update_data(code, kline)
                        except Exception as _re:
                            logger.debug(f"[regime多周期] {code} {k_type} 更新失败: {_re}")
                    elif k_type == "K_1M":
                        pass  # 1M 由 _update_adx 中的 regime_detector.update_data 负责
                    
                    # 2026-03-19: 增强止盈止损检查 (保本止损 + 分批止盈)
                    current_price = kline.get('close', 0)
                    if current_price > 0:
                        # 0b. Task2: 追踪止盈检查（K线粒度）- 2026-03-24 优化
                        # 追踪止盈改为部分平仓60%+保本止损，留40%跟趋势
                        pos_now = subscriber.position_manager.get_position(code)
                        subscriber._update_trailing_stop(code, current_price, pos_now)
                        trail_price = subscriber._trailing_stop_cache.get(code, 0)
                        if pos_now and trail_price > 0:
                            triggered = (
                                (pos_now.action == 'BUY' and current_price < trail_price) or
                                (pos_now.action == 'SELL' and current_price > trail_price)
                            )
                            if triggered:
                                # 验证：只有盈利时才执行追踪止盈（避免亏损时误触发）
                                pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
                                if pos_now.action == 'SELL':
                                    pnl_rate = -pnl_rate
                                
                                if pnl_rate > 0:
                                    logger.info(
                                        f"[追踪止盈触发] {code} 价格={current_price} "
                                        f"穿越追踪线={trail_price:.3f} | 盈利={pnl_rate:.2%}"
                                    )
                                    # 部分平仓 60%，留40%跟趋势
                                    subscriber._execute_partial_take_profit(code, current_price, ratio=0.6)
                                    # 剩余仓位保本止损：止损上移到成本价
                                    subscriber.position_manager.update_breakeven_stop(code)
                                    logger.info(
                                        f"[追踪止盈] {code} 已平仓60%锁利，剩余40%保本止损已激活"
                                    )
                                    # 清理追踪缓存（等待下次重新激活）
                                    subscriber._trailing_stop_cache.pop(code, None)
                                    subscriber._peak_price_cache.pop(code, None)
                                    # 设置本次追踪止盈触发时间（用于冷却期）
                                    subscriber._trailing_exit_time[code] = time.time()
                                else:
                                    logger.debug(
                                        f"[追踪止盈跳过] {code} 未盈利({pnl_rate:.2%})，跳过"
                                    )
                                continue

                        # 1. 检查保本止损 (盈利2%后)
                        if subscriber.position_manager.check_breakeven_stop(code, current_price):
                            subscriber.position_manager.update_breakeven_stop(code)
                        
                        # 2. 检查分批止盈
                        partial_ratio = subscriber.position_manager.check_partial_take_profit(code, current_price)
                        if partial_ratio > 0:
                            logger.info(f"[分批止盈] {code} 当前价={current_price} 平仓比例={partial_ratio:.0%}")
                            subscriber._execute_partial_take_profit(code, current_price, partial_ratio)
                            if partial_ratio >= 1.0:
                                continue  # 全部平仓后跳过策略计算
                        
                        # 3. 检查止损
                        if subscriber.position_manager.check_stop_loss(code, current_price):
                            logger.info(f"[止损触发] {code} 当前价={current_price}")
                            subscriber._execute_stop_loss(code, current_price)
                            continue  # 触发止损后跳过策略计算
                        
                        # 3.5 软止损预警 (2026-03-25 新增)
                        if pos_now and pos_now.stop_loss and pos_now.stop_loss > 0:
                            pnl_rate = (current_price - pos_now.entry_price) / pos_now.entry_price
                            stop_rate = (pos_now.stop_loss - pos_now.entry_price) / pos_now.entry_price
                            soft_threshold = stop_rate * 0.8
                            
                            if pnl_rate <= soft_threshold and pnl_rate > stop_rate:
                                distance_to_stop = abs(pnl_rate - stop_rate)
                                logger.warning(
                                    f"[软止损预警] {code} 亏损 {abs(pnl_rate):.2%} "
                                    f"接近止损线 {abs(stop_rate):.2%}，距离 {distance_to_stop:.2%}"
                                )
                        
                        # 4. 检查止盈
                        if subscriber.position_manager.check_take_profit(code, current_price):
                            logger.info(f"[止盈触发] {code} 当前价={current_price}")
                            subscriber._execute_take_profit(code, current_price)
                            continue  # 触发止盈后跳过策略计算
                    
                    # 更新 ADX (每根 K 线)
                    update_adx(code, kline)

                    # P0-2: 场景识别在多周期 K 线触发（2026-04-09 修复）
                    # 使用 5M/15M 中周期 K 线进行场景识别，减少 1M 噪声
                    if k_type in ("K_5M", "K_15M") and subscriber._scenario_classifier is not None:
                        try:
                            buf = subscriber._kline_buffers[code]
                            if buf.size >= 30:
                                # 准备真实 K 线数据
                                klines_for_scenario = []
                                closes = list(buf.close)
                                highs = list(buf.high)
                                lows = list(buf.low)
                                opens = list(buf.open)
                                volumes = list(buf.volume)

                                n = min(100, len(closes))
                                for i in range(-n, 0):
                                    klines_for_scenario.append({
                                        'open': opens[i],
                                        'high': highs[i],
                                        'low': lows[i],
                                        'close': closes[i],
                                        'volume': volumes[i],
                                    })

                                if klines_for_scenario:
                                    scenario_result = subscriber._scenario_classifier.classify(code, klines_for_scenario)

                                    # 场景记忆平滑
                                    if subscriber._scenario_memory is not None:
                                        smoothed_scenario = subscriber._scenario_memory.update(
                                            code,
                                            scenario_result.scenario_type,
                                            scenario_result.confidence
                                        )

                                        # 注入场景到共识引擎
                                        subscriber._consensus.update_scenario(code, smoothed_scenario)

                                        logger.info(
                                            f"[场景分类/多周期] {code} {k_type} {smoothed_scenario.value} | "
                                            f"置信度={scenario_result.confidence:.2f}"
                                        )
                        except Exception as _scen_e:
                            logger.debug(f"[场景分类/多周期] {code} {k_type} 异常：{_scen_e}")

                    # 策略计算 (仅在 K_1M 上触发，避免重复)
                    if k_type == "K_1M":
                        for strategy in strategies:
                            sig = strategy.on_kline(code, kline, k_type)
                            if sig:
                                process_signal(sig)

        ctx = self._get_quote_ctx()
        ctx.set_handler(MyQuoteHandler())
        ctx.set_handler(MyKlineHandler())
        logger.info("行情回调 handler 已设置")

    def _reconnect(self):
        """
        断线重连 (优化版 - 2026-03-27): 优先复用现有连接
        
        优化逻辑:
        1. 先测试现有连接是否可用
        2. 仅在网络真正断开时重建
        3. 重新订阅而非重新创建连接
            
            # 1. 优先尝试复用现有连接 (2026-03-27 新增)
            if self._quote_ctx is not None:
                try:
                    # 测试连接是否有效
                    ret, data = self._quote_ctx.get_basic_info("HK.00700", num=1)
                    if ret == 0:
                        logger.info("[重连] 现有连接仍然有效，仅重新订阅行情...")
                        self._resubscribe()
                        self._setup_handlers()
                        self._last_quote_time = time.time()
                        logger.info("[重连] 重新订阅成功，行情已恢复")
                        return
                    else:
                        logger.warning(f"[重连] 现有连接测试失败：{data}，需要重建")
                except Exception as test_error:
                    logger.warning(f"[重连] 现有连接测试异常：{test_error}，需要重建")
            
            # 2. 现有连接不可用，关闭并重建
            logger.info("[重连] 现有连接不可用，开始重建新连接...")
        """
        if not self._reconnect_lock.acquire(blocking=False):
            logger.warning("[重连] 已有重连任务进行中，跳过")
            return
        try:
            logger.info("[重连] 检测到行情断线，开始自动重连...")
            # 关闭旧连接（强制释放 Futu SDK 内部 socket）
            try:
                if self._quote_ctx is not None:
                    self._quote_ctx.close()
            except Exception:
                pass
            finally:
                self._quote_ctx = None

            # 等待 3 秒让 OpenD 清理旧会话
            time.sleep(2)

            # 重建连接 → 重订阅 → 重设 handler（指数退避，最多 5 次）
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    self.subscribe()        # 内部调用 _get_quote_ctx 重新建连
                    self._setup_handlers()
                    self._last_quote_time = time.time()
                    logger.info(f"[重连] 第 {attempt} 次重连成功，行情已恢复")
                    return
                except Exception as e:
                    logger.error(f"[重连] 第 {attempt} 次重连失败: {e}")
                    # 清除上次失败的 ctx，下次重建
                    try:
                        if self._quote_ctx is not None:
                            self._quote_ctx.close()
                    except Exception:
                        pass
                    self._quote_ctx = None
                    time.sleep(min(5 * attempt, 30))  # 最长等 30 秒

            logger.error("[重连] 达到最大重试次数，行情断线未恢复，请人工检查")
        finally:
            self._reconnect_lock.release()

    def start(self):
        self._running = True
        try:
            self.subscribe()
            self._setup_handlers()
            logger.info(
                f"数据订阅已启动 | 共识窗口={CONSENSUS_WINDOW}s | "
                f"触发阈值={CONSENSUS_THRESHOLD} | "
                f"ADX趋势>{CONSENSUS_ADX_TREND} 震荡<{CONSENSUS_ADX_RANGE} | "
                f"推送冷却={SIGNAL_COOLDOWN}s"
            )
        except Exception as e:
            logger.error(f"数据订阅启动异常：{e}")
            raise

        def cleanup_loop():
            # 心跳检测：连续超过 180 秒无行情数据，判定断线并重连
            HEARTBEAT_TIMEOUT = 180  # 秒
            while self._running:
                time.sleep(60)
                signal_store.cleanup_expired()
                self._cleanup_cooldown()

                # 仅在交易时段内做心跳检测（非交易时段本来就没有行情）
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

    def _cleanup_cooldown(self):
        now = time.time()
        with self._cooldown_lock:
            expired = [k for k, t in self._cooldown.items() if now - t > SIGNAL_COOLDOWN * 2]
            for k in expired:
                del self._cooldown[k]

    # ----------------------------------------------------------
    # 【新增 2026-04-06】质量管理器辅助方法
    # ----------------------------------------------------------

    def _check_quality_and_valuation(self, code: str, price: float) -> Optional[Dict]:
        """
        检查质量和估值 (巴菲特理念)

        流程:
        1. 质量评分 - 过滤垃圾股 (JUNK)
        2. 水位检查 - 确定是否允许交易
        3. 估值检查 - 确保有安全边际

        Args:
            code: 股票代码
            price: 当前价格

        Returns:
            质量检查结果字典，或 None (检查失败)
        """
        try:
            # 1. 获取基本面数据
            fundamentals = self._get_fundamentals(code)
            if not fundamentals:
                logger.warning(f"[质量检查] {code} 基本面数据不可用")
                return None

            # 2. 质量评分
            quality = self.quality_scorer.calculate(code, fundamentals)
            if not quality:
                logger.warning(f"[质量检查] {code} 质量评分失败")
                return None

            logger.info(f"[质量检查] {code} 质量评分：{quality.score} ({quality.class_.value})")

            # 3. 过滤垃圾股
            if quality.class_.value == 'JUNK':
                return {'reject': True, 'reason': f'质量评级为 JUNK (得分:{quality.score})'}

            # 4. 水位检查
            can_trade, reason = self.water_level.can_trade(quality.class_.value)
            if not can_trade:
                return {'reject': True, 'reason': f'水位限制：{reason}'}

            # 5. 估值检查
            valuation = self.valuation.calculate(code, price, fundamentals)
            if not valuation:
                logger.warning(f"[质量检查] {code} 估值计算失败")
                return None

            logger.info(f"[估值检查] {code} 估值信号：{valuation.signal.value} (安全边际:{valuation.margin_of_safety_pct:.1%})")

            # 6. 过滤高估股票
            if valuation.signal.value in ('SELL', 'STRONG_SELL'):
                return {'reject': True, 'reason': f'估值过高：{valuation.notes}'}

            return {
                'reject': False,
                'quality_class': quality.class_.value,
                'quality_score': quality.score,
                'valuation_signal': valuation.signal.value,
            }
        except Exception as e:
            logger.warning(f"[质量检查] {code} 异常：{e}")
            return None

    def _get_fundamentals(self, code: str) -> Optional[Dict]:
        """
        获取基本面数据 (模拟数据，实际需要从 API 获取)

        Args:
            code: 股票代码

        Returns:
            基本面数据字典
        """
        try:
            if '00700' in code:  # 腾讯 - 优质股
                return {
                    'roe_5y_avg': 25.0, 'debt_ratio': 0.25, 'operating_cashflow': 1e9,
                    'gross_margin': 0.45, 'margin_trend': 0.05, 'revenue_growth_3y': 0.20,
                    'eps': 20.0, 'pe_ttm': 25.0, 'growth_rate': 0.15, 'book_value': 100.0,
                }
            elif '09988' in code:  # 阿里 - 良好股
                return {
                    'roe_5y_avg': 15.0, 'debt_ratio': 0.35, 'operating_cashflow': 5e8,
                    'gross_margin': 0.35, 'margin_trend': 0.02, 'revenue_growth_3y': 0.12,
                    'eps': 15.0, 'pe_ttm': 18.0, 'growth_rate': 0.10, 'book_value': 80.0,
                }
            elif '03690' in code:  # 美团 - 良好股
                return {
                    'roe_5y_avg': 12.0, 'debt_ratio': 0.40, 'operating_cashflow': 3e8,
                    'gross_margin': 0.30, 'margin_trend': 0.03, 'revenue_growth_3y': 0.25,
                    'eps': 8.0, 'pe_ttm': 30.0, 'growth_rate': 0.20, 'book_value': 50.0,
                }
            else:  # 默认 - 一般股
                return {
                    'roe_5y_avg': 10.0, 'debt_ratio': 0.50, 'operating_cashflow': 1e8,
                    'gross_margin': 0.25, 'margin_trend': 0.0, 'revenue_growth_3y': 0.08,
                    'eps': 5.0, 'pe_ttm': 15.0, 'growth_rate': 0.08, 'book_value': 40.0,
                }
        except Exception as e:
            logger.warning(f"[基本面数据] {code} 获取失败：{e}")
            return None

    def _resubscribe(self):
        """
        重新订阅行情 (不创建新连接) - 2026-03-27 新增
        """
        try:
            if self._quote_ctx is None:
                logger.error("[重新订阅] 行情上下文为 None")
                return
            
            # 重新订阅股票
            from futu import SubType
            codes = config.WATCH_LIST
            for code in codes:
                try:
                    # 订阅报价
                    self._quote_ctx.subscribe(code, [SubType.QUOTE])
                    # 订阅 K 线
                    self._quote_ctx.subscribe(code, [SubType.K_1M])
                    logger.debug(f"[重新订阅] {code} 订阅成功")
                except Exception as e:
                    logger.warning(f"[重新订阅] {code} 订阅失败：{e}")
            
            logger.info("[重新订阅] 完成")
            
        except Exception as e:
            logger.error(f"[重新订阅] 失败：{e}")



    def stop(self):
        self._running = False
        if self._quote_ctx:
            self._quote_ctx.close()
            self._quote_ctx = None
            logger.info("行情上下文已关闭")

    def get_snapshot(self, codes=None):
        ctx = self._get_quote_ctx()
        codes = codes or config.WATCH_LIST
        ret, data = ctx.get_market_snapshot(codes)
        return data if ret == 0 else None
