#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lead-Lag 追踪器 (Lead-Lag Tracker)

功能:
- 实时记录价格变动
- 检测跨市场滞后关系 (美股→港股 / 港股→美股)
- 计算响应速度、相关性、强度

使用场景:
- 跨市场套利策略
- 新闻事件驱动策略
- ADR 溢价套利

示例:
    tracker = LeadLagTracker()
    tracker.record_price_move("US.TSLA", timestamp, 5.0)  # 美股涨 5%
    tracker.record_price_move("HK.00700", timestamp + 180, 3.2)  # 港股 3 分钟后跟涨
    metrics = tracker.detect_lag("US.TSLA", "HK.00700")
    # metrics.lag_seconds = 180
    # metrics.correlation = 0.85
    # metrics.strength = 0.7
"""

import logging
from app.storage.repository import get_db
import time
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek.strategy.lead_lag")
logger.propagate = False


@dataclass
class LagMetrics:
    """滞后关系指标"""
    lead_code: str = ""       # 领涨/领跌标的
    lag_code: str = ""        # 滞后标的
    lag_seconds: float = 0.0  # 滞后秒数
    correlation: float = 0.0  # 相关性 (-1 到 1)
    strength: float = 0.0     # 响应强度 (0-1)
    confidence: float = 0.0   # 置信度 (0-1)
    sample_count: int = 0     # 样本数
    direction: str = ""       # 方向 (positive/negative)
    calculated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'lead_code': self.lead_code,
            'lag_code': self.lag_code,
            'lag_seconds': self.lag_seconds,
            'correlation': self.correlation,
            'strength': self.strength,
            'confidence': self.confidence,
            'sample_count': self.sample_count,
            'direction': self.direction,
            'calculated_at': datetime.fromtimestamp(self.calculated_at).isoformat(),
        }


@dataclass
class PriceMove:
    """价格变动记录"""
    timestamp: float
    price_change_pct: float  # 价格变动百分比
    volume: float = 0.0
    is_significant: bool = False  # 是否显著变动 (>2%)


class LeadLagTracker:
    """
    Lead-Lag 追踪器

    核心逻辑:
    1. 记录每个标的的价格变动时间序列
    2. 使用交叉相关分析检测滞后关系
    3. 缓存计算结果，定期刷新
    """

    def __init__(self,
                 window_size: int = 300,  # 5 分钟窗口 (秒)
                 max_lag_seconds: int = 120,  # 最大检测滞后 120 秒
                 min_sample_count: int = 20,  # 最小样本数
                 db_path: str = None):
        """
        初始化追踪器

        Args:
            window_size: 滑动窗口大小 (秒)
            max_lag_seconds: 最大检测滞后时间
            min_sample_count: 最小样本数要求
            db_path: SQLite 数据库路径 (用于持久化)
        """
        self.window_size = window_size
        self.max_lag_seconds = max_lag_seconds
        self.min_sample_count = min_sample_count

        # 内存缓存：code -> deque of PriceMove
        self._windows: Dict[str, deque] = {}

        # 计算结果缓存：(lead, lag) -> (LagMetrics, expiry_time)
        self._cache: Dict[Tuple[str, str], Tuple[LagMetrics, float]] = {}
        self._cache_ttl = 60  # 缓存 60 秒

        # 数据库持久化 (可选)
        self._db_path = db_path or str(Path(__file__).parent.parent / "data" / "lead_lag.db")
        self._init_db()

        # 显著变动阈值 (用于检测事件驱动的价格变动)
        self.significant_threshold = 0.02  # 2%

        logger.info(
            f"Lead-Lag Tracker 已初始化 | "
            f"window={window_size}s, max_lag={max_lag_seconds}s, min_samples={min_sample_count}"
        )

    def _init_db(self):
        """初始化数据库"""
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS price_moves (
                    code TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    price_change_pct REAL NOT NULL,
                    volume REAL,
                    is_significant INTEGER,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lag_metrics (
                    lead_code TEXT NOT NULL,
                    lag_code TEXT NOT NULL,
                    lag_seconds REAL,
                    correlation REAL,
                    strength REAL,
                    confidence REAL,
                    sample_count INTEGER,
                    direction TEXT,
                    calculated_at REAL NOT NULL,
                    PRIMARY KEY (lead_code, lag_code, calculated_at)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_price_moves_code
                ON price_moves(code, timestamp)
            """)
            conn.commit()
            conn.close()
            logger.debug(f"数据库已初始化：{self._db_path}")
        except Exception as e:
            logger.warning(f"数据库初始化失败：{e}")
            self._db_path = None

    def record_price_move(self, code: str, timestamp: float,
                         price_change_pct: float, volume: float = 0.0):
        """
        记录价格变动

        Args:
            code: 标的代码 (US.TSLA / HK.00700)
            timestamp: 时间戳
            price_change_pct: 价格变动百分比 (如 5.0 表示涨 5%)
            volume: 成交量
        """
        # 内存记录
        if code not in self._windows:
            self._windows[code] = deque(maxlen=1000)

        move = PriceMove(
            timestamp=timestamp,
            price_change_pct=price_change_pct,
            volume=volume,
            is_significant=abs(price_change_pct) >= self.significant_threshold * 100
        )
        self._windows[code].append(move)

        # 清除过期缓存 (该标的涉及的所有滞后关系)
        self._invalidate_cache(code)

        # 数据库持久化 (可选)
        self._persist_price_move(code, move)

        if move.is_significant:
            logger.debug(f"[显著变动] {code} @ {datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')} : {price_change_pct:+.2f}%")

    def _persist_price_move(self, code: str, move: PriceMove):
        """持久化价格变动到数据库"""
        if not self._db_path:
            return
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO price_moves (code, timestamp, price_change_pct, volume, is_significant, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                code, move.timestamp, move.price_change_pct, move.volume,
                1 if move.is_significant else 0, time.time()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"持久化失败：{e}")

    def _invalidate_cache(self, code: str):
        """清除涉及某标的的缓存"""
        keys_to_delete = [
            k for k in self._cache.keys()
            if k[0] == code or k[1] == code
        ]
        for key in keys_to_delete:
            del self._cache[key]

    def detect_lag(self, lead_code: str, lag_code: str,
                   force_refresh: bool = False) -> LagMetrics:
        """
        检测滞后关系

        Args:
            lead_code: 领涨/领跌标的
            lag_code: 滞后标的
            force_refresh: 是否强制刷新 (忽略缓存)

        Returns:
            LagMetrics: 滞后关系指标
        """
        # 检查缓存
        if not force_refresh:
            cached = self._cache.get((lead_code, lag_code))
            if cached:
                metrics, expiry = cached
                if time.time() < expiry:
                    logger.debug(f"使用缓存滞后指标：{lead_code} → {lag_code}")
                    return metrics

        # 获取数据窗口
        lead_window = list(self._windows.get(lead_code, []))
        lag_window = list(self._windows.get(lag_code, []))

        if len(lead_window) < self.min_sample_count or len(lag_window) < self.min_sample_count:
            logger.debug(f"样本数不足：{lead_code}={len(lead_window)}, {lag_code}={len(lag_window)}")
            return LagMetrics(
                lead_code=lead_code,
                lag_code=lag_code,
                sample_count=len(lead_window),
                confidence=0.0
            )

        # 同步时间窗口 (只保留两个标的都有的时间段)
        lead_start = lead_window[0].timestamp
        lag_start = lag_window[0].timestamp
        common_start = max(lead_start, lag_start)

        lead_data = [(m.timestamp, m.price_change_pct) for m in lead_window if m.timestamp >= common_start]
        lag_data = [(m.timestamp, m.price_change_pct) for m in lag_window if m.timestamp >= common_start]

        if len(lead_data) < self.min_sample_count or len(lag_data) < self.min_sample_count:
            return LagMetrics(
                lead_code=lead_code,
                lag_code=lag_code,
                sample_count=min(len(lead_data), len(lag_data)),
                confidence=0.0
            )

        # 交叉相关分析
        metrics = self._calculate_cross_correlation(lead_code, lag_code, lead_data, lag_data)

        # 缓存结果
        self._cache[(lead_code, lag_code)] = (metrics, time.time() + self._cache_ttl)

        # 持久化到数据库
        self._persist_lag_metrics(metrics)

        logger.info(
            f"[Lead-Lag] {lead_code} → {lag_code}: "
            f"lag={metrics.lag_seconds:.1f}s, corr={metrics.correlation:.3f}, "
            f"strength={metrics.strength:.3f}, conf={metrics.confidence:.2f}"
        )

        return metrics

    def _calculate_cross_correlation(self, lead_code: str, lag_code: str,
                                     lead_data: List[Tuple[float, float]],
                                     lag_data: List[Tuple[float, float]]) -> LagMetrics:
        """
        计算交叉相关性

        方法:
        1. 将数据按时间对齐 (1 秒粒度)
        2. 计算不同滞后下的相关系数
        3. 找到最佳滞后时间和相关性
        """
        import numpy as np

        # 转换为秒级时间序列
        min_ts = min(lead_data[0][0], lag_data[0][0])
        max_ts = max(lead_data[-1][0], lag_data[-1][0])
        duration = int(max_ts - min_ts) + 1

        if duration > self.window_size:
            # 截断到窗口大小
            min_ts = max_ts - self.window_size
            duration = self.window_size

        # 创建时间序列数组 (1 秒粒度)
        lead_series = np.zeros(duration)
        lag_series = np.zeros(duration)

        # 填充数据 (使用最近的价格变动)
        for ts, pct in lead_data:
            idx = int(ts - min_ts)
            if 0 <= idx < duration:
                lead_series[idx] = pct

        for ts, pct in lag_data:
            idx = int(ts - min_ts)
            if 0 <= idx < duration:
                lag_series[idx] = pct

        # 计算不同滞后下的相关性
        max_lag = min(self.max_lag_seconds, duration // 2)
        correlations = []

        for lag in range(max_lag + 1):
            if lag >= len(lag_series):
                break

            # 计算滞后后的相关性
            lead_trimmed = lead_series[:-lag] if lag > 0 else lead_series
            lag_trimmed = lag_series[lag:]

            # 过滤零值 (没有价格变动的时间点)
            mask = (lead_trimmed != 0) | (lag_trimmed != 0)
            if np.sum(mask) < 5:
                continue

            corr = np.corrcoef(lead_trimmed[mask], lag_trimmed[mask])[0, 1]
            if not np.isnan(corr):
                correlations.append((lag, corr))

        if not correlations:
            return LagMetrics(
                lead_code=lead_code,
                lag_code=lag_code,
                sample_count=len(lead_data),
                confidence=0.0
            )

        # 找到最佳滞后
        best_lag, best_corr = max(correlations, key=lambda x: abs(x[1]))

        # 计算响应强度 (相关性 * 时间衰减)
        time_decay = 1.0 - (best_lag / max_lag) if max_lag > 0 else 1.0
        strength = abs(best_corr) * time_decay

        # 计算置信度 (基于样本数和相关性强度)
        sample_count = len(lead_data)
        # 置信度 = 样本数得分 (0-0.5) + 相关性得分 (0-0.5)
        sample_score = min(0.5, sample_count / 200)  # 200 个样本得满分 0.5
        correlation_score = abs(best_corr) * 0.5  # 相关性绝对值*0.5
        confidence = sample_score + correlation_score

        return LagMetrics(
            lead_code=lead_code,
            lag_code=lag_code,
            lag_seconds=float(best_lag),
            correlation=round(best_corr, 4),
            strength=round(strength, 4),
            confidence=round(confidence, 4),
            sample_count=sample_count,
            direction='positive' if best_corr > 0 else 'negative'
        )

    def _persist_lag_metrics(self, metrics: LagMetrics):
        """持久化滞后指标到数据库"""
        if not self._db_path:
            return
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO lag_metrics
                (lead_code, lag_code, lag_seconds, correlation, strength,
                 confidence, sample_count, direction, calculated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                metrics.lead_code, metrics.lag_code, metrics.lag_seconds,
                metrics.correlation, metrics.strength, metrics.confidence,
                metrics.sample_count, metrics.direction, time.time()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"持久化 lag_metrics 失败：{e}")

    def get_all_pairs(self) -> List[Tuple[str, str]]:
        """获取所有已计算的标的对"""
        return list(self._cache.keys())

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()

    def cleanup_old_data(self, keep_seconds: int = 3600):
        """清理旧数据"""
        cutoff = time.time() - keep_seconds

        for code in list(self._windows.keys()):
            window = self._windows[code]
            while window and window[0].timestamp < cutoff:
                window.popleft()
            if not window:
                del self._windows[code]

        logger.info(f"已清理 {cutoff} 之前的数据")


# 全局单例
_tracker: Optional[LeadLagTracker] = None


def get_lead_lag_tracker(window_size: int = 300,
                         max_lag_seconds: int = 120,
                         db_path: str = None) -> LeadLagTracker:
    """获取全局 Lead-Lag 追踪器单例"""
    global _tracker
    if _tracker is None:
        _tracker = LeadLagTracker(
            window_size=window_size,
            max_lag_seconds=max_lag_seconds,
            db_path=db_path
        )
    return _tracker


def reset_tracker():
    """重置追踪器 (用于测试)"""
    global _tracker
    _tracker = None
