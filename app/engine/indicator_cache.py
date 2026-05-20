#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IndicatorCache — ADX/ATR/峰值 指标计算缓存

架构位置:
  DataSubscriber → [IndicatorCache] → risk_pipeline (IMarketDataProvider)

设计目的:
  将指标计算逻辑从 God Class (data_subscriber.py) 提取为独立模块，
  封装 _update_adx, _get_atr_percent 等计算，对外提供只读查询接口 (IMarketDataProvider)。

职责:
  1. 封装 ADX 计算和缓存 (_adx_cache)
  2. 封装 ATR 百分比计算 (_get_atr_percent, _get_atr_from_period)
  3. 提供 IMarketDataProvider 接口 (get_adx, get_atr_percent)

约束:
  - 热路径：ADX 计算在 K 线回调中执行 (< 5ms)
  - 线程安全：写入操作持有锁，读取操作无锁 (dict 查询)
  - 依赖注入：依赖 MarketDataStore 获取 K 线数据

最终归宿:
  DataSubscriber 通过依赖注入持有 IndicatorCache 实例,
  Adapter 退役后直接由 IndicatorCache 实现 IMarketDataProvider 接口。

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取 ADX/ATR 计算逻辑
"""

import logging
import threading
import time
from typing import Dict, Optional, Any

from app.core.interfaces import IMarketDataProvider

logger = logging.getLogger("ftseek.engine.indicator")


class IndicatorCache:
    """
    ADX/ATR/峰值 指标计算缓存

    状态字典:
      - _adx_cache: Dict[str, float] — ADX 值缓存 (code -> adx_value)
      - _peak_price_cache: Dict[str, float] — 持仓峰值价格缓存 (code -> peak_price)

    IMarketDataProvider 接口:
      - get_adx(code)
      - get_atr_percent(code, period)
      - get_peak_price(code) (委托给 ExitStateEngine)
    """

    def __init__(
        self,
        market_data_store: Any,
        consensus_engine: Any = None,
        adx_period: int = 14,
    ):
        """
        初始化指标缓存

        Args:
            market_data_store: MarketDataStore 实例 (依赖注入)
            consensus_engine: ConsensusEngine 实例 (可选，用于注入 ADX 值)
            adx_period: ADX 计算周期 (默认 14)
        """
        self._store = market_data_store
        self._consensus = consensus_engine
        self._adx_period = adx_period

        # =====================================================================
        #  状态字典
        # =====================================================================

        # ADX 值缓存：code -> adx_value
        # 用于 ADXConsistencyCheck 和多周期对齐
        self._adx_cache: Dict[str, float] = {}

        # 持仓峰值价格缓存：code -> peak_price
        # 用于移动止损计算 (委托给 ExitStateEngine 管理)
        self._peak_price_cache: Dict[str, float] = {}

        # =====================================================================
        #  锁 (保护写入操作)
        # =====================================================================
        self._cache_lock = threading.Lock()

        logger.info(
            f"[IndicatorCache] 初始化完成： "
            f"adx_period={adx_period}"
        )

    # =========================================================================
    #  IMarketDataProvider 接口实现 (只读)
    # =========================================================================

    def get_adx(self, code: str) -> float:
        """
        获取标的 ADX 值

        Args:
            code: 股票代码

        Returns:
            ADX 值 (0=无数据)
        """
        return self._adx_cache.get(code, 0.0)

    def get_atr_percent(self, code: str, period: str = "K_15M") -> float:
        """
        获取 ATR 波动率百分比

        Args:
            code: 股票代码
            period: K 线周期 "K_1M" / "K_15M" / "K_60M" 等

        Returns:
            ATR 百分比 (0.003 ~ 0.15)
        """
        if period == "K_1M":
            return self._get_atr_percent_1m(code)
        else:
            return self._get_atr_from_period(code, period)

    def get_peak_price(self, code: str) -> float:
        """
        获取持仓峰值价格

        Args:
            code: 股票代码

        Returns:
            峰值价格 (0=无缓存)
        """
        return self._peak_price_cache.get(code, 0.0)

    # =========================================================================
    #  写入方法 (从 KlineWorker 调用)
    # =========================================================================

    def update_adx(self, code: str, kline: dict) -> None:
        """
        每根 K 线到达时更新 ADX 并注入共识引擎

        迁移自 data_subscriber._update_adx()

        Args:
            code: 股票代码
            kline: K 线数据字典
        """
        try:
            buf = self._store.get_kline_buffer(code)
            buf.push_kline(kline)

            # 数据不足时跳过
            if buf.size < self._adx_period * 2 + 5:
                return

            # 计算 ADX
            from app.strategy.indicators import adx as calc_adx
            adx_val = calc_adx(
                list(buf.high), list(buf.low), list(buf.close), self._adx_period
            )

            if adx_val is not None:
                with self._cache_lock:
                    self._adx_cache[code] = adx_val

                # 注入共识引擎
                if self._consensus is not None:
                    try:
                        self._consensus.update_adx(code, adx_val)
                    except Exception as e:
                        logger.debug(f"[IndicatorCache] ADX 注入共识引擎失败：{e}")

                logger.debug(
                    f"[IndicatorCache] {code} ADX 更新：{adx_val:.1f} (buf.size={buf.size})"
                )

        except Exception as e:
            logger.debug(f"[IndicatorCache] {code} ADX 更新异常：{e}")

    def update_regime(self, code: str, kline: dict) -> None:
        """
        更新市场状态检测 (MarketRegimeDetector)

        迁移自 data_subscriber._update_adx() 中的 regime 检测部分

        Args:
            code: 股票代码
            kline: K 线数据字典
        """
        try:
            from app.risk.regime_detector import get_regime_detector
            regime_detector = get_regime_detector()
            regime_detector.update_data(code, kline)
            regime = regime_detector.detect(code)

            if regime is not None and self._consensus is not None:
                # 高波动市场：提高共识阈值，降低假信号
                if regime.volatility_state.value == 'high':
                    self._consensus.threshold = 2.0
                else:
                    # 恢复默认阈值
                    from app.data_subscriber import CONSENSUS_THRESHOLD
                    self._consensus.threshold = CONSENSUS_THRESHOLD

                # 日线趋势注入（用于多周期对齐降权）
                trend_map = {
                    'strong_up': 'up', 'weak_up': 'up',
                    'strong_down': 'down', 'weak_down': 'down',
                }
                daily_trend = trend_map.get(regime.trend_direction.value, 'neutral')
                self._consensus.update_daily_trend(code, daily_trend)

                logger.debug(
                    f"[IndicatorCache] {code} 市场状态：{regime.regime_type.value} | "
                    f"趋势={daily_trend} | 阈值={self._consensus.threshold:.1f}"
                )

        except Exception as e:
            logger.debug(f"[IndicatorCache] {code} 市场状态检测异常：{e}")

    # =========================================================================
    #  内部计算方法 (从 data_subscriber 迁移)
    # =========================================================================

    def _get_atr_percent_1m(self, code: str) -> float:
        """
        计算 1M K 线的 ATR 波动率百分比

        迁移自 data_subscriber._get_atr_percent()

        使用 Wilder 风格的 True Range 均值计算，避免用整体价格区间导致 ATR 估算过小。
        True Range = max(High, PrevClose) - min(Low, PrevClose)
        ATR = 14 根 K 线的 TR 均值 / 当前价

        Returns:
            ATR 百分比 (0.003 ~ 0.08)
        """
        buf = self._store.get_kline_buffer(code)
        if buf.size < 15:
            return 0.02  # 默认 2%

        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)

        # 取最近 14 根 K 线计算 True Range
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
        """
        从指定周期 K 线缓冲区计算 ATR 百分比

        迁移自 data_subscriber._get_atr_from_period()

        通过多周期 K 线（15M/30M/60M/日线）获取更稳定的 ATR，用于止损宽度自适应。

        Args:
            code: 股票代码
            k_type: K 线周期 "K_15M" / "K_30M" / "K_60M" / "K_DAY"
            period: ATR 计算周期，默认 14

        Returns:
            ATR 百分比 (0.003 ~ 0.15)
        """
        try:
            buf = self._store.get_kline_buffer_by_period(code, k_type)
            if buf is None or buf.size < period + 1:
                return self._get_atr_percent_1m(code)  # fallback 到 1M

            closes = list(buf.close)
            highs = list(buf.high)
            lows = list(buf.low)

            tr_list = []
            n = min(period, len(highs))
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

            # 设置合理上下界（0.3% ~ 15%），避免极端值
            return max(0.003, min(atr_pct, 0.15))

        except Exception as e:
            logger.debug(f"[IndicatorCache] {code} {k_type} ATR 计算异常：{e}")
            return self._get_atr_percent_1m(code)

    def get_preferred_atr_percent(self, code: str) -> float:
        """
        获取优先使用的 ATR 百分比 (默认 60M K 线)

        迁移自 data_subscriber._get_preferred_atr_percent()

        优先使用 60M K 线的 ATR，追踪更稳定。

        Returns:
            ATR 百分比
        """
        # 优先使用 60M K 线
        if code in self._store._kline_buffers_by_period:
            buf_60m = self._store._kline_buffers_by_period[code].get("K_60M")
            if buf_60m and buf_60m.size >= 15:
                return self._get_atr_from_period(code, "K_60M")

        # fallback 到 1M
        return self._get_atr_percent_1m(code)

    # =========================================================================
    #  清理方法
    # =========================================================================

    def cleanup_expired_cache(self, max_age_seconds: float = 86400) -> None:
        """
        清理过期缓存 (每日调用一次，默认 24h 过期)

        Args:
            max_age_seconds: 过期时长 (秒)
        """
        # ADX 缓存不需要清理 (每次 K 线到达时更新)
        # 峰值缓存由 ExitStateEngine 管理

        logger.debug(f"[IndicatorCache] 清理过期缓存完成")

    # =========================================================================
    #  调试方法
    # =========================================================================

    def get_stats(self) -> Dict:
        """获取指标缓存统计信息"""
        return {
            "adx_cache_count": len(self._adx_cache),
            "peak_price_cache_count": len(self._peak_price_cache),
        }
