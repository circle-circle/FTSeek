#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IndicatorService — 指标计算服务

架构位置:
  DataSubscriber._update_adx / _get_atr_percent / _update_trailing_stop
    ↓ 提取为
  app/services/indicator_service.py

设计目的:
  将指标计算逻辑从 DataSubscriber 中提取为独立服务，
  包括 ADX/ATR/追踪止损/MTF 等指标的计算和更新。

版本历史:
  2026-05-19 v1: 创建 (Phase 10E)
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger("ftseek.services.indicator_service")


class IndicatorService:
    """指标计算服务 — ADX/ATR/追踪止损/MTF"""

    def __init__(
        self,
        kline_buffers,
        indicator_cache,
        mtf_engine,
        consensus_engine,
    ):
        self._kline_buffers = kline_buffers
        self._indicator_cache = indicator_cache
        self._mtf_engine = mtf_engine
        self._consensus = consensus_engine

    def update_adx(self, code: str, kline: dict) -> None:
        """
        更新 ADX 指标

        Args:
            code: 股票代码
            kline: K 线数据
        """
        try:
            if self._consensus and hasattr(self._consensus, 'update_adx'):
                self._consensus.update_adx(code, kline)
        except Exception as e:
            logger.debug(f"[ADX 更新] {code} 异常：{e}")

    def get_atr_percent(self, code: str) -> float:
        """
        获取 ATR 百分比

        Returns:
            ATR 百分比 (0 表示不可用)
        """
        if self._consensus and hasattr(self._consensus, '_atr_cache'):
            return self._consensus._atr_cache.get(code, 0)
        return 0

    def update_trailing_stop(self, code: str, current_price: float, position) -> None:
        """
        更新追踪止损

        Args:
            code: 股票代码
            current_price: 当前价格
            position: 持仓对象
        """
        if position is None:
            return

        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            # 委托给 DEM 处理追踪止损逻辑
            _dem.update_trailing_stop(code, current_price, position)
        except Exception as e:
            logger.debug(f"[追踪止损] {code} 异常：{e}")

    def update_multi_timeframe_trend(self, code: str) -> None:
        """
        更新多周期趋势

        Args:
            code: 股票代码
        """
        if self._mtf_engine:
            try:
                self._mtf_engine.update_all(code)
            except Exception as e:
                logger.debug(f"[MTF 更新] {code} 异常：{e}")
