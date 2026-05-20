#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketDataStore — 行情数据 CQRS 存储

架构位置:
  DataSubscriber → [MarketDataStore] → risk_pipeline (IMarketDataProvider)

设计目的:
  将行情数据存储从 God Class (data_subscriber.py 3615 行) 提取为独立模块，
  实现 CQRS 读写分离：写入由 Worker 线程调用 (持有锁), 读取由 RiskCheck/API 调用 (无锁或读锁)。

职责:
  1. 管理 K 线缓冲区 (_kline_buffers, _kline_buffers_by_period)
  2. 管理最新报价 (_latest_quotes)
  3. 管理多周期趋势缓存 (_multi_timeframe_trends, _trends_updated, _mtf_warmup_until)

约束:
  - 热路径：写入方法由 Worker 线程调用 (持有锁), 读取方法由 RiskCheck/API 调用 (无锁或读锁)
  - 线程安全：写入操作持有 _write_lock, 读取操作允许轻微不一致 (dict 查询 < 0.1ms)
  - 依赖注入：无外部依赖 (纯状态管理)

最终归宿:
  DataSubscriber 通过依赖注入持有 MarketDataStore 实例,
  Adapter 退役后直接由 MarketDataStore 实现 IMarketDataProvider 接口。

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取行情数据存储状态
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Dict, Optional, Any, Set

from app.core.interfaces import IMarketDataProvider

logger = logging.getLogger("ftseek.engine.market_data")


