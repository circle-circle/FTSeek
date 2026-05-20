#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一错误处理器 (Error Handler)

集中处理所有异常，提供：
1. 错误分类与统计
2. 错误率监控
3. 告警触发（飞书/钉钉）
4. 错误降级策略

设计原则:
- 单一职责：只处理错误，不处理业务逻辑
- 可配置：每个组件的错误率阈值可独立配置
- 可追溯：记录错误发生的位置、频率、影响
- 可告警：超过阈值时触发告警

使用示例:
    from app.core.error_handler import ErrorHandler

    try:
        # 业务逻辑
        pass
    except Exception as e:
        ErrorHandler.handle('llm_gate', e, severity='warning')

版本历史:
  2026-05-19: 创建
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class ErrorHandler:
    """
    统一错误处理器 — 集中处理所有异常

    特点:
    - 错误分类统计
    - 错误率监控（滑动窗口）
    - 告警触发（可配置回调）
    - 错误降级策略
    """

    _instance = None
    _lock = threading.Lock()

    # 错误严重程度
    SEVERITY_DEBUG = 'debug'
    SEVERITY_INFO = 'info'
    SEVERITY_WARNING = 'warning'
    SEVERITY_ERROR = 'error'
    SEVERITY_CRITICAL = 'critical'

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
        #  错误统计
        # ============================================================
        # 错误计数：component -> count
        self._error_counts: Dict[str, int] = defaultdict(int)
        self._error_counts_lock = threading.Lock()

        # 错误历史：component -> deque of timestamps (滑动窗口 1 小时)
        self._error_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

        # 最后错误时间：component -> timestamp
        self._last_error_time: Dict[str, float] = {}

        # ============================================================
        #  告警配置
        # ============================================================
        # 告警阈值：component -> threshold (errors per hour)
        self._alert_thresholds: Dict[str, int] = {
            'llm_gate': 10,          # 10 次/小时触发告警
            'fee_gate': 5,           # 5 次/小时触发告警
            'time_gate': 3,          # 3 次/小时触发告警
            'market_regime_gate': 5, # 5 次/小时触发告警
            'kline_process': 20,     # 20 次/小时触发告警
            'quote_handler': 50,     # 50 次/小时触发告警
            'trade_executor': 10,    # 10 次/小时触发告警
            'position_manager': 10,  # 10 次/小时触发告警
        }

        # 告警冷却：component -> last_alert_timestamp
        self._alert_cooldown: Dict[str, float] = {}
        self._alert_cooldown_seconds = 300  # 5 分钟冷却

        # 告警回调：List[Callable[[component, error, count], None]]
        self._alert_callbacks: List[Callable] = []

        # ============================================================
        #  降级策略
        # ============================================================
        # 降级计数器：component -> consecutive_error_count
        self._degradation_counters: Dict[str, int] = defaultdict(int)

        # 降级阈值：component -> threshold (连续错误次数触发降级)
        self._degradation_thresholds: Dict[str, int] = {
            'llm_gate': 5,       # 连续 5 次错误触发降级
            'market_regime_gate': 3,  # 连续 3 次错误触发降级
            'trend_filter': 5,   # 连续 5 次错误触发降级
        }

        # 降级状态：component -> is_degraded
        self._degradation_state: Dict[str, bool] = defaultdict(bool)

        # 降级恢复时间：component -> recover_timestamp
        self._degradation_recover_time: Dict[str, float] = {}

        self._initialized = True
        logger.info("[ErrorHandler] 已初始化 (单例模式)")

    # ============================================================
    #  核心 API
    # ============================================================

    def handle(self, component: str, error: Exception, severity: str = SEVERITY_WARNING,
               context: Optional[Dict] = None):
        """
        处理错误

        Args:
            component: 组件名称 (如 'llm_gate', 'fee_gate')
            error: 异常对象
            severity: 严重程度 (debug/info/warning/error/critical)
            context: 上下文信息 (可选)
        """
        now = time.time()

        # 1. 记录错误
        with self._error_counts_lock:
            self._error_counts[component] += 1
            self._error_history[component].append(now)
            self._last_error_time[component] = now

        # 2. 记录日志
        log_msg = f"[{component}] {type(error).__name__}: {str(error)[:200]}"
        if severity == self.SEVERITY_DEBUG:
            logger.debug(log_msg)
        elif severity == self.SEVERITY_INFO:
            logger.info(log_msg)
        elif severity == self.SEVERITY_WARNING:
            logger.warning(log_msg)
        elif severity == self.SEVERITY_ERROR:
            logger.error(log_msg, exc_info=True)
        elif severity == self.SEVERITY_CRITICAL:
            logger.critical(log_msg, exc_info=True)

        # 3. 检查告警
        if self._should_alert(component):
            self._trigger_alert(component, error, context)

        # 4. 检查降级
        self._check_degradation(component)

    def get_error_count(self, component: str) -> int:
        """获取错误计数"""
        return self._error_counts.get(component, 0)

    def get_error_rate(self, component: str, window_seconds: int = 3600) -> float:
        """
        获取错误率 (错误数/小时)

        Args:
            component: 组件名称
            window_seconds: 时间窗口 (默认 1 小时)

        Returns:
            float: 错误率
        """
        now = time.time()
        cutoff = now - window_seconds

        # 统计窗口内的错误数
        count = sum(1 for ts in self._error_history[component] if ts >= cutoff)
        return count * (3600 / window_seconds)  # 转换为每小时错误数

    def get_all_error_rates(self) -> Dict[str, float]:
        """获取所有组件的错误率"""
        return {
            comp: self.get_error_rate(comp)
            for comp in self._error_counts.keys()
        }

    def reset_counts(self, component: Optional[str] = None):
        """
        重置错误计数

        Args:
            component: 指定组件 (None=重置所有)
        """
        with self._error_counts_lock:
            if component:
                self._error_counts[component] = 0
                self._error_history[component].clear()
            else:
                self._error_counts.clear()
                self._error_history.clear()

    # ============================================================
    #  告警 API
    # ============================================================

    def register_alert_callback(self, callback: Callable[[str, Exception, Dict], None]):
        """
        注册告警回调

        Args:
            callback: 回调函数 (component, error, context) -> None
        """
        self._alert_callbacks.append(callback)
        logger.info(f"[ErrorHandler] 已注册告警回调 (当前 {len(self._alert_callbacks)} 个)")

    def set_alert_threshold(self, component: str, threshold: int):
        """
        设置告警阈值

        Args:
            component: 组件名称
            threshold: 每小时错误数阈值
        """
        self._alert_thresholds[component] = threshold

    def set_degradation_threshold(self, component: str, threshold: int):
        """
        设置降级阈值

        Args:
            component: 组件名称
            threshold: 连续错误次数阈值
        """
        self._degradation_thresholds[component] = threshold

    def _should_alert(self, component: str) -> bool:
        """检查是否应该触发告警"""
        # 检查冷却期
        last_alert = self._alert_cooldown.get(component, 0)
        if (time.time() - last_alert) < self._alert_cooldown_seconds:
            return False

        # 检查错误率
        threshold = self._alert_thresholds.get(component, 10)
        error_rate = self.get_error_rate(component)

        return error_rate >= threshold

    def _trigger_alert(self, component: str, error: Exception, context: Optional[Dict] = None):
        """触发告警"""
        self._alert_cooldown[component] = time.time()

        # 构建告警消息
        error_rate = self.get_error_rate(component)
        alert_msg = (
            f"[错误告警] {component} 错误率超过阈值\n"
            f"错误率：{error_rate:.1f} 次/小时\n"
            f"最新错误：{type(error).__name__}: {str(error)[:100]}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        logger.warning(alert_msg)

        # 调用所有注册的回调
        ctx = context or {}
        for callback in self._alert_callbacks:
            try:
                callback(component, error, ctx)
            except Exception as e:
                logger.error(f"[ErrorHandler] 告警回调执行失败：{e}")

    # ============================================================
    #  降级 API
    # ============================================================

    def _check_degradation(self, component: str):
        """检查是否需要降级"""
        threshold = self._degradation_thresholds.get(component)
        if threshold is None:
            return

        # 增加连续错误计数
        self._degradation_counters[component] += 1

        # 检查是否触发降级
        if self._degradation_counters[component] >= threshold:
            if not self._degradation_state[component]:
                self._trigger_degradation(component)

    def _trigger_degradation(self, component: str):
        """触发降级"""
        self._degradation_state[component] = True
        self._degradation_recover_time[component] = time.time() + 60.0  # 1 分钟后恢复

        logger.warning(
            f"[ErrorHandler] {component} 连续错误过多，触发降级 (1 分钟后自动恢复)"
        )

    def is_degraded(self, component: str) -> bool:
        """
        检查组件是否处于降级状态

        Args:
            component: 组件名称

        Returns:
            bool: 是否降级
        """
        if not self._degradation_state[component]:
            return False

        # 检查恢复时间
        recover_time = self._degradation_recover_time.get(component, 0)
        if time.time() >= recover_time:
            # 自动恢复
            self._degradation_state[component] = False
            self._degradation_counters[component] = 0
            logger.info(f"[ErrorHandler] {component} 已自动恢复")
            return False

        return True

    def reset_degradation(self, component: str):
        """手动重置降级状态"""
        self._degradation_state[component] = False
        self._degradation_counters[component] = 0

    def get_degradation_status(self) -> Dict[str, bool]:
        """获取所有组件的降级状态"""
        return dict(self._degradation_state)

    # ============================================================
    #  监控 API
    # ============================================================

    def get_summary(self) -> Dict[str, Any]:
        """
        获取错误处理摘要 (用于监控)

        Returns:
            Dict[str, Any]: 错误统计
        """
        return {
            'error_counts': dict(self._error_counts),
            'error_rates': self.get_all_error_rates(),
            'degradation_status': self.get_degradation_status(),
            'last_error_times': dict(self._last_error_time),
        }

    def get_top_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取错误最多的组件

        Args:
            limit: 返回数量

        Returns:
            List[Dict]: 错误统计
        """
        sorted_errors = sorted(
            self._error_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )[:limit]

        return [
            {
                'component': comp,
                'count': count,
                'rate': self.get_error_rate(comp),
            }
            for comp, count in sorted_errors
        ]


# ============================================================
#  单例获取函数
# ============================================================

def get_error_handler() -> ErrorHandler:
    """获取 ErrorHandler 单例"""
    return ErrorHandler()


__all__ = ['ErrorHandler', 'get_error_handler']
