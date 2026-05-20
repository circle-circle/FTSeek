#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FutuBridge — Futu OpenD 连接桥

架构位置:
  DataSubscriber → [FutuBridge] → Futu OpenD SDK

设计目的:
  将 Futu SDK 连接管理从 God Class (data_subscriber.py) 提取为独立模块，
  封装连接生命周期、回调注册、订阅管理、心跳检测。

职责:
  1. 管理 SDK 连接 (_quote_ctx)
  2. 注册回调 handler (MyQuoteHandler, MyKlineHandler)
  3. 管理订阅/退订 (subscribe, unsubscribe_all)
  4. 心跳检测和自动重连 (_last_quote_time, _reconnect)
  5. 启动预热 (_warm_up_buffers)

约束:
  - 热路径: Futu 回调 < 1ms (只做 queue.put，不做业务逻辑)
  - 线程安全: _reconnect_lock 保护重连操作
  - 依赖注入: quote_callback, kline_callback 通过 start_workers 注入

最终归宿:
  DataSubscriber 通过依赖注入持有 FutuBridge 实例,
  Adapter 退役后直接由 FutuBridge 实现 IFutuBridgeProvider 接口。

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取 SDK 连接管理逻辑
"""

import logging
import queue
import threading
import time
from typing import Optional, Callable, List, Any, Set

logger = logging.getLogger("ftseek.engine.futu")


class FutuBridge:
    """
    Futu OpenD 连接桥

    状态 (从 data_subscriber 迁移):
      - _quote_ctx: OpenQuoteContext — SDK 连接上下文
      - _subscribed: bool — 动态订阅状态标记
      - _last_quote_time: float — 最后一次收到行情的时间戳 (心跳检测)
      - _reconnect_lock: threading.Lock — 重连锁
      - _quote_handler: MyQuoteHandler — 行情回调 handler
      - _kline_handler: MyKlineHandler — K 线回调 handler

    热路径:
      - on_recv_rsp: < 1ms (只做 queue.put)
      - Worker 线程: 从 queue 取数据后执行业务逻辑
    """

    def __init__(
        self,
        opend_host: str = "127.0.0.1",
        opend_port: int = 11111,
        subscribe_types: List[str] = None,
        heartbeat_timeout: int = 180,
    ):
        """
        初始化 Futu 连接桥

        Args:
            opend_host: OpenD 主机地址
            opend_port: OpenD 端口
            subscribe_types: 订阅类型列表 ["QUOTE", "K_1M", ...]
            heartbeat_timeout: 心跳超时 (秒)，超过此时间触发重连
        """
        self._opend_host = opend_host
        self._opend_port = opend_port
        self._subscribe_types = subscribe_types or ["QUOTE", "K_1M", "K_5M", "K_15M"]
        self._heartbeat_timeout = heartbeat_timeout

        # =====================================================================
        #  状态 (从 data_subscriber 迁移)
        # =====================================================================

        # SDK 连接上下文
        self._quote_ctx: Any = None

        # 动态订阅状态标记 (True=已订阅, False=已退订)
        self._subscribed: bool = False

        # 心跳检测: 最后一次收到行情的时间戳
        self._last_quote_time: float = time.time()

        # 重连锁 (防止多线程同时重连)
        self._reconnect_lock = threading.Lock()

        # 回调 handler (懒加载)
        self._quote_handler: Any = None
        self._kline_handler: Any = None

        # Worker 队列 (由 start_workers 注入)
        self._quote_queue: Optional[queue.Queue] = None
        self._kline_queue: Optional[queue.Queue] = None
        self._pending_codes: Set[str] = set()
        self._quote_lock = threading.Lock()

        # 回调函数 (由 start_workers 注入)
        self._quote_callback: Optional[Callable] = None
        self._kline_callback: Optional[Callable] = None

        logger.info(
            f"[FutuBridge] 初始化完成: "
            f"host={opend_host}:{opend_port}, "
            f"subscribe_types={subscribe_types}, "
            f"heartbeat_timeout={heartbeat_timeout}s"
        )

    # =========================================================================
    #  连接生命周期
    # =========================================================================

    def connect(self) -> bool:
        """
        创建 Futu OpenD 连接

        Returns:
            True=连接成功, False=连接失败
        """
        if self._quote_ctx is not None:
            logger.debug("[FutuBridge] 连接已存在")
            return True

        try:
            import logging as stdlib_logging

            # 配置 futu 日志到临时目录，避免文件锁冲突
            futu_logger = stdlib_logging.getLogger("futu")
            futu_logger.setLevel(stdlib_logging.ERROR)

            # 移除现有的文件 handler
            for handler in futu_logger.handlers[:]:
                if isinstance(handler, stdlib_logging.FileHandler):
                    futu_logger.removeHandler(handler)

            from futu import OpenQuoteContext
            self._quote_ctx = OpenQuoteContext(
                host=self._opend_host, port=self._opend_port
            )
            logger.info("[FutuBridge] 行情上下文已创建")
            return True

        except Exception as e:
            logger.error(f"[FutuBridge] 创建行情上下文失败：{e}")
            return False

    def disconnect(self) -> None:
        """断开 Futu OpenD 连接"""
        try:
            if self._quote_ctx is not None:
                self._quote_ctx.close()
                logger.info("[FutuBridge] 连接已断开")
        except Exception as e:
            logger.warning(f"[FutuBridge] 断开连接异常：{e}")
        finally:
            self._quote_ctx = None
            self._subscribed = False

    def is_connected(self) -> bool:
        """检查连接是否有效"""
        if self._quote_ctx is None:
            return False
        try:
            # 测试连接是否有效
            ret, data = self._quote_ctx.get_basic_info("HK.00700", num=1)
            return ret == 0
        except Exception:
            return False

    # =========================================================================
    #  回调注册
    # =========================================================================

    def setup_handlers(
        self,
        quote_queue: queue.Queue,
        kline_queue: queue.Queue,
    ) -> None:
        """
        设置 Futu SDK 回调 handler

        Args:
            quote_queue: 行情队列 (QuoteWorker 消费)
            kline_queue: K 线队列 (KlineWorker 消费)
        """
        self._quote_queue = quote_queue
        self._kline_queue = kline_queue

        ctx = self.connect()
        if not ctx:
            logger.error("[FutuBridge] 连接失败，无法设置 handler")
            return

        bridge = self

        # =====================================================================
        #  MyQuoteHandler: 行情回调 (< 1ms, 只做 queue.put)
        # =====================================================================
        from futu import StockQuoteHandlerBase

        class MyQuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_str):
                # 心跳更新 (必须在回调线程中快速执行)
                bridge._last_quote_time = time.time()
                try:
                    ret, data = super().on_recv_rsp(rsp_str)
                    if ret != 0:
                        return
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        quote = row.to_dict()
                        with bridge._quote_lock:
                            # 更新最新报价 (覆盖旧值)
                            bridge._latest_quotes[code] = quote
                            # 同 code 已在队列中 → 跳过
                            if code not in bridge._pending_codes:
                                bridge._pending_codes.add(code)
                                try:
                                    bridge._quote_queue.put_nowait(code)
                                except queue.Full:
                                    bridge._pending_codes.discard(code)
                                    logger.warning(f"[QuoteHandler] 队列已满，丢弃 {code} 行情")
                except Exception as e:
                    logger.error(f"[QUOTE回调异常] {e}", exc_info=True)

        # =====================================================================
        #  MyKlineHandler: K 线回调 (< 1ms, 只做 queue.put)
        # =====================================================================
        from futu import CurKlineHandlerBase

        class MyKlineHandler(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_str):
                # 心跳更新
                bridge._last_quote_time = time.time()
                try:
                    ret, data = super().on_recv_rsp(rsp_str)
                    if ret != 0:
                        return
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        kline = row.to_dict()
                        k_type = kline.get("k_type", "K_1M")
                        try:
                            bridge._kline_queue.put_nowait((code, kline, k_type))
                        except queue.Full:
                            logger.warning(f"[KlineHandler] 队列已满，丢弃 {code} {k_type}")
                except Exception as e:
                    logger.error(f"[KLINE回调异常] {e}", exc_info=True)

        self._quote_handler = MyQuoteHandler()
        self._kline_handler = MyKlineHandler()
        ctx.set_handler(self._quote_handler)
        ctx.set_handler(self._kline_handler)
        logger.info("[FutuBridge] 行情回调 handler 已设置 (quote + kline，异步处理模式)")

    # =========================================================================
    #  订阅管理
    # =========================================================================

    def subscribe(self, codes: List[str]) -> bool:
        """
        订阅行情

        Args:
            codes: 股票代码列表 ["HK.00700", ...]

        Returns:
            True=订阅成功, False=订阅失败
        """
        from futu import SubType

        ctx = self.connect()
        if not ctx:
            logger.error("[FutuBridge] 连接失败，无法订阅")
            return False

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }

        # 过滤港股
        subscribe_list = [c for c in codes if c.startswith("HK.")]

        sub_types = [sub_type_map[st] for st in self._subscribe_types if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        logger.info(f"[FutuBridge] 开始订阅 {len(subscribe_list)} 只港股标的, 类型={[str(s) for s in sub_types]}")
        success_count = 0
        fail_count = 0

        for code in subscribe_list:
            for st in sub_types:
                ret, msg = ctx.subscribe([code], [st])
                if ret == 0:
                    success_count += 1
                    logger.debug(f"[FutuBridge] 订阅成功: {code} -> {st}")
                else:
                    fail_count += 1
                    if "无权限" in str(msg) or "no permission" in str(msg).lower():
                        logger.warning(f"[FutuBridge] 订阅跳过(无权限): {code} -> {st} | {msg}")
                    else:
                        logger.error(f"[FutuBridge] 订阅失败: {code} -> {st}: {msg}")

        logger.info(f"[FutuBridge] 订阅完成: 成功={success_count}, 失败={fail_count}, 总计={success_count+fail_count}")
        self._subscribed = True

        # 启动预热
        self._warm_up_buffers(codes)

        return True

    def unsubscribe_all(self, codes: List[str]) -> None:
        """
        取消所有港股行情订阅 (收盘释放槽位)

        Args:
            codes: 股票代码列表
        """
        from futu import SubType

        if not self._quote_ctx:
            logger.debug("[FutuBridge] 无活跃连接，无需退订")
            return

        # 过滤港股
        codes = [c for c in codes if c.startswith("HK.")]
        if not codes:
            logger.debug("[FutuBridge] 无代码，无需退订")
            return

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }
        sub_types = [sub_type_map[st] for st in self._subscribe_types if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        logger.info(f"[FutuBridge] 开始退订 {len(codes)} 只港股标的 (收盘释放槽位)...")
        success_count = 0
        fail_count = 0

        for code in codes:
            for st in sub_types:
                try:
                    ret, msg = self._quote_ctx.unsubscribe([code], [st])
                    if ret == 0:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.warning(f"[FutuBridge] 退订 {code} {st} 异常: {e}")

        self._subscribed = False
        logger.info(f"[FutuBridge] 退订完成: 成功={success_count}, 失败={fail_count}")

    def _warm_up_buffers(self, codes: List[str]) -> None:
        """
        启动预热 — 从 Futu 拉取历史 K 线填充 MTF 缓存

        目标:
            系统重启或隔夜后，内存中的 K 线 Buffer 为空。
            本方法会拉取最近 3 天的 K 线数据并推入 Buffer，
            从而立即初始化多周期趋势 (_multi_timeframe_trends)。

        Args:
            codes: 股票代码列表
        """
        import time
        from datetime import datetime, timedelta
        from futu import KLType

        if not codes:
            logger.warning("[FutuBridge] 无 Watchlist，跳过预热")
            return

        logger.info(f"[FutuBridge] 开始拉取 {len(codes)} 只股票历史数据...")

        # 定义需要的 K 线类型和数量
        warmup_targets = [
            (KLType.K_60M, 50),
            (KLType.K_DAY, 60),
            (KLType.K_15M, 100),
        ]

        success_count = 0
        fail_count = 0

        for code in codes:
            try:
                for ktype, count in warmup_targets:
                    end_time = datetime.now().strftime('%Y-%m-%d')
                    start_time = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')

                    ret, data, _ = self._quote_ctx.request_history_kline(
                        code, start=start_time, end=end_time, ktype=ktype, max_count=count
                    )
                    if ret == 0 and data is not None and len(data) > 0:
                        # 将历史 K 线按时间顺序推入队列
                        k_type_str = f"K_{ktype.name}" if hasattr(ktype, 'name') else "K_60M"
                        for _, row in data.iterrows():
                            kline = row.to_dict()
                            try:
                                self._kline_queue.put_nowait((code, kline, k_type_str))
                            except queue.Full:
                                pass
                        success_count += 1
                        logger.debug(f"[FutuBridge] 预热 {code} {k_type_str}: {len(data)} 根 K 线")
                    else:
                        fail_count += 1
                        logger.debug(f"[FutuBridge] 预热 {code} {ktype} 无数据")

                    # 限速：防止触发 OpenD Limit
                    time.sleep(0.1)

            except Exception as e:
                logger.warning(f"[FutuBridge] 预热 {code} 失败: {e}")

        logger.info(f"[FutuBridge] 预热完成: 成功={success_count}, 失败={fail_count}")

    # =========================================================================
    #  重连管理
    # =========================================================================

    def reconnect(self, subscribe_callback: Callable) -> None:
        """
        断线重连 (优化版 - 指数退避)

        优化逻辑:
        1. 先测试现有连接是否可用
        2. 仅在网络真正断开时重建
        3. 重新订阅而非重新创建连接

        Args:
            subscribe_callback: 重新订阅的回调函数 (由调用方注入)
        """
        if not self._reconnect_lock.acquire(blocking=False):
            logger.warning("[FutuBridge] 已有重连任务进行中，跳过")
            return

        try:
            logger.info("[FutuBridge] 检测到行情断线，开始自动重连...")

            # 关闭旧连接 (强制释放 Futu SDK 内部 socket)
            try:
                if self._quote_ctx is not None:
                    self._quote_ctx.close()
            except Exception:
                pass
            finally:
                self._quote_ctx = None

            # 等待 2 秒让 OpenD 清理旧会话
            time.sleep(2)

            # 重建连接 → 重订阅 → 重设 handler (指数退避，最多 5 次)
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    # 调用方注入的订阅回调
                    subscribe_callback()
                    self._last_quote_time = time.time()
                    logger.info(f"[FutuBridge] 第 {attempt} 次重连成功，行情已恢复")
                    return
                except Exception as e:
                    logger.error(f"[FutuBridge] 第 {attempt} 次重连失败: {e}")
                    # 清除上次失败的 ctx，下次重建
                    try:
                        if self._quote_ctx is not None:
                            self._quote_ctx.close()
                    except Exception:
                        pass
                    self._quote_ctx = None
                    time.sleep(min(5 * attempt, 30))  # 最长等 30 秒

            logger.error("[FutuBridge] 达到最大重试次数，行情断线未恢复，请人工检查")
        finally:
            self._reconnect_lock.release()

    def check_heartbeat(self) -> bool:
        """
        检查心跳是否超时

        Returns:
            True=心跳正常, False=超时 (需要重连)
        """
        elapsed = time.time() - self._last_quote_time
        if elapsed > self._heartbeat_timeout:
            logger.warning(
                f"[FutuBridge] {elapsed:.0f}s 未收到行情数据，疑似断线"
            )
            return False
        return True

    def update_heartbeat(self) -> None:
        """更新心跳时间戳 (从 Worker 线程调用)"""
        self._last_quote_time = time.time()

    # =========================================================================
    #  状态查询
    # =========================================================================

    def is_subscribed(self) -> bool:
        """是否已订阅"""
        return self._subscribed

    def get_quote_ctx(self) -> Any:
        """获取 SDK 连接上下文 (供外部调用，如查询数据)"""
        return self._quote_ctx

    def get_stats(self) -> dict:
        """获取连接统计信息"""
        return {
            "connected": self._quote_ctx is not None,
            "subscribed": self._subscribed,
            "last_quote_time": self._last_quote_time,
            "heartbeat_timeout": self._heartbeat_timeout,
            "quote_queue_size": self._quote_queue.qsize() if self._quote_queue else 0,
            "kline_queue_size": self._kline_queue.qsize() if self._kline_queue else 0,
        }

    # =========================================================================
    #  内部状态 (从 DataSubscriber 委托)
    # =========================================================================

    @property
    def _latest_quotes(self) -> dict:
        """最新报价缓存 (委托给 MarketDataStore)"""
        # 懒加载: 从调用方获取 MarketDataStore 实例
        if not hasattr(self, '_market_data_store'):
            return {}
        return self._market_data_store._latest_quotes

    def set_market_data_store(self, store) -> None:
        """注入 MarketDataStore (由 DataSubscriber 调用)"""
        self._market_data_store = store
