#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开盘/收盘效应策略 (性能优化版)

逻辑:
1. 开盘突破 (09:30-10:00): 开盘后 30 分钟内，价格突破开盘价 2% 且放量，做多
2. 收盘延续 (15:30-16:00): 收盘前 30 分钟，趋势日延续，做多

港股交易时间:
- 早市：09:30-12:00
- 午市：13:00-16:00

性能优化 (2026-04-05):
- 使用 collections.deque 限制缓存大小，O(1) 追加/弹出
- 使用 numpy 数组存储数值数据，支持向量化计算
"""
import logging
from datetime import datetime
from collections import deque
from typing import Optional, Dict

import numpy as np

from .base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger("ftseek.strategy.open_close_effect")
logger.propagate = False


class OpenCloseEffectStrategy(BaseStrategy):
    """开盘/收盘效应策略 (性能优化版)"""
    name = "open_close_effect"

    def __init__(self, config: dict = None):
        super().__init__(config or {})

        # 开盘突破参数
        self.open_breakout_threshold = self.config.get("open_breakout_threshold", 0.02)  # 2% 突破
        self.open_volume_ratio = self.config.get("open_volume_ratio", 1.5)  # 成交量 1.5 倍
        self.open_window_minutes = self.config.get("open_window_minutes", 30)  # 开盘 30 分钟内

        # 收盘延续参数
        self.close_trend_threshold = self.config.get("close_trend_threshold", 0.015)  # 1.5% 趋势
        self.close_window_minutes = self.config.get("close_window_minutes", 30)  # 收盘前 30 分钟

        # K 线缓存优化 (2026-04-05): 使用 deque + numpy
        # deque 限制大小，自动弹出旧数据
        self._kline_cache: Dict[str, deque] = {}
        # numpy 数组存储数值，支持向量化计算
        self._close_array: Dict[str, np.ndarray] = {}
        self._volume_array: Dict[str, np.ndarray] = {}

        logger.info(f"开盘/收盘效应策略已初始化 (性能优化版)")
        logger.info(f"  开盘突破阈值：{self.open_breakout_threshold:.1%}")
        logger.info(f"  收盘趋势阈值：{self.close_trend_threshold:.1%}")

    def update_params(self, params: dict):
        """动态更新参数"""
        for key, val in params.items():
            if hasattr(self, key):
                setattr(self, key, val)
        logger.info(f"开盘/收盘效应策略参数已更新")

    def get_params(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "open_breakout_threshold": self.open_breakout_threshold,
            "open_volume_ratio": self.open_volume_ratio,
            "close_trend_threshold": self.close_trend_threshold,
        }

    def _is_opening_window(self, dt: datetime) -> bool:
        """是否在开盘窗口内 (09:30-10:00)"""
        if dt.weekday() >= 5:  # 周末
            return False
        if dt.hour == 9 and dt.minute >= 30:
            return True
        if dt.hour == 10 and dt.minute == 0:
            return True
        return False

    def _is_closing_window(self, dt: datetime) -> bool:
        """是否在收盘窗口内 (15:30-16:00)"""
        if dt.weekday() >= 5:  # 周末
            return False
        if dt.hour == 15 and dt.minute >= 30:
            return True
        if dt.hour == 16 and dt.minute == 0:
            return True
        return False

    def _is_trend_day(self, code: str, klines: deque) -> bool:
        """
        判断是否为趋势日 (向量化优化)

        条件:
        1. 当前涨幅 > 1.5%
        2. 价格在日内均线之上
        3. 成交量放大
        """
        if len(klines) < 20:
            return False

        # 使用 numpy 向量化计算
        closes = self._close_array.get(code, np.zeros(50))
        volumes = self._volume_array.get(code, np.zeros(50))
        n = min(len(klines), 50)

        current_price = closes[n-1]
        open_price = klines[0]['open']

        # 条件 1: 当前涨幅 > 1.5%
        day_return = (current_price - open_price) / open_price
        if day_return < self.close_trend_threshold:
            return False

        # 条件 2: 价格在日内均线之上 (numpy 均值)
        intraday_ma = np.mean(closes[max(0, n-10):n])
        if current_price < intraday_ma:
            return False

        # 条件 3: 成交量放大 (numpy 均值)
        avg_volume = np.mean(volumes[max(0, n-20):n-1])
        if avg_volume <= 0 or volumes[n-1] < avg_volume * 1.2:
            return False

        return True

    def on_kline(self, code: str, kline: dict, kline_type: str = "K_1M") -> Optional[Signal]:
        """
        K 线数据回调 (性能优化版)

        :param code: 标的代码
        :param kline: K 线数据
        :param kline_type: K 线类型 (默认 1 分钟)
        :return: 信号或 None
        """
        if not self.enabled:
            return None

        # 只处理 1 分钟 K 线
        if kline_type not in ("K_1M", "K_3M", "K_5M"):
            return None

        # 更新 K 线缓存 (优化：deque 自动限制大小)
        if code not in self._kline_cache:
            self._kline_cache[code] = deque(maxlen=50)  # 最多保留 50 根 K 线
            self._close_array[code] = np.zeros(50)
            self._volume_array[code] = np.zeros(50)

        self._kline_cache[code].append(kline)

        # 更新 numpy 数组 (滚动窗口)
        close_list = [k['close'] for k in self._kline_cache[code]]
        volume_list = [k['volume'] for k in self._kline_cache[code]]
        n = len(close_list)
        self._close_array[code][:n] = close_list
        self._volume_array[code][:n] = volume_list

        # 解析时间
        kline_time = datetime.strptime(kline['time'], '%Y-%m-%d %H:%M:%S')

        # 1. 检查开盘突破
        if self._is_opening_window(kline_time):
            signal = self._check_open_breakout(code, kline)
            if signal:
                return signal

        # 2. 检查收盘延续
        if self._is_closing_window(kline_time):
            signal = self._check_close_continuation(code, kline)
            if signal:
                return signal

        return None

    def _check_open_breakout(self, code: str, kline: dict) -> Optional[Signal]:
        """检查开盘突破信号 (向量化优化)"""
        klines = self._kline_cache.get(code, deque(maxlen=50))

        if len(klines) < 5:
            return None

        # 获取开盘价 (第一根 K 线)
        open_price = klines[0]['open']
        current_price = kline['close']

        # 使用 numpy 数组计算成交量均值 (向量化)
        volumes = self._volume_array.get(code, np.zeros(50))
        n = min(len(klines), 50)
        avg_volume = np.mean(volumes[max(0, n-5):n-1]) if n > 4 else kline['volume']

        # 向上突破
        if current_price > open_price * (1 + self.open_breakout_threshold):
            if avg_volume > 0 and kline['volume'] > avg_volume * self.open_volume_ratio:
                volume_ratio = kline['volume'] / avg_volume if avg_volume > 0 else 1.0
                price_change = (current_price - open_price) / open_price
                logger.info(f"[开盘突破] {code} 价格上涨 {price_change:.1%}, "
                           f"成交量 {kline['volume']:.0f} (> {self.open_volume_ratio:.1f}x 均值)")

                return Signal(
                    code=code,
                    action=SignalAction.BUY,
                    price=current_price,
                    reason=f"开盘突破：上涨 {price_change:.1%}, "
                           f"成交量 {kline['volume']:.0f} ({volume_ratio:.1f}x 均值)",
                    strategy=self.name,
                    extra={
                        'open_price': open_price,
                        'breakout_type': 'upside',
                        'volume_ratio': volume_ratio,
                    }
                )

        # 向下跌破 (可考虑做空，如开启)
        if current_price < open_price * (1 - self.open_breakout_threshold):
            if avg_volume > 0 and kline['volume'] > avg_volume * self.open_volume_ratio:
                price_change = (current_price - open_price) / open_price
                logger.info(f"[开盘突破] {code} 价格下跌 {price_change:.1%}, "
                           f"成交量 {kline['volume']:.0f}")

                return Signal(
                    code=code,
                    action=SignalAction.SELL,
                    price=current_price,
                    reason=f"开盘跌破：下跌 {price_change:.1%}",
                    strategy=self.name,
                    extra={
                        'open_price': open_price,
                        'breakout_type': 'downside',
                    }
                )

        return None

    def _check_close_continuation(self, code: str, kline: dict) -> Optional[Signal]:
        """检查收盘延续信号"""
        klines = self._kline_cache.get(code, [])

        if not self._is_trend_day(code, klines):
            return None

        current_price = kline['close']

        # 趋势日，收盘前做多
        logger.info(f"[收盘延续] {code} 趋势日延续，当前涨幅 {((current_price - klines[0]['open']) / klines[0]['open']):.1%}")

        return Signal(
            code=code,
            action=SignalAction.BUY,
            price=current_price,
            reason=f"收盘延续：趋势日 (当前涨幅 {((current_price - klines[0]['open']) / klines[0]['open']):.1%})",
            strategy=self.name,
            extra={
                'signal_type': 'close_continuation',
                'day_return': (current_price - klines[0]['open']) / klines[0]['open'],
            }
        )

    def clear_cache(self, code: str = None):
        """清空 K 线缓存"""
        if code:
            self._kline_cache.pop(code, None)
            self._close_array.pop(code, None)
            self._volume_array.pop(code, None)
        else:
            self._kline_cache.clear()
            self._close_array.clear()
            self._volume_array.clear()
        logger.debug("K 线缓存已清空")
