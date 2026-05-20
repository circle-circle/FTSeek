#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkerManager — Worker 线程管理服务

架构位置:
  DataSubscriber._start_workers / _stop_workers / _quote_worker_loop / _kline_worker_loop
    ↓ 提取为
  app/services/worker_threads.py

设计目的:
  将 Worker 线程管理逻辑从 DataSubscriber 中提取为独立服务，
  包括线程启动/停止/循环逻辑等。

版本历史:
  2026-05-19 v1: 创建 (Phase 10F)
"""

import logging
import queue
import threading
from typing import Optional, Callable

logger = logging.getLogger("ftseek.services.worker_threads")


class WorkerManager:
    """Worker 线程管理服务 — Quote/Kline 队列处理"""

    def __init__(
        self,
        quote_processor: Callable,
        kline_processor: Callable,
        quote_queue_size: int = 50,
        kline_queue_size: int = 100,
    ):
        self._quote_processor = quote_processor
        self._kline_processor = kline_processor
        self._quote_queue: queue.Queue = queue.Queue(quote_queue_size)
        self._kline_queue: queue.Queue = queue.Queue(kline_queue_size)
        self._worker_running = False
        self._quote_worker: Optional[threading.Thread] = None
        self._kline_worker: Optional[threading.Thread] = None
        self._quote_lock = threading.Lock()
        self._kline_lock = threading.Lock()

    def start(self) -> None:
        """启动 Worker 线程"""
        if self._worker_running:
            logger.warning("[Worker 管理] 已启动，跳过")
            return

        self._worker_running = True
        self._quote_worker = threading.Thread(
            target=self._quote_worker_loop,
            daemon=True,
            name="QuoteWorker"
        )
        self._kline_worker = threading.Thread(
            target=self._kline_worker_loop,
            daemon=True,
            name="KlineWorker"
        )
        self._quote_worker.start()
        self._kline_worker.start()
        logger.info("[Worker 管理] Worker 线程已启动")

    def stop(self) -> None:
        """停止 Worker 线程"""
        if not self._worker_running:
            return

        self._worker_running = False
        # 发送哨兵值解除阻塞
        try:
            self._quote_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self._kline_queue.put_nowait(None)
        except queue.Full:
            pass

        logger.info("[Worker 管理] Worker 线程已停止")

    def _quote_worker_loop(self) -> None:
        """Quote 数据处理循环"""
        logger.info("[QuoteWorker] 启动")
        while self._worker_running:
            try:
                code = self._quote_queue.get(timeout=2)
            except queue.Empty:
                continue
            if code is None:
                break
            try:
                self._quote_processor(code)
            except Exception as e:
                logger.error(f"[QuoteWorker] 处理异常：{e}", exc_info=True)
        logger.info("[QuoteWorker] 退出")

    def _kline_worker_loop(self) -> None:
        """K 线数据处理循环"""
        logger.info("[KlineWorker] 启动")
        while self._worker_running:
            try:
                item = self._kline_queue.get(timeout=2)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                code, kline, k_type = item
                self._kline_processor(code, kline, k_type)
            except Exception as e:
                logger.error(f"[KlineWorker] 处理异常：{e}", exc_info=True)
        logger.info("[KlineWorker] 退出")

    def get_quote_queue(self) -> queue.Queue:
        """获取 Quote 队列"""
        return self._quote_queue

    def get_kline_queue(self) -> queue.Queue:
        """获取 K 线队列"""
        return self._kline_queue

    def is_running(self) -> bool:
        """检查是否运行中"""
        return self._worker_running
