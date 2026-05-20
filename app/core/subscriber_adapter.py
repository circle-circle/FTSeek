#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DataSubscriberAdapter — 将 DataSubscriber 私有属性适配为 Provider 接口

架构位置:
  DataSubscriber (God Class) → [DataSubscriberAdapter] → risk_pipeline (Provider 接口)

设计目的:
  Phase 1 Shadow 过渡层：在不修改 DataSubscriber 内部实现的前提下，
  通过适配器暴露 7 个 Provider 接口，供 risk_pipeline 依赖倒置使用。

最终归宿:
  Phase 2-6 后由具体引擎 (ExitStateEngine, CooldownManager 等) 直接实现 Provider 接口，
  本适配器退役。

约束:
  - 所有方法只做 dict 查询/委托调用，不引入锁竞争或网络调用
  - 热路径开销 < 0.1ms
  - Phase 9: 集成性能监控 (所有 14 个方法)
"""

import logging
from typing import Optional, Dict, Any, List, Tuple

from app.core.performance_monitor import get_performance_monitor

logger = logging.getLogger("ftseek.subscriber_adapter")


class DataSubscriberAdapter:
    """
    DataSubscriber → Provider 适配器

    将 God Class 的私有属性/方法包装为 7 个 Provider 接口:
      ITradingHoursProvider / IExitStateProvider / ICooldownProvider
      ICircuitBreakerProvider / IQualityProvider / IMarketDataProvider
      IPositionQueryProvider
    """

    def __init__(self, subscriber, performance_monitor=None):
        """
        Args:
            subscriber: DataSubscriber 实例 (God Class)
            performance_monitor: 性能监控器 (可选，用于延迟统计)
        """
        self._sub = subscriber
        self._monitor = performance_monitor or get_performance_monitor()

    # ================================================================
    #  ITradingHoursProvider
    # ================================================================

    def is_trading_hours(self) -> bool:
        """是否在交易时段"""
        with self._monitor.track("is_trading_hours"):
            try:
                return self._sub._is_trading_hours()
            except Exception as e:
                logger.warning(f"[Adapter] is_trading_hours 异常：{e}")
                return False

    def is_high_quality_trading_hours(self) -> bool:
        """是否在高质量交易时段 (非噪音时段)"""
        with self._monitor.track("is_high_quality_trading_hours"):
            try:
                return self._sub._is_high_quality_trading_hours()
            except Exception as e:
                logger.warning(f"[Adapter] is_high_quality_trading_hours 异常：{e}")
                return True  # 异常时视为高质量时段 (放行，避免误杀)

    def get_trading_period(self) -> Dict[str, str]:
        """获取当前交易时段名称 (目前返回空字典，由后续引擎扩展)"""
        return {}

    # ================================================================
    #  IExitStateProvider
    # ================================================================

    def get_trailing_exit_time(self, code: str) -> float:
        """获取标的追踪止盈触发时间戳"""
        with self._monitor.track("get_trailing_exit_time"):
            try:
                return getattr(self._sub, '_trailing_exit_time', {}).get(code, 0)
            except Exception as e:
                logger.warning(f"[Adapter] get_trailing_exit_time 异常：{e}")
                return 0

    def get_trailing_stop_price(self, code: str) -> float:
        """获取标的追踪止损价格"""
        with self._monitor.track("get_trailing_stop_price"):
            try:
                return getattr(self._sub, '_trailing_stop_cache', {}).get(code, 0)
            except Exception as e:
                logger.warning(f"[Adapter] get_trailing_stop_price 异常：{e}")
                return 0

    def get_peak_price(self, code: str) -> float:
        """获取标的峰值价格缓存"""
        with self._monitor.track("get_peak_price"):
            try:
                return getattr(self._sub, '_peak_price_cache', {}).get(code, 0)
            except Exception as e:
                logger.warning(f"[Adapter] get_peak_price 异常：{e}")
                return 0

    # ================================================================
    #  ICooldownProvider
    # ================================================================

    def is_signal_cooled(self, code: str) -> bool:
        """信号是否已冷却 (预留，目前恒返回 True)"""
        # Phase 3 CooldownManager 提取后实现完整逻辑
        return True

    def get_last_open_ts(self, code: str) -> float:
        """获取标的最近一次开仓时间戳"""
        with self._monitor.track("get_last_open_ts"):
            try:
                return getattr(self._sub, '_open_position_ts', {}).get(code, 0)
            except Exception as e:
                logger.warning(f"[Adapter] get_last_open_ts 异常：{e}")
                return 0

    def get_symbol_loss_cooldown_until(self, code: str) -> float:
        """获取标的连续亏损冷却截止时间戳

        注意：_symbol_loss_cooldowns 在 data_subscriber 中未被初始化，
        SymbolLossCooldownCheck 通过 getattr 获取空 dict.
        此处保持一致的防御行为.
        """
        with self._monitor.track("get_symbol_loss_cooldown_until"):
            try:
                cooldowns = getattr(self._sub, '_symbol_loss_cooldowns', {})
                return cooldowns.get(code, 0)
            except Exception as e:
                logger.warning(f"[Adapter] get_symbol_loss_cooldown_until 异常：{e}")
                return 0

    # ================================================================
    #  ICircuitBreakerProvider
    # ================================================================

    def is_paused(self) -> bool:
        """熔断器是否处于暂停状态"""
        with self._monitor.track("is_paused"):
            try:
                cb = getattr(self._sub, 'circuit_breaker', None)
                if cb is None:
                    return True  # 熔断器不存在，保守拒绝
                return cb.is_paused()
            except Exception as e:
                logger.error(f"[Adapter] is_paused 异常：{e}")
                return True  # 异常时保守拒绝

    def check(self, current_equity: Optional[float], signal_confidence: float) -> Tuple[bool, str, float]:
        """智能熔断检查 → (allowed, reason, position_multiplier)"""
        with self._monitor.track("check"):
            try:
                cb = getattr(self._sub, 'circuit_breaker', None)
                if cb is None:
                    return (False, "熔断器未初始化", 0.0)
                return cb.check(current_equity=current_equity, signal_confidence=signal_confidence)
            except Exception as e:
                logger.error(f"[Adapter] circuit_breaker.check 异常：{e}")
                return (False, f"熔断检查异常：{e}", 0.0)

    def get_current_equity(self) -> Optional[float]:
        """获取当前账户权益"""
        with self._monitor.track("get_current_equity"):
            try:
                if hasattr(self._sub, 'get_current_equity'):
                    return self._sub.get_current_equity()
                return None
            except Exception as e:
                logger.warning(f"[Adapter] get_current_equity 异常：{e}")
                return None

    # ================================================================
    #  IQualityProvider
    # ================================================================

    def check_quality(self, code: str, price: float) -> Optional[Dict]:
        """检查标的质量和估值"""
        with self._monitor.track("check_quality"):
            try:
                if hasattr(self._sub, '_check_quality_and_valuation'):
                    return self._sub._check_quality_and_valuation(code, price)
                return None
            except Exception as e:
                logger.warning(f"[Adapter] check_quality 异常：{e}")
                return None

    def get_quality_scorer(self) -> Optional[Any]:
        """获取质量评分器实例"""
        return getattr(self._sub, 'quality_scorer', None)

    # ================================================================
    #  IMarketDataProvider
    # ================================================================

    def get_mtf_alignment(self, code: str, action: str) -> Tuple:
        """获取多周期趋势对齐结果 → (confirmed, boost, reason)"""
        with self._monitor.track("get_mtf_alignment"):
            try:
                return self._sub._get_multi_timeframe_signal(code, action)
            except Exception as e:
                logger.warning(f"[Adapter] get_mtf_alignment 异常：{e}")
                return (True, 0.0, f"MTF 查询异常：{e}")

    def get_mtf_scores(self, code: str) -> Tuple[float, float]:
        """获取 MTF BUY/SELL 对齐分数"""
        with self._monitor.track("get_mtf_scores"):
            try:
                buy_score, _ = self._sub._get_mtf_alignment_score(code, "BUY")
                sell_score, _ = self._sub._get_mtf_alignment_score(code, "SELL")
                return (buy_score, sell_score)
            except Exception as e:
                logger.warning(f"[Adapter] get_mtf_scores 异常：{e}")
                return (0.0, 0.0)

    def update_mtf_alignment(self, code: str, buy_score: float, sell_score: float) -> None:
        """同步 MTF 分数至共识引擎缓存"""
        with self._monitor.track("update_mtf_alignment"):
            try:
                consensus = getattr(self._sub, '_consensus', None)
                if consensus is not None:
                    consensus.update_mtf_alignment(code, buy_score, sell_score)
            except Exception as e:
                logger.debug(f"[Adapter] update_mtf_alignment 异常：{e}")

    def get_adx(self, code: str) -> float:
        """获取标的 ADX 值"""
        with self._monitor.track("get_adx"):
            try:
                consensus = getattr(self._sub, '_consensus', None)
                if consensus is not None and hasattr(consensus, '_adx_cache'):
                    return consensus._adx_cache.get(code, 0)
                return 0
            except Exception as e:
                logger.warning(f"[Adapter] get_adx 异常：{e}")
                return 0

    # ================================================================
    #  IPositionQueryProvider
    # ================================================================

    def get_position(self, code: str) -> Optional[Any]:
        """获取指定标的持仓"""
        with self._monitor.track("get_position"):
            try:
                pm = getattr(self._sub, 'position_manager', None)
                if pm is None:
                    return None
                return pm.get_position(code)
            except Exception as e:
                logger.warning(f"[Adapter] get_position 异常：{e}")
                return None

    def get_all_positions(self) -> List[Any]:
        """获取所有持仓列表"""
        with self._monitor.track("get_all_positions"):
            try:
                pm = getattr(self._sub, 'position_manager', None)
                if pm is None:
                    return []
                return pm.get_all_positions()
            except Exception as e:
                logger.warning(f"[Adapter] get_all_positions 异常：{e}")
                return []

    def get_open_position_ts(self, code: str) -> float:
        """获取开仓时间戳 (get_last_open_ts 的语义别名)"""
        return self.get_last_open_ts(code)
