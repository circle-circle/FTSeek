#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KlineProcessor — K 线处理服务

架构位置:
  DataSubscriber._process_kline_data
    ↓ 提取为
  app/services/kline_processor.py

设计目的:
  将 K 线数据处理逻辑从 DataSubscriber 中提取为独立服务，
  包括 ADX/ATR 更新、MTF 更新、止损/止盈检查等。

版本历史:
  2026-05-19 v1: 创建 (Phase 10C)
"""

import logging
from typing import Dict, Optional, Any

logger = logging.getLogger("ftseek.services.kline_processor")


class KlineProcessor:
    """K 线处理服务 — ADX/ATR/MTF/止损止盈"""

    def __init__(
        self,
        kline_buffers,
        mtf_engine,
        position_manager,
        exit_executor,
        regime_detector,
        consensus_engine,
        config,
    ):
        self._kline_buffers = kline_buffers
        self._mtf_engine = mtf_engine
        self._pm = position_manager
        self._exit_executor = exit_executor
        self._regime_detector = regime_detector
        self._consensus = consensus_engine
        self._config = config

    def process(self, code: str, kline: dict, k_type: str) -> None:
        """
        处理单个 K 线数据

        Args:
            code: 股票代码
            kline: K 线数据
            k_type: K 线类型 (K_1M, K_5M, K_15M, etc.)
        """
        # 提取当前价格
        current_price = kline.get('close', 0) or kline.get('last_price', 0)

        # 多周期趋势更新
        self._update_multi_timeframe_trend(code, k_type, kline)

        # 更新 K 线 buffer
        self._kline_buffers[code].push_kline(kline)

        # MarketRegimeDetector 接入多周期数据
        if k_type in ("K_15M", "K_30M", "K_60M"):
            try:
                self._regime_detector.update_data(code, kline)
            except Exception as e:
                logger.debug(f"[regime 多周期] {code} {k_type} 更新失败：{e}")

        # 硬止损 (保留作为最后一道防线)
        if current_price > 0:
            pos_now = self._pm.get_position(code)
            if pos_now and self._pm.check_stop_loss(code, current_price):
                logger.info(f"[止损触发] {code} 当前价={current_price}")
                if self._exit_executor:
                    self._exit_executor.execute_stop_loss(code, current_price)

    def _update_multi_timeframe_trend(self, code: str, k_type: str, kline: dict) -> None:
        """更新多周期趋势 (委托给 MTF engine)"""
        if self._mtf_engine:
            try:
                self._mtf_engine.update(code, k_type, kline)
            except Exception as e:
                logger.debug(f"[MTF 更新] {code} {k_type} 异常：{e}")

    def get_atr_percent(self, code: str) -> float:
        """获取 ATR 百分比 (委托给共识引擎)"""
        if self._consensus and hasattr(self._consensus, '_atr_cache'):
            return self._consensus._atr_cache.get(code, 0)
        return 0
