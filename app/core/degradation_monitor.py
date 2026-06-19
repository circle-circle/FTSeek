"""DegradationMonitor — 降级计数器与告警 (P0 2026-06-17)

审计发现: 代码库大量使用 `except Exception → logger.debug → 继续` 模式,
导致系统可能在多个模块降级运行, 但没有任何外部可见的告警.

本模块提供:
1. 每模块的 silent catch 计数器 (线程安全)
2. 超过阈值 (10 次/小时) 时触发 PushHub 告警
3. Dashboard /system 页面可查询的降级状态面板
4. 告警冷却 (同一模块 30 分钟内不重复告警)

Usage:
    from app.core.degradation_monitor import get_degradation_monitor

    # 在 except 块中调用:
    try:
        result = risky_operation()
    except Exception as e:
        get_degradation_monitor().record("module_name", str(e))
        result = DEFAULT_VALUE  # 降级路径
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class DegradationRecord:
    """单个模块的降级记录"""
    module: str
    count: int = 0                     # 当前窗口内的降级次数
    first_seen: float = 0.0            # 窗口内首次降级时间
    last_seen: float = 0.0             # 最近一次降级时间
    last_alert_ts: float = 0.0         # 最近一次告警时间
    last_error: str = ""               # 最近一次错误信息
    total_count: int = 0               # 历史总降级次数 (不重置)


@dataclass
class DegradationSnapshot:
    """供 Dashboard /system 页面读取的降级状态快照"""
    degraded_modules: List[Dict[str, Any]]  # 当前处于降级状态的模块
    alert_history: List[Dict[str, Any]]     # 最近 20 条告警
    is_any_degraded: bool                   # 是否有模块正在降级


class DegradationMonitor:
    """降级计数器与告警 (线程安全单例)"""

    # 告警阈值: 同一模块 1 小时内降级次数超过此值 → 触发告警
    ALERT_THRESHOLD: int = 10

    # 计数窗口: 1 小时 (秒)
    WINDOW_SECONDS: float = 3600.0

    # 告警冷却: 同一模块 30 分钟内不重复告警 (秒)
    ALERT_COOLDOWN: float = 1800.0

    def __init__(self):
        self._records: Dict[str, DegradationRecord] = {}
        self._alert_history: List[Dict[str, Any]] = []
        self._max_alert_history: int = 50
        self._lock = threading.Lock()

    def record(self, module: str, error_msg: str = "") -> None:
        """记录一次降级事件

        Args:
            module: 模块名称 (如 "shared_kernel", "market_data_router", "vix_fetcher")
            error_msg: 错误信息 (截断到 200 字符)
        """
        now = time.time()
        msg = str(error_msg)[:200]

        with self._lock:
            if module not in self._records:
                self._records[module] = DegradationRecord(module=module)

            rec = self._records[module]
            rec.total_count += 1
            rec.last_seen = now
            rec.last_error = msg

            # 窗口过期 → 重置计数
            if now - rec.first_seen > self.WINDOW_SECONDS:
                rec.count = 0
                rec.first_seen = now

            rec.count += 1

            # 检查是否需要告警
            if (rec.count >= self.ALERT_THRESHOLD
                    and now - rec.last_alert_ts > self.ALERT_COOLDOWN):
                self._fire_alert(rec, now)

    def _fire_alert(self, rec: DegradationRecord, now: float) -> None:
        """触发 PushHub 告警 (best-effort, 不阻塞)"""
        rec.last_alert_ts = now

        alert_msg = (
            f"⚠️ [降级告警] {rec.module} 在过去 1h 内降级 {rec.count} 次 "
            f"(阈值 {self.ALERT_THRESHOLD})\n"
            f"最近错误: {rec.last_error}\n"
            f"历史总降级: {rec.total_count} 次"
        )
        logger.warning(alert_msg)

        # 记录到告警历史
        self._alert_history.append({
            "ts": now,
            "module": rec.module,
            "count": rec.count,
            "error": rec.last_error,
            "total": rec.total_count,
        })
        if len(self._alert_history) > self._max_alert_history:
            self._alert_history = self._alert_history[-self._max_alert_history:]

        # 异步推送 PushHub (best-effort)
        try:
            from app.push.hub import PushHub, PushMessage
            PushHub.push(PushMessage(
                category="degradation_alert",
                severity="WARNING",
                title=f"降级告警: {rec.module}",
                body=alert_msg,
            ))
        except Exception:
            pass  # PushHub 不可用时不影响主流程

    def get_snapshot(self) -> DegradationSnapshot:
        """获取降级状态快照 (供 Dashboard 读取)"""
        now = time.time()
        degraded = []

        with self._lock:
            for module, rec in self._records.items():
                # 只展示 1h 内有降级记录的模块
                if now - rec.last_seen <= self.WINDOW_SECONDS and rec.count > 0:
                    degraded.append({
                        "module": module,
                        "count_1h": rec.count,
                        "total_count": rec.total_count,
                        "last_error": rec.last_error,
                        "last_seen_ago_sec": round(now - rec.last_seen),
                        "is_alerting": rec.count >= self.ALERT_THRESHOLD,
                    })

            # 按降级次数排序 (最严重的在前)
            degraded.sort(key=lambda x: x["count_1h"], reverse=True)

            return DegradationSnapshot(
                degraded_modules=degraded,
                alert_history=list(self._alert_history[-20:]),
                is_any_degraded=len(degraded) > 0,
            )

    def get_module_count(self, module: str) -> int:
        """获取指定模块当前窗口内的降级次数"""
        now = time.time()
        with self._lock:
            rec = self._records.get(module)
            if rec is None:
                return 0
            if now - rec.first_seen > self.WINDOW_SECONDS:
                return 0
            return rec.count

    def reset_module(self, module: str) -> None:
        """重置指定模块的计数器 (如模块恢复后手动重置)"""
        with self._lock:
            if module in self._records:
                self._records[module].count = 0
                self._records[module].first_seen = 0.0


# ── 单例 ──

_singleton: Optional[DegradationMonitor] = None
_singleton_lock = threading.Lock()


def get_degradation_monitor() -> DegradationMonitor:
    """获取 DegradationMonitor 单例 (线程安全)"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = DegradationMonitor()
    return _singleton