class MarketDataStore:
    """
    行情数据 CQRS 存储

    状态字典 (从 data_subscriber 迁移):
      - _kline_buffers: Dict[str, IndicatorBuffer] — K 线滑动窗口 (maxlen=200)
      - _kline_buffers_by_period: Dict[str, Dict[str, IndicatorBuffer]] — 多周期 K 线缓存
      - _latest_quotes: Dict[str, dict] — 最新报价 (per-code 去重后)
      - _multi_timeframe_trends: Dict[str, Dict[str, str]] — 多周期趋势缓存
      - _trends_updated: Set[Tuple[str, str]] — 趋势更新追踪
      - _mtf_warmup_until: float — MTF 冷启动宽限期截止时间

    CQRS 分离:
      Command (写入): push_kline, update_quote, update_mtf_trend
      Query (只读): get_kline_buffer, get_latest_quote, get_mtf_trends
    """

    def __init__(
        self,
        indicator_buffer_factory=None,
        kline_maxlen: int = 200,
        mtf_warmup_seconds: int = 300,
    ):
        """
        初始化行情数据存储

        Args:
            indicator_buffer_factory: IndicatorBuffer 工厂函数 (可选，懒加载)
            kline_maxlen: K 线缓冲区最大长度 (默认 200)
            mtf_warmup_seconds: MTF 冷启动宽限期 (秒，默认 300=5 分钟)
        """
        self._buffer_factory = indicator_buffer_factory
        self._kline_maxlen = kline_maxlen

        # =====================================================================
        #  状态字典 (从 data_subscriber 迁移)
        # =====================================================================

        # K 线缓冲：为每个 code 维护滑动窗口，用于计算 ADX
        # 结构：{code: IndicatorBuffer(maxlen=200)}
        self._kline_buffers: Dict[str, Any] = defaultdict(
            lambda: self._create_indicator_buffer()
        )

        # 多周期 K 线缓存 (每个周期独立)
        # 结构：{code: {"K_1M": IndicatorBuffer, "K_5M": IndicatorBuffer, ...}}
        self._kline_buffers_by_period: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "K_1M": self._create_indicator_buffer(maxlen=100),
                "K_3M": self._create_indicator_buffer(maxlen=100),
                "K_5M": self._create_indicator_buffer(maxlen=100),
                "K_15M": self._create_indicator_buffer(maxlen=100),
                "K_30M": self._create_indicator_buffer(maxlen=100),
                "K_60M": self._create_indicator_buffer(maxlen=100),
                "K_DAY": self._create_indicator_buffer(maxlen=100),
            }
        )

        # 最新报价：per-code 去重后的最新行情
        # 结构：{code: {"last_price": float, "volume": int, ...}}
        self._latest_quotes: Dict[str, dict] = {}

        # 多周期趋势缓存
        # 结构：{code: {"K_1M": "up"/"down"/"neutral", "K_5M": trend, ...}}
        self._multi_timeframe_trends: Dict[str, Dict[str, str]] = defaultdict(
            lambda: {
                "K_1M": "neutral",
                "K_3M": "neutral",
                "K_5M": "neutral",
                "K_15M": "neutral",
                "K_30M": "neutral",
                "K_60M": "neutral",
                "K_DAY": "neutral",
            }
        )

        # 趋势更新追踪：区分 "neutral=默认值" vs "neutral=计算后中性"
        # 结构：{(code, period)} — 至少成功更新过一次趋势的 period
        self._trends_updated: Set[tuple] = set()

        # MTF 冷启动宽限期截止时间 (epoch seconds)
        # 在宽限期内，未更新的周期使用默认值，避免冷启动误判
        self._mtf_warmup_until: float = time.time() + mtf_warmup_seconds

        # =====================================================================
        #  锁 (保护写入操作)
        # =====================================================================
        self._write_lock = threading.Lock()

        logger.info(
            f"[MarketDataStore] 初始化完成： "
            f"kline_maxlen={kline_maxlen}, "
            f"mtf_warmup={mtf_warmup_seconds}s"
        )

    # =========================================================================
    #  内部辅助方法
    # =========================================================================

    def _create_indicator_buffer(self, maxlen: int = None):
        """创建 IndicatorBuffer 实例 (懒加载)"""
        if maxlen is None:
            maxlen = self._kline_maxlen
        try:
            from app.strategy.indicators import IndicatorBuffer
            return IndicatorBuffer(maxlen=maxlen)
        except Exception as e:
            logger.warning(f"[MarketDataStore] IndicatorBuffer 创建失败：{e}")
            # fallback: 返回空 dict 作为占位符
            return type('MockBuffer', (), {'push_kline': lambda *args: None, 'size': 0})()

    # =========================================================================
    #  Command 方法 (写入，由 Worker 线程调用，持有锁)
    # =========================================================================

    def push_kline(self, code: str, kline: dict, k_type: str = "K_1M") -> None:
        """
        推送 K 线数据到缓冲区

        Args:
            code: 股票代码
            kline: K 线数据字典 {"open": float, "high": float, "low": float, "close": float, ...}
            k_type: K 线周期 "K_1M" / "K_5M" / "K_15M" 等
        """
        with self._write_lock:
            # 推送到主缓冲区 (用于 ADX 计算)
            self._kline_buffers[code].push_kline(kline)

            # 推送到多周期缓冲区
            if k_type in self._kline_buffers_by_period[code]:
                self._kline_buffers_by_period[code][k_type].push_kline(kline)

    def update_quote(self, code: str, quote: dict) -> None:
        """
        更新最新报价 (per-code 去重)

        Args:
            code: 股票代码
            quote: 行情数据字典 {"last_price": float, "volume": int, ...}
        """
        with self._write_lock:
            self._latest_quotes[code] = quote

    def update_mtf_trend(self, code: str, period: str, trend: str) -> None:
        """
        更新多周期趋势缓存

        Args:
            code: 股票代码
            period: K 线周期 "K_1M" / "K_5M" 等
            trend: 趋势方向 "up" / "down" / "neutral"
        """
        with self._write_lock:
            if code not in self._multi_timeframe_trends:
                self._multi_timeframe_trends[code] = {
                    "K_1M": "neutral",
                    "K_3M": "neutral",
                    "K_5M": "neutral",
                    "K_15M": "neutral",
                    "K_30M": "neutral",
                    "K_60M": "neutral",
                    "K_DAY": "neutral",
                }
            old_trend = self._multi_timeframe_trends[code].get(period, "neutral")
            self._multi_timeframe_trends[code][period] = trend
            self._trends_updated.add((code, period))

            if trend != old_trend:
                logger.debug(
                    f"[MarketDataStore] {code} {period} 趋势变化：{old_trend} → {trend}"
                )

    # =========================================================================
    #  Query 方法 (只读，由 RiskCheck/API 调用，无锁或读锁，允许轻微不一致)
    # =========================================================================

    def get_kline_buffer(self, code: str) -> Any:
        """
        获取指定标的的 K 线缓冲区

        Args:
            code: 股票代码

        Returns:
            IndicatorBuffer 实例 (可能为空)
        """
        return self._kline_buffers[code]

    def get_kline_buffer_by_period(self, code: str, period: str) -> Any:
        """
        获取指定标的和周期的 K 线缓冲区

        Args:
            code: 股票代码
            period: K 线周期 "K_1M" / "K_5M" 等

        Returns:
            IndicatorBuffer 实例 (可能为空)
        """
        if code not in self._kline_buffers_by_period:
            return None
        return self._kline_buffers_by_period[code].get(period)

    def get_latest_quote(self, code: str) -> Optional[dict]:
        """
        获取最新报价

        Args:
            code: 股票代码

        Returns:
            行情数据字典或 None
        """
        return self._latest_quotes.get(code)

    def get_mtf_trends(self, code: str) -> Dict[str, str]:
        """
        获取多周期趋势缓存

        Args:
            code: 股票代码

        Returns:
            趋势字典 {"K_1M": "up"/"down"/"neutral", ...}
        """
        return self._multi_timeframe_trends.get(code, {
            "K_1M": "neutral",
            "K_3M": "neutral",
            "K_5M": "neutral",
            "K_15M": "neutral",
            "K_30M": "neutral",
            "K_60M": "neutral",
            "K_DAY": "neutral",
        })

    def get_trends_updated(self) -> Set[tuple]:
        """
        获取趋势更新追踪集合

        Returns:
            Set[Tuple[code, period]] — 已更新过趋势的 (code, period) 集合
        """
        return self._trends_updated.copy()

    def is_in_warmup(self) -> bool:
        """
        是否在 MTF 冷启动宽限期内

        Returns:
            True=在宽限期内，False=宽限期已结束
        """
        return time.time() < self._mtf_warmup_until

    def get_warmup_remaining(self) -> int:
        """
        获取冷启动宽限期剩余秒数

        Returns:
            剩余秒数 (0=已结束)
        """
        remaining = int(self._mtf_warmup_until - time.time())
        return max(0, remaining)

    # =========================================================================
    #  清理方法
    # =========================================================================

    def cleanup_stale_quotes(self, max_age_seconds: float = 60.0) -> None:
        """
        清理过期报价 (默认 60s 过期)

        Args:
            max_age_seconds: 过期时长 (秒)
        """
        now = time.time()
        stale_codes = []

        with self._write_lock:
            for code, quote in list(self._latest_quotes.items()):
                quote_time = quote.get("timestamp", 0)
                if now - quote_time > max_age_seconds:
                    stale_codes.append(code)

            for code in stale_codes:
                self._latest_quotes.pop(code, None)

        if stale_codes:
            logger.info(f"[MarketDataStore] 清理 {len(stale_codes)} 个过期报价")

    def cleanup_stale_trends(self, max_age_seconds: float = 3600.0) -> None:
        """
        清理过期趋势缓存 (默认 1h 过期)

        Args:
            max_age_seconds: 过期时长 (秒)
        """
        now = time.time()
        stale_keys = []

        with self._write_lock:
            for code, periods in list(self._multi_timeframe_trends.items()):
                # 检查是否所有周期都是 neutral (表示已失效)
                if all(v == "neutral" for v in periods.values()):
                    stale_keys.append(code)

            for code in stale_keys:
                self._multi_timeframe_trends.pop(code, None)
                # 清理 trends_updated 集合
                self._trends_updated.discard(code)

        if stale_keys:
            logger.info(f"[MarketDataStore] 清理 {len(stale_keys)} 个过期趋势缓存")

    # =========================================================================
    #  调试方法
    # =========================================================================

    def get_stats(self) -> Dict:
        """获取行情数据存储统计信息"""
        return {
            "kline_buffers_count": len(self._kline_buffers),
            "kline_buffers_by_period_count": len(self._kline_buffers_by_period),
            "latest_quotes_count": len(self._latest_quotes),
            "multi_timeframe_trends_count": len(self._multi_timeframe_trends),
            "trends_updated_count": len(self._trends_updated),
            "mtf_warmup_remaining": self.get_warmup_remaining(),
        }
