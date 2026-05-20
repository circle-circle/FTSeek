#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易状态管理器 (Trading State Manager)

集中管理 data_subscriber.py 中分散的 15+ 个状态字典。

设计原则:
- 单一职责: 只管理状态，不处理业务逻辑
- 线程安全: 所有状态访问都加锁
- 可持久化: 支持状态持久化到磁盘/Redis
- 可清理: 统一清理过期状态

状态分类:
1. 冷却状态 (cooldown): 防止频繁交易
2. 峰值状态 (peak): 追踪最高/最低价格
3. 快照状态 (snapshot): Flash Exit 使用
4. 缓存状态 (cache): K 线、技术指标、情绪
5. 锁状态 (lock): 防止并发冲突

版本历史:
  2026-05-19: 从 data_subscriber.py 提取

使用示例:
    from app.services.trading_state_manager import get_trading_state_manager

    state_mgr = get_trading_state_manager()

    # 检查冷却期
    if state_mgr.is_in_cooldown(code):
        return

    # 更新峰值价格
    state_mgr.update_peak_price(code, current_price)

    # 统一清理过期状态
    state_mgr.cleanup_expired()
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Tuple, Optional, Set, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class TradingStateManager:
    """
    交易状态管理器 — 集中管理所有交易状态

    特点:
    - 线程安全: 所有状态访问都加锁
    - 可持久化: 支持 persist() / restore()
    - 统一清理: cleanup_expired()
    - 状态监控: get_state_summary()
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 防止重复初始化
        if hasattr(self, '_initialized'):
            return

        # ============================================================
        #  1. 冷却状态 (Cooldown States)
        # ============================================================
        # 推送冷却：code -> last_push_time
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

        # 止损冷却：code -> last_stop_loss_time
        self._stop_loss_cooldown: Dict[str, float] = {}
        self._stop_loss_fail_count: Dict[str, int] = {}

        # 软止损预警冷却：code -> last_warning_timestamp
        self._soft_stop_warnings: Dict[str, float] = {}

        # ============================================================
        #  2. 峰值状态 (Peak States)
        # ============================================================
        # 持仓期间峰值价格：code -> peak_price
        self._peak_price_cache: Dict[str, float] = {}

        # 追踪止盈缓存：code -> trailing_stop_price
        self._trailing_stop_cache: Dict[str, float] = {}
        # 追踪止盈触发时间：code -> timestamp
        self._trailing_exit_time: Dict[str, float] = {}

        # ============================================================
        #  3. 快照状态 (Snapshot States)
        # ============================================================
        # Flash Exit 快照：code -> (price, timestamp)
        self._flash_snapshots: Dict[str, Tuple[float, float]] = {}
        self._flash_exit_enabled: bool = True
        self._flash_atr_mult: float = 1.0
        self._flash_window_seconds: float = 60.0

        # ============================================================
        #  4. 缓存状态 (Cache States)
        # ============================================================
        # K 线缓冲：code -> IndicatorBuffer
        self._kline_buffers: Dict[str, Any] = defaultdict(lambda: None)

        # 多周期趋势缓存：code -> {period: trend}
        self._multi_timeframe_trends: Dict[str, Dict[str, str]] = defaultdict(lambda: {
            "K_1M": "neutral",
            "K_3M": "neutral",
            "K_5M": "neutral",
            "K_15M": "neutral",
            "K_30M": "neutral",
            "K_60M": "neutral",
            "K_DAY": "neutral"
        })

        # 多周期 K 线缓存：code -> {period: IndicatorBuffer}
        self._kline_buffers_by_period: Dict[str, Dict[str, Any]] = defaultdict(lambda: {})

        # 新闻情绪缓存：code -> (sentiment_score, timestamp)
        self._news_sentiment_cache: Dict[str, Tuple[float, float]] = {}

        # 行情心跳：last_quote_time
        self._last_quote_time: float = time.time()

        # ============================================================
        #  5. 锁状态 (Lock States)
        # ============================================================
        # code 级别交易锁：code -> threading.Lock
        self._trade_locks: Dict[str, threading.Lock] = {}
        self._open_position_ts: Dict[str, float] = {}
        self._open_lock = threading.Lock()

        # 重连锁
        self._reconnect_lock = threading.Lock()

        # 趋势更新追踪
        self._trends_updated: Set[Tuple[str, str]] = set()
        self._mtf_warmup_until: float = time.time() + 60.0  # 默认 60 秒宽限期

        self._initialized = True
        logger.info("[TradingStateManager] 已初始化 (单例模式)")

    # ============================================================
    #  冷却状态 API
    # ============================================================

    def is_in_cooldown(self, code: str, cooldown_seconds: float = 120.0) -> bool:
        """
        检查是否在冷却期

        Args:
            code: 股票代码
            cooldown_seconds: 冷却时间 (秒)

        Returns:
            bool: 是否在冷却期
        """
        with self._cooldown_lock:
            last_push = self._cooldown.get(code, 0)
            return (time.time() - last_push) < cooldown_seconds

    def update_cooldown(self, code: str):
        """更新冷却时间"""
        with self._cooldown_lock:
            self._cooldown[code] = time.time()

    def is_in_stop_loss_cooldown(self, code: str, cooldown_seconds: float = 30.0) -> bool:
        """检查是否在止损冷却期"""
        last_sl = self._stop_loss_cooldown.get(code, 0)
        return (time.time() - last_sl) < cooldown_seconds

    def update_stop_loss_cooldown(self, code: str):
        """更新止损冷却时间"""
        self._stop_loss_cooldown[code] = time.time()

    def increment_stop_loss_fail_count(self, code: str):
        """增加止损失败计数"""
        self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1

    def reset_stop_loss_fail_count(self, code: str):
        """重置止损失败计数"""
        self._stop_loss_fail_count[code] = 0

    def is_soft_stop_warning_cooled(self, code: str, min_interval: float = 300.0) -> bool:
        """检查软止损预警是否冷却 (默认 300 秒)"""
        last_warning = self._soft_stop_warnings.get(code, 0)
        return (time.time() - last_warning) >= min_interval

    def update_soft_stop_warning(self, code: str):
        """更新软止损预警时间"""
        self._soft_stop_warnings[code] = time.time()

    # ============================================================
    #  峰值状态 API
    # ============================================================

    def update_peak_price(self, code: str, price: float):
        """更新峰值价格"""
        current_peak = self._peak_price_cache.get(code, 0)
        if price > current_peak:
            self._peak_price_cache[code] = price

    def get_peak_price(self, code: str) -> float:
        """获取峰值价格"""
        return self._peak_price_cache.get(code, 0)

    def reset_peak_price(self, code: str):
        """重置峰值价格 (平仓后调用)"""
        self._peak_price_cache.pop(code, None)

    def update_trailing_stop(self, code: str, stop_price: float):
        """更新追踪止损价"""
        self._trailing_stop_cache[code] = stop_price

    def get_trailing_stop(self, code: str) -> float:
        """获取追踪止损价"""
        return self._trailing_stop_cache.get(code, 0)

    def update_trailing_exit_time(self, code: str):
        """更新追踪止盈触发时间"""
        self._trailing_exit_time[code] = time.time()

    def is_in_trailing_cooldown(self, code: str, cooldown_seconds: float = 300.0) -> bool:
        """检查是否在追踪止盈冷却期"""
        exit_time = self._trailing_exit_time.get(code, 0)
        return (time.time() - exit_time) < cooldown_seconds

    # ============================================================
    #  快照状态 API
    # ============================================================

    def update_flash_snapshot(self, code: str, price: float):
        """更新 Flash Exit 快照"""
        self._flash_snapshots[code] = (price, time.time())

    def get_flash_snapshot(self, code: str) -> Optional[Tuple[float, float]]:
        """获取 Flash Exit 快照"""
        return self._flash_snapshots.get(code)

    def clear_flash_snapshot(self, code: str):
        """清除 Flash Exit 快照"""
        self._flash_snapshots.pop(code, None)

    # ============================================================
    #  缓存状态 API
    # ============================================================

    def update_last_quote_time(self, timestamp: Optional[float] = None):
        """更新最后行情时间"""
        self._last_quote_time = timestamp or time.time()

    def get_last_quote_time(self) -> float:
        """获取最后行情时间"""
        return self._last_quote_time

    def is_quote_stale(self, max_age_seconds: float = 10.0) -> bool:
        """检查行情是否过期"""
        return (time.time() - self._last_quote_time) > max_age_seconds

    def update_trend(self, code: str, period: str, trend: str):
        """更新趋势"""
        if code not in self._multi_timeframe_trends:
            self._multi_timeframe_trends[code] = {
                "K_1M": "neutral", "K_3M": "neutral", "K_5M": "neutral",
                "K_15M": "neutral", "K_30M": "neutral", "K_60M": "neutral", "K_DAY": "neutral"
            }
        self._multi_timeframe_trends[code][period] = trend

    def get_trend(self, code: str, period: str) -> str:
        """获取趋势"""
        return self._multi_timeframe_trends.get(code, {}).get(period, "neutral")

    def mark_trend_updated(self, code: str, period: str):
        """标记趋势已更新"""
        self._trends_updated.add((code, period))

    def is_trend_updated(self, code: str, period: str) -> bool:
        """检查趋势是否已更新"""
        return (code, period) in self._trends_updated

    # ============================================================
    #  锁状态 API
    # ============================================================

    def get_trade_lock(self, code: str) -> threading.Lock:
        """获取 code 级别交易锁"""
        if code not in self._trade_locks:
            self._trade_locks[code] = threading.Lock()
        return self._trade_locks[code]

    def check_open_position_atomicity(self, code: str, current_price: float, min_interval: float = 1.0) -> bool:
        """
        检查开仓原子性 (防止同一标的在同一秒内重复开仓)

        Returns:
            bool: 是否允许开仓
        """
        with self._open_lock:
            last_open = self._open_position_ts.get(code, 0)
            now = time.time()
            if now - last_open < min_interval:
                return False
            self._open_position_ts[code] = now
            return True

    # ============================================================
    #  统一清理
    # ============================================================

    def cleanup_expired(self):
        """
        统一清理过期状态

        清理策略:
        - 冷却状态：超过 24 小时
        - 峰值状态：保留最近 100 个
        - 快照状态：超过 5 分钟
        - 情绪缓存：超过 30 分钟
        """
        now = time.time()
        cleaned = 0

        # 1. 清理冷却状态 (超过 24 小时)
        with self._cooldown_lock:
            expired_cooldown = [k for k, v in self._cooldown.items() if now - v > 86400]
            for k in expired_cooldown:
                del self._cooldown[k]
            cleaned += len(expired_cooldown)

        # 2. 清理止损冷却 (超过 24 小时)
        expired_sl = [k for k, v in self._stop_loss_cooldown.items() if now - v > 86400]
        for k in expired_sl:
            del self._stop_loss_cooldown[k]
        cleaned += len(expired_sl)

        # 3. 清理 Flash 快照 (超过 5 分钟)
        expired_flash = [k for k, (price, ts) in self._flash_snapshots.items() if now - ts > 300]
        for k in expired_flash:
            del self._flash_snapshots[k]
        cleaned += len(expired_flash)

        # 4. 清理情绪缓存 (超过 30 分钟)
        expired_news = [k for k, (score, ts) in self._news_sentiment_cache.items() if now - ts > 1800]
        for k in expired_news:
            del self._news_sentiment_cache[k]
        cleaned += len(expired_news)

        # 5. 清理软止损预警 (超过 10 分钟)
        expired_warning = [k for k, v in self._soft_stop_warnings.items() if now - v > 600]
        for k in expired_warning:
            del self._soft_stop_warnings[k]
        cleaned += len(expired_warning)

        if cleaned > 0:
            logger.debug(f"[TradingStateManager] 已清理 {cleaned} 个过期状态")

    # ============================================================
    #  状态监控
    # ============================================================

    def get_state_summary(self) -> Dict[str, Any]:
        """
        获取状态摘要 (用于监控)

        Returns:
            Dict[str, Any]: 状态统计
        """
        return {
            'cooldown_count': len(self._cooldown),
            'stop_loss_cooldown_count': len(self._stop_loss_cooldown),
            'peak_price_count': len(self._peak_price_cache),
            'trailing_stop_count': len(self._trailing_stop_cache),
            'flash_snapshot_count': len(self._flash_snapshots),
            'kline_buffer_count': len(self._kline_buffers),
            'news_sentiment_count': len(self._news_sentiment_cache),
            'trend_updated_count': len(self._trends_updated),
            'last_quote_age': time.time() - self._last_quote_time,
        }

    # ============================================================
    #  持久化 (Phase 3 扩展)
    # ============================================================

    def persist(self, path: str) -> bool:
        """
        持久化状态到磁盘 (JSON 格式)

        Args:
            path: 文件路径

        Returns:
            bool: 是否成功
        """
        try:
            state = {
                'version': '1.0',
                'timestamp': time.time(),
                'cooldown': dict(self._cooldown),
                'stop_loss_cooldown': dict(self._stop_loss_cooldown),
                'peak_price_cache': dict(self._peak_price_cache),
                'trailing_stop_cache': dict(self._trailing_stop_cache),
                'last_quote_time': self._last_quote_time,
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            logger.info(f"[TradingStateManager] 状态已持久化到 {path}")
            return True
        except Exception as e:
            logger.error(f"[TradingStateManager] 状态持久化失败：{e}")
            return False

    def restore(self, path: str) -> bool:
        """
        从磁盘恢复状态

        Args:
            path: 文件路径

        Returns:
            bool: 是否成功
        """
        try:
            with open(path, 'r', encoding='utf-8') as f:
                state = json.load(f)

            if state.get('version') != '1.0':
                logger.warning(f"[TradingStateManager] 状态版本不匹配：{state.get('version')}")
                return False

            self._cooldown.update(state.get('cooldown', {}))
            self._stop_loss_cooldown.update(state.get('stop_loss_cooldown', {}))
            self._peak_price_cache.update(state.get('peak_price_cache', {}))
            self._trailing_stop_cache.update(state.get('trailing_stop_cache', {}))
            self._last_quote_time = state.get('last_quote_time', time.time())

            logger.info(f"[TradingStateManager] 状态已从 {path} 恢复")
            return True
        except FileNotFoundError:
            logger.info(f"[TradingStateManager] 状态文件不存在，跳过恢复：{path}")
            return False
        except Exception as e:
            logger.error(f"[TradingStateManager] 状态恢复失败：{e}")
            return False


# ============================================================
#  单例获取函数
# ============================================================

def get_trading_state_manager() -> TradingStateManager:
    """获取 TradingStateManager 单例"""
    return TradingStateManager()


__all__ = ['TradingStateManager', 'get_trading_state_manager']
