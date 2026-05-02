"""
Worker 线程池 — 行情数据分发、生命周期管理

从 data_subscriber.py 拆分出来，职责：
1. 管理 quote_worker 和 kline_worker 线程
2. 数据队列分发（quote_queue / kline_queue）
3. 线程生命周期（启动/停止/重启）
4. 健康检查（超时检测、队列堆积监控）
"""

import logging
import time
import queue
import threading
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger("ftseek.intraday.worker_pool")


class WorkerPool:
    """
    Worker 线程池
    
    职责：管理数据处理线程的生命周期。
    不包含业务逻辑，只负责线程调度。
    """
    
    def __init__(self, quote_maxsize: int = 5000, kline_maxsize: int = 10000):
        """
        Args:
            quote_maxsize: 行情队列最大容量
            kline_maxsize: K 线队列最大容量
        """
        self._quote_queue: queue.Queue = queue.Queue(maxsize=quote_maxsize)
        self._kline_queue: queue.Queue = queue.Queue(maxsize=kline_maxsize)
        
        self._quote_worker: Optional[threading.Thread] = None
        self._kline_worker: Optional[threading.Thread] = None
        self._running = False
        
        # 回调函数
        self._on_quote_data: Optional[Callable] = None
        self._on_kline_data: Optional[Callable] = None
        
        # 健康监控
        self._last_quote_time: float = time.time()
        self._last_kline_time: float = time.time()
        self._quote_dropped: int = 0
        self._kline_dropped: int = 0
        
        # 锁
        self._start_lock = threading.Lock()
    
    def set_callbacks(self, on_quote=None, on_kline=None):
        """设置数据处理回调"""
        self._on_quote_data = on_quote
        self._on_kline_data = on_kline
    
    # ===== 生命周期 =====
    
    def start(self):
        """启动 Worker 线程"""
        with self._start_lock:
            if self._running:
                logger.warning("Worker 线程已在运行")
                return
            
            self._running = True
            self._quote_dropped = 0
            self._kline_dropped = 0
            
            self._quote_worker = threading.Thread(
                target=self._quote_loop, daemon=True, name="quote-worker"
            )
            self._kline_worker = threading.Thread(
                target=self._kline_loop, daemon=True, name="kline-worker"
            )
            
            self._quote_worker.start()
            self._kline_worker.start()
            
            logger.info("Worker 线程池已启动")
    
    def stop(self, timeout: float = 5.0):
        """停止 Worker 线程"""
        with self._start_lock:
            if not self._running:
                return
            
            self._running = False
            
            if self._quote_worker:
                self._quote_worker.join(timeout=timeout)
                if self._quote_worker.is_alive():
                    logger.warning("quote-worker 停止超时")
            
            if self._kline_worker:
                self._kline_worker.join(timeout=timeout)
                if self._kline_worker.is_alive():
                    logger.warning("kline-worker 停止超时")
            
            logger.info("Worker 线程池已停止")
    
    def restart(self):
        """重启 Worker 线程"""
        self.stop()
        time.sleep(0.5)
        self.start()
    
    # ===== 数据入队 =====
    
    def push_quote(self, data: Any) -> bool:
        """
        推入行情数据
        
        Returns:
            是否成功入队
        """
        self._last_quote_time = time.time()
        try:
            self._quote_queue.put_nowait(data)
            return True
        except queue.Full:
            self._quote_dropped += 1
            if self._quote_dropped % 100 == 0:
                logger.warning(f"行情队列已满，已丢弃 {self._quote_dropped} 条")
            return False
    
    def push_kline(self, data: Any) -> bool:
        """
        推入 K 线数据
        
        Returns:
            是否成功入队
        """
        self._last_kline_time = time.time()
        try:
            self._kline_queue.put_nowait(data)
            return True
        except queue.Full:
            self._kline_dropped += 1
            if self._kline_dropped % 100 == 0:
                logger.warning(f"K线队列已满，已丢弃 {self._kline_dropped} 条")
            return False
    
    # ===== Worker 循环 =====
    
    def _quote_loop(self):
        """行情 Worker 循环"""
        logger.info("quote-worker 启动")
        while self._running:
            try:
                data = self._quote_queue.get(timeout=1)
                if self._on_quote_data:
                    self._on_quote_data(data)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"quote-worker 异常: {e}", exc_info=True)
        logger.info("quote-worker 停止")
    
    def _kline_loop(self):
        """K 线 Worker 循环"""
        logger.info("kline-worker 启动")
        while self._running:
            try:
                data = self._kline_queue.get(timeout=1)
                if self._on_kline_data:
                    self._on_kline_data(data)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"kline-worker 异常: {e}", exc_info=True)
        logger.info("kline-worker 停止")
    
    # ===== 健康检查 =====
    
    def check_health(self, timeout_seconds: float = 30.0) -> Dict[str, Any]:
        """
        检查 Worker 健康状态
        
        Returns:
            健康状态字典
        """
        now = time.time()
        quote_idle = (now - self._last_quote_time) > timeout_seconds
        kline_idle = (now - self._last_kline_time) > timeout_seconds
        
        quote_alive = self._quote_worker is not None and self._quote_worker.is_alive()
        kline_alive = self._kline_worker is not None and self._kline_worker.is_alive()
        
        return {
            "running": self._running,
            "quote_worker_alive": quote_alive,
            "kline_worker_alive": kline_alive,
            "quote_queue_size": self._quote_queue.qsize(),
            "kline_queue_size": self._kline_queue.qsize(),
            "quote_dropped": self._quote_dropped,
            "kline_dropped": self._kline_dropped,
            "quote_idle": quote_idle,
            "kline_idle": kline_idle,
            "healthy": quote_alive and kline_alive and not quote_idle and not kline_idle,
        }
    
    def needs_restart(self, timeout_seconds: float = 30.0) -> bool:
        """是否需要重启"""
        health = self.check_health(timeout_seconds)
        return not health["healthy"]
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def queue_sizes(self) -> tuple:
        return (self._quote_queue.qsize(), self._kline_queue.qsize())
