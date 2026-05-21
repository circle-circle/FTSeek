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
        self.soft_stop_warnings: Dict[str, float] = {}  # 软止损预警缓存
        
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
        """更新多周期趋势"""
        # TODO: 从 data_subscriber 迁移 _update_multi_timeframe_trend 逻辑
        # 当前先保持为占位符
        pass
    
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
        """动态退出检查"""
        # TODO: 从 data_subscriber 迁移 dynamic_exit_manager + HKExitAggregator 逻辑
        # 当前先保持为占位符
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
