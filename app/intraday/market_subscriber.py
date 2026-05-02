"""
行情订阅器 — Futu Push 行情处理

从 data_subscriber.py 拆分出来，职责：
1. 管理 Futu 行情连接（QuoteContext）
2. 处理实时行情推送（QuoteHandler）
3. 处理 K 线数据推送（KlineHandler）
4. 订阅/退订管理

不处理交易信号，不执行交易，只负责行情数据的接收和分发。
"""

import logging
import time
import threading
import queue
from typing import Optional, Dict, Any, List, Callable
from collections import defaultdict

from app.core.config_loader import cfg
from app.core.trading_env import get_trading_env

logger = logging.getLogger("ftseek.intraday.market_subscriber")


class MarketSubscriber:
    """
    行情订阅器
    
    职责：管理 Futu 行情连接，接收并分发实时数据。
    通过回调函数将数据传递给外部处理者。
    """
    
    def __init__(self):
        self._quote_ctx = None
        self._running = False
        self._subscribed = False
        
        # 行情心跳
        self._last_quote_time: float = time.time()
        self._reconnect_lock = threading.Lock()
        
        # 异步处理队列
        self._quote_queue: queue.Queue = queue.Queue(maxsize=5000)
        self._kline_queue: queue.Queue = queue.Queue(maxsize=10000)
        self._quote_worker: Optional[threading.Thread] = None
        self._kline_worker: Optional[threading.Thread] = None
        self._worker_running = False
        
        # K 线缓存 (P1 修复 2026-04-29: 使用 deque 限制大小，防止内存泄漏)
        from collections import deque
        self._kline_buffers: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: {k: deque(maxlen=500) for k in ["K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"]}
        )
        
        # 多周期趋势缓存 (P1 修复 2026-04-29: 限制数量防止无限增长)
        self._multi_timeframe_trends: Dict[str, Dict[str, str]] = defaultdict(
            lambda: {k: "neutral" for k in ["K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"]}
        )
        self._max_codes = 100  # 最多缓存 100 只股票的多周期趋势
        
        # 回调函数
        self._on_quote: Optional[Callable] = None
        self._on_kline: Optional[Callable] = None
    
    def set_callbacks(self, on_quote=None, on_kline=None):
        """设置数据回调"""
        self._on_quote = on_quote
        self._on_kline = on_kline
    
    # ===== 连接管理 =====
    
    def get_quote_context(self):
        """获取或创建行情上下文"""
        if self._quote_ctx is None:
            from futu import OpenQuoteContext
            host = cfg.get("futu.host", "127.0.0.1")
            port = cfg.get("futu.port", 11111)
            self._quote_ctx = OpenQuoteContext(host=host, port=port)
            logger.info("行情上下文已创建")
        return self._quote_ctx
    
    def connect(self):
        """建立行情连接"""
        try:
            ctx = self.get_quote_context()
            self._running = True
            logger.info("行情连接已建立")
            return True
        except Exception as e:
            logger.error(f"行情连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开行情连接"""
        self._running = False
        if self._quote_ctx:
            try:
                self._quote_ctx.close()
            except:
                pass
            self._quote_ctx = None
            logger.info("行情连接已断开")
    
    # ===== 订阅管理 =====
    
    def subscribe(self, codes: Optional[List[str]] = None):
        """订阅行情"""
        if codes is None:
            codes = cfg.get("trading.watch_list_hk", [])
        
        if not codes:
            logger.warning("订阅列表为空，跳过订阅")
            return False
        
        subscribe_types = cfg.get("strategy.subscribe_types", ["QUOTE", "K_1M"])
        
        try:
            ctx = self.get_quote_context()
            from futu import SubType
            
            type_map = {
                "QUOTE": SubType.QUOTE,
                "K_1M": SubType.K_1M,
                "K_3M": SubType.K_3M,
                "K_5M": SubType.K_5M,
                "K_15M": SubType.K_15M,
                "K_30M": SubType.K_30M,
                "K_60M": SubType.K_60M,
                "K_DAY": SubType.K_DAY,
            }
            
            sub_types = [type_map[t] for t in subscribe_types if t in type_map]
            
            ret, data = ctx.subscribe(codes, sub_types, subscribe_push=True)
            if ret == 0:
                self._subscribed = True
                logger.info(f"✅ 订阅成功: {len(codes)} 只股票 × {len(sub_types)} 种类型")
                return True
            else:
                logger.error(f"❌ 订阅失败: {data}")
                return False
        except Exception as e:
            logger.error(f"订阅异常: {e}")
            return False
    
    def unsubscribe_all(self):
        """退订所有行情"""
        try:
            ctx = self.get_quote_context()
            codes = cfg.get("trading.watch_list_hk", [])
            subscribe_types = cfg.get("strategy.subscribe_types", ["QUOTE", "K_1M"])
            
            from futu import SubType
            type_map = {
                "QUOTE": SubType.QUOTE, "K_1M": SubType.K_1M,
                "K_3M": SubType.K_3M, "K_5M": SubType.K_5M,
                "K_15M": SubType.K_15M, "K_30M": SubType.K_30M,
                "K_60M": SubType.K_60M, "K_DAY": SubType.K_DAY,
            }
            sub_types = [type_map[t] for t in subscribe_types if t in type_map]
            
            ctx.unsubscribe(codes, sub_types)
            self._subscribed = False
            logger.info("已退订所有行情")
        except Exception as e:
            logger.error(f"退订失败: {e}")
    
    # ===== Handler 设置 =====
    
    def setup_handlers(self):
        """设置 Futu Push Handler"""
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase
        
        ctx = self.get_quote_context()
        
        class QuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_str):
                self._parent._last_quote_time = time.time()
                # 入队，由 worker 处理
                try:
                    self._parent._quote_queue.put_nowait(rsp_str)
                except queue.Full:
                    logger.warning("行情队列已满，丢弃数据")
        
        class KlineHandler(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_str):
                try:
                    self._parent._kline_queue.put_nowait(rsp_str)
                except queue.Full:
                    logger.warning("K线队列已满，丢弃数据")
        
        # 注入 parent 引用
        QuoteHandler._parent = self
        KlineHandler._parent = self
        
        ctx.set_handler(QuoteHandler())
        ctx.set_handler(KlineHandler())
        
        logger.info("行情 Handler 已设置")
    
    # ===== Worker 线程 =====
    
    def start_workers(self):
        """启动数据处理 Worker"""
        self._worker_running = True
        
        self._quote_worker = threading.Thread(
            target=self._quote_worker_loop, daemon=True, name="quote-worker"
        )
        self._kline_worker = threading.Thread(
            target=self._kline_worker_loop, daemon=True, name="kline-worker"
        )
        
        self._quote_worker.start()
        self._kline_worker.start()
        logger.info("行情 Worker 线程已启动")
    
    def stop_workers(self):
        """停止 Worker 线程"""
        self._worker_running = False
        if self._quote_worker:
            self._quote_worker.join(timeout=5)
        if self._kline_worker:
            self._kline_worker.join(timeout=5)
        logger.info("行情 Worker 线程已停止")
    
    def _quote_worker_loop(self):
        """行情 Worker 循环 — 解析 rsp_str 后回调"""
        from futu import StockQuoteHandlerBase
        
        # 创建解析器实例
        handler = StockQuoteHandlerBase()
        
        while self._worker_running:
            try:
                rsp_str = self._quote_queue.get(timeout=1)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"行情队列异常: {e}")
                continue
            
            try:
                # 解析 rsp_str → DataFrame
                ret, data = handler.on_recv_rsp(rsp_str)
                if ret != 0:
                    continue
                
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    quote = row.to_dict()
                    if self._on_quote:
                        self._on_quote(code, quote)
            except Exception as e:
                logger.error(f"行情解析异常: {e}")

    def _kline_worker_loop(self):
        """K 线 Worker 循环 — 解析 rsp_str 后回调"""
        from futu import CurKlineHandlerBase
        
        # 创建解析器实例
        handler = CurKlineHandlerBase()
        
        while self._worker_running:
            try:
                rsp_str = self._kline_queue.get(timeout=1)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"K线队列异常: {e}")
                continue

            try:
                # 解析 rsp_str → DataFrame
                ret, data = handler.on_recv_rsp(rsp_str)
                if ret != 0:
                    continue

                for _, row in data.iterrows():
                    code = row.get("code", "")
                    kline = row.to_dict()
                    k_type = kline.get("k_type", "K_1M")
                    if self._on_kline:
                        self._on_kline(code, kline, k_type)
            except Exception as e:
                logger.error(f"K线解析异常: {e}")
    
    # ===== 重连 =====
    
    def check_and_reconnect(self):
        """检查连接状态并重连"""
        if not self._running:
            return
        
        # 30 秒无行情视为断线
        if time.time() - self._last_quote_time > 30:
            with self._reconnect_lock:
                logger.warning("行情超时，尝试重连...")
                self.disconnect()
                if self.connect():
                    self.setup_handlers()
                    # 重连后不自动重新订阅 — 由 SubscriptionManager 的自动循环在下一周期处理
                    # 这样可以保持分时段门控的一致性
                    self._subscribed = False
                    logger.info("[重连] 连接已恢复，订阅将由 SubscriptionManager 在下一周期处理")
    
    # ===== 状态查询 =====
    
    @property
    def is_connected(self) -> bool:
        return self._running and self._quote_ctx is not None
    
    @property
    def is_subscribed(self) -> bool:
        return self._subscribed
