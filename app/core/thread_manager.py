"""
线程生命周期管理器 — ThreadManager

统一管理所有后台线程的启动、停止和健康检查。
替代 scheduler_core.py 中散布的线程创建逻辑。

使用:
    tm = ThreadManager()
    tm.add("scan", scheduler._scan_loop)
    tm.add("monitor", scheduler._monitor_loop)
    tm.start_all()
    # ... 运行中 ...
    tm.stop_all(timeout=5)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Any, List, Callable, Optional

logger = logging.getLogger(__name__)


class ManagedThread:
    """被管理的线程包装器"""

    def __init__(self, name: str, target: Callable, daemon: bool = True):
        self.name = name
        self.target = target
        self.daemon = daemon
        self.thread: Optional[threading.Thread] = None
        self.started_at: float = 0.0
        self.error_count: int = 0
        self.last_error: Optional[str] = None

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def uptime_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return time.time() - self.started_at


class ThreadManager:
    """
    线程生命周期管理器

    提供:
    - add(): 注册线程
    - start_all(): 启动所有线程
    - stop_all(): 优雅停止所有线程
    - health_check(): 检查所有线程状态
    """

    def __init__(self):
        self._threads: Dict[str, ManagedThread] = {}
        self._lock = threading.RLock()
        self._running = False

    def add(self, name: str, target: Callable, daemon: bool = True) -> "ThreadManager":
        """注册一个线程（不立即启动）"""
        with self._lock:
            self._threads[name] = ManagedThread(name=name, target=target, daemon=daemon)
            logger.debug("[ThreadManager] 已注册线程: %s", name)
        return self

    def start(self, name: str) -> bool:
        """启动单个线程"""
        with self._lock:
            if name not in self._threads:
                logger.warning("[ThreadManager] 未知线程: %s", name)
                return False
            mt = self._threads[name]
            if mt.is_alive:
                logger.debug("[ThreadManager] 线程已在运行: %s", name)
                return True

            def _wrapper():
                try:
                    mt.target()
                except Exception as e:
                    mt.error_count += 1
                    mt.last_error = str(e)
                    logger.error("[ThreadManager] 线程 %s 异常退出: %s", name, e)

            mt.thread = threading.Thread(
                target=_wrapper,
                name=name,
                daemon=mt.daemon,
            )
            mt.thread.start()
            mt.started_at = time.time()
            mt.error_count = 0
            mt.last_error = None
            logger.info("[ThreadManager] 已启动线程: %s", name)
            return True

    def start_all(self) -> List[str]:
        """启动所有已注册线程"""
        self._running = True
        started = []
        for name in list(self._threads.keys()):
            if self.start(name):
                started.append(name)
        logger.info("[ThreadManager] 已启动 %d/%d 个线程: %s",
                    len(started), len(self._threads), started)
        return started

    def stop(self, name: str, timeout: float = 5.0) -> bool:
        """停止单个线程"""
        with self._lock:
            if name not in self._threads:
                return False
            mt = self._threads[name]
            if mt.thread and mt.thread.is_alive():
                mt.thread.join(timeout=timeout)
                if mt.thread.is_alive():
                    logger.warning("[ThreadManager] 线程 %s 未能在 %.1fs 内退出", name, timeout)
                    return False
            logger.info("[ThreadManager] 已停止线程: %s", name)
            return True

    def stop_all(self, timeout: float = 5.0) -> Dict[str, bool]:
        """停止所有线程"""
        self._running = False
        results = {}
        for name in list(self._threads.keys()):
            results[name] = self.stop(name, timeout)
        alive = [n for n, ok in results.items() if not ok]
        if alive:
            logger.warning("[ThreadManager] %d 个线程未正常退出: %s", len(alive), alive)
        else:
            logger.info("[ThreadManager] 所有线程已停止")
        return results

    def health_check(self) -> Dict[str, Any]:
        """检查所有线程健康状态"""
        details = {}
        alive_count = 0
        dead_count = 0

        with self._lock:
            for name, mt in self._threads.items():
                is_alive = mt.is_alive
                details[name] = {
                    "alive": is_alive,
                    "uptime_seconds": round(mt.uptime_seconds, 1),
                    "error_count": mt.error_count,
                    "last_error": mt.last_error,
                }
                if is_alive:
                    alive_count += 1
                else:
                    dead_count += 1

        return {
            "total": len(details),
            "alive": alive_count,
            "dead": dead_count,
            "running": self._running,
            "details": details,
        }

    def get_thread_names(self) -> List[str]:
        """获取所有线程名称"""
        with self._lock:
            return list(self._threads.keys())


# ================================================================
#  预定义线程工厂
# ================================================================
def create_default_threads(scheduler) -> ThreadManager:
    """
    为 TradingScheduler 创建默认线程集

    集中管理所有后台线程，替代 scheduler.run_forever() 中的内联创建。
    """
    tm = ThreadManager()

    # 信号扫描线程
    tm.add("SignalScanLoop", scheduler._scan_loop)

    # 持仓监控线程
    tm.add("PositionMonitorLoop", scheduler._monitor_loop)

    # LLM 策略引擎线程
    if hasattr(scheduler, '_llm_strategy_loop'):
        tm.add("LLMStrategyLoop", scheduler._llm_strategy_loop)

    # 新闻管线 V2 线程
    if hasattr(scheduler, '_news_v2_loop') and scheduler.news_pipeline_v2:
        tm.add("NewsV2Loop", scheduler._news_v2_loop)

    # VIX 监控线程
    if hasattr(scheduler, '_vix_monitor_loop') and scheduler.vix_monitor:
        tm.add("VIXMonitorLoop", scheduler._vix_monitor_loop)

    return tm
