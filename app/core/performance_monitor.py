#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能监控 — Provider 调用延迟监控

架构位置:
  app/core/interfaces.py (Provider 接口)
    ↓ 监控
  app/core/performance_monitor.py (延迟统计)

设计目的:
  1. 监控 Provider 调用延迟 (<1ms 目标)
  2. 识别慢查询 (>10ms 报警)
  3. 提供性能统计 (p50/p95/p99)

约束:
  - 监控开销 < 0.1ms
  - 不阻塞热路径
  - 统计窗口 1000 次调用

版本历史:
  2026-05-19 v1: 创建性能监控模块
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.performance")


class PerformanceMonitor:
    """
    性能监控器

    使用方式:
        monitor = get_performance_monitor()
        with monitor.track("get_adx"):
            adx = provider.get_adx(code)
    """

    def __init__(self, window_size: int = 1000, alert_threshold_ms: float = 10.0):
        """
        初始化监控器

        Args:
            window_size: 统计窗口大小 (默认 1000 次调用)
            alert_threshold_ms: 报警阈值 (默认 10ms)
        """
        self._window_size = window_size
        self._alert_threshold_ms = alert_threshold_ms

        # 延迟统计：{method_name: [latency_ms, ...]}
        self._latencies: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

        # 计数器
        self._call_count: Dict[str, int] = defaultdict(int)

        logger.info(
            f"[PerformanceMonitor] 初始化完成: "
            f"window_size={window_size}, alert_threshold={alert_threshold_ms}ms"
        )

    def track(self, method_name: str) -> 'TrackContext':
        """
        创建跟踪上下文

        Args:
            method_name: 方法名称

        Returns:
            TrackContext (支持 with 语句)
        """
        return TrackContext(self, method_name)

    def record_latency(self, method_name: str, latency_ms: float):
        """
        记录延迟

        Args:
            method_name: 方法名称
            latency_ms: 延迟 (毫秒)
        """
        with self._lock:
            # 添加延迟
            self._latencies[method_name].append(latency_ms)
            self._call_count[method_name] += 1

            # 超过窗口大小，移除最早的
            if len(self._latencies[method_name]) > self._window_size:
                self._latencies[method_name].pop(0)

            # 报警检查
            if latency_ms > self._alert_threshold_ms:
                logger.warning(
                    f"[PerformanceMonitor] 慢查询警告: {method_name} "
                    f"耗时 {latency_ms:.2f}ms > 阈值 {self._alert_threshold_ms}ms"
                )

    def get_stats(self, method_name: str = None) -> Dict:
        """
        获取性能统计

        Args:
            method_name: 方法名称 (None 返回所有方法)

        Returns:
            统计信息 {method_name: {p50, p95, p99, count, avg}}
        """
        with self._lock:
            if method_name is not None:
                return self._calc_stats(method_name)

            result = {}
            for name in self._latencies:
                result[name] = self._calc_stats(name)
            return result

    def _calc_stats(self, method_name: str) -> Dict:
        """计算单个方法的统计信息"""
        latencies = self._latencies.get(method_name, [])
        if not latencies:
            return {"count": 0, "p50": 0, "p95": 0, "p99": 0, "avg": 0}

        # 排序
        sorted_latencies = sorted(latencies)
        n = len(sorted_latencies)

        # 计算分位数
        p50_idx = int(n * 0.50)
        p95_idx = int(n * 0.95)
        p99_idx = int(n * 0.99)

        return {
            "count": n,
            "p50": sorted_latencies[min(p50_idx, n - 1)],
            "p95": sorted_latencies[min(p95_idx, n - 1)],
            "p99": sorted_latencies[min(p99_idx, n - 1)],
            "avg": sum(sorted_latencies) / n,
        }

    def report(self, method_name: str = None):
        """
        打印性能报告

        Args:
            method_name: 方法名称 (None 报告所有方法)
        """
        stats = self.get_stats(method_name)

        if method_name is not None:
            self._print_stats(method_name, stats)
        else:
            for name, stat in stats.items():
                self._print_stats(name, stat)

    def _print_stats(self, method_name: str, stats: Dict):
        """打印单个方法的统计信息"""
        logger.info(
            f"[PerformanceMonitor] {method_name}: "
            f"count={stats['count']}, "
            f"p50={stats['p50']:.2f}ms, "
            f"p95={stats['p95']:.2f}ms, "
            f"p99={stats['p99']:.2f}ms, "
            f"avg={stats['avg']:.2f}ms"
        )


class TrackContext:
    """跟踪上下文 (支持 with 语句)"""

    def __init__(self, monitor: PerformanceMonitor, method_name: str):
        self._monitor = monitor
        self._method_name = method_name
        self._start_time = None

    def __enter__(self):
        self._start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._start_time is not None:
            elapsed_ms = (time.perf_counter() - self._start_time) * 1000
            self._monitor.record_latency(self._method_name, elapsed_ms)


# 全局实例
_monitor: Optional[PerformanceMonitor] = None


def get_performance_monitor() -> PerformanceMonitor:
    """获取全局性能监控实例"""
    global _monitor
    if _monitor is None:
        _monitor = PerformanceMonitor()
    return _monitor
