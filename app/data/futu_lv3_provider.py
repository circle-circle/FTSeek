"""
富途 LV3 美股实时行情提供商 (Futu LV3 US Quote Provider)

Phase 1 实现: 通过 Futu OpenD 获取美股实时行情 (LV3)
- Nasdaq Basic + Nasdaq TotalView + NYSE Arcabook (推广期免费)
- 实时报价 (毫秒级延迟, 替代 Finnhub 15 分钟延迟)
- 自动订阅/取消订阅管理
- 降级到 Finnhub (LV3 不可用时)

架构:
  Futu OpenD (同一实例, 港股+美股共享)
    → OpenQuoteContext (美股专用连接)
    → subscribe(QUOTE, K_1M) for watchlist
    → get_market_snapshot() for real-time prices

降级策略:
  Level 1: Futu LV3 实时行情 (主)
  Level 2: Finnhub HTTP API (备, 15 分钟延迟)
  Level 3: Alpha Vantage (终备)

使用:
  provider = FutuLV3Provider(host="127.0.0.1", port=11111)
  quote = provider.get_quote("US.NVDA")
  context = provider.get_full_market_context("US.NVDA")
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, List

from futu import CurKlineHandlerBase

logger = logging.getLogger("ftseek_us.futu_lv3")


class FutuLV3Provider:
    """
    富途 LV3 美股实时行情提供商

    特性:
    - 共享已有 Futu OpenD 实例 (港股+美股)
    - 实时报价 (QUOTE) + 1分钟K线 (K_1M)
    - 自动订阅管理 (watchlist 自动订阅/取消)
    - 连接健康监控 + 自动重连
    - 降级到 Finnhub (当 Futu 不可用时)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        fallback_provider=None,
        cache_ttl_seconds: int = 5,
    ):
        """
        初始化富途 LV3 提供商

        Args:
            host: Futu OpenD 主机地址
            port: Futu OpenD 端口
            fallback_provider: 降级数据源 (FinnhubProvider 实例)
            cache_ttl_seconds: 缓存 TTL (秒), 默认 5 秒 (实时行情)
        """
        self.host = host
        self.port = port
        self.fallback = fallback_provider
        self.cache_ttl = cache_ttl_seconds
        self.enabled = False

        # 连接状态
        self._quote_ctx = None
        self._connected = False
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._cache_time: Dict[str, float] = {}

        # 健康统计
        self._success_count = 0
        self._fail_count = 0
        self._last_error: Optional[str] = None
        self._last_connect_time: Optional[float] = None

        # 订阅状态
        self._subscribed_symbols: set = set()

        # Push 行情缓存 (由 Handler 异步更新，扫描循环主动读取)
        self._push_quote_cache: Dict[str, Any] = {}  # symbol -> quote dict
        self._push_kline_cache: Dict[str, Any] = {}   # symbol -> latest kline
        self._push_ticker_cache: Dict[str, List] = {} # symbol -> recent tickers (ring buffer)
        self._push_orderbook_cache: Dict[str, Any] = {}  # symbol -> latest orderbook
        self._push_cache_lock = threading.Lock()
        self._kline_handler = None
        self._push_kline_cache_lock = threading.Lock()

        self._push_quote_count: int = 0  # 统计接收到的 Push 报价数量
        self._push_ticker_count: int = 0
        self._push_orderbook_count: int = 0

        # 延迟初始化 (首次调用时连接)
        self._initialized = False

        # K线请求限速: Futu OpenD 硬限制 60次/30秒 (2026-05-06 优化)
        self._kline_req_times: list = []  # 最近30秒内的请求时间戳
        self._kline_req_lock = threading.Lock()
        self._kline_max_per_30s = 60  # Futu 硬限制，不可突破
        
        # P0 修复: K线数据持久化缓存 + TTL，避免重复请求消耗配额
        self._kline_data_cache: dict = {}       # symbol -> {"data": [...], "ts": time.time()}
        self._kline_cache_lock = threading.Lock()
        self._kline_cache_ttl = 300             # 缓存有效期5分钟 (1分钟K线，减少配额消耗)
        self._kline_symbol_cooldown: dict = {}  # symbol -> last_request_time (防同一标的重复请求)
        self._kline_symbol_cooldown_sec = 30    # 同一标的最小请求间隔30秒 (10→30 降低配额压力)

    def _is_us_market_open(self) -> bool:
        """判断当前是否为美股交易时段 (使用美东时间)

        美股交易: 美东 09:30-16:00，周一至周五
        修复 (2026-05-09): 原用北京时间判断，周六凌晨 0-4 点被错误拦截，
        但此时对应美东周五下午，仍在交易时段内。
        """
        import pytz
        from datetime import datetime

        ET = pytz.timezone("America/New_York")
        now_et = datetime.now(ET)
        hour = now_et.hour
        minute = now_et.minute
        weekday = now_et.weekday()

        # 周一至周五
        if weekday >= 5:
            return False

        # 美东 09:30-16:00 (570-960 分钟)
        t = hour * 60 + minute
        return 570 <= t < 960

    def _ensure_connected(self) -> bool:
        """确保已连接到 Futu OpenD

        非美股交易时段拒绝连接，不消耗 OpenD 槽位。
        """
        if not self._is_us_market_open():
            return False

        if self._connected and self._quote_ctx is not None:
            return True

        with self._lock:
            if not self._is_us_market_open():
                return False
            if self._connected and self._quote_ctx is not None:
                return True

            try:
                os_env_set = False
                if "FUTU_LOG_LEVEL" not in os.environ:
                    os.environ["FUTU_LOG_LEVEL"] = "ERROR"
                    os.environ["FUTU_LOG_TO_FILE"] = "0"
                    os_env_set = True

                from futu import OpenQuoteContext, SysConfig, SubType

                # 禁用 Futu SDK 日志
                futu_logger = logging.getLogger("futu")
                futu_logger.setLevel(logging.CRITICAL)
                futu_logger.handlers = []
                futu_logger.propagate = False

                SysConfig.enable_proto_encrypt(False)

                logger.info(f"[FutuLV3] 正在连接 OpenD {self.host}:{self.port}...")
                self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

                self._connected = True
                self._last_connect_time = time.time()
                self.enabled = True

                # 注册轻量 Push Handler (报价 + K 线累积)
                self._setup_push_handlers()

                # 验证 LV3 权限 (获取美股快照)
                test_ret, test_data = self._quote_ctx.get_market_snapshot(["US.AAPL"])
                if test_ret == 0 and test_data is not None and len(test_data) > 0:
                    price = test_data.iloc[0].get("last_price", 0)
                    logger.info(
                        f"[FutuLV3] ✅ 连接成功 | LV3 行情可用 | "
                        f"US.AAPL=${price:.2f} | 延迟 < 1s"
                    )
                else:
                    logger.warning(
                        f"[FutuLV3] ⚠️ 连接成功但 LV3 快照获取失败 (ret={test_ret}), "
                        f"可能无美股行情权限, 将使用降级模式"
                    )

                return True

            except ImportError:
                logger.error("[FutuLV3] ❌ futu SDK 未安装, 使用降级模式")
                self._last_error = "futu SDK not installed"
                return False
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                self._fail_count += 1
                logger.error(f"[FutuLV3] ❌ 连接失败: {e}, 使用降级模式")
                return False

    def _to_futu_symbol(self, symbol: str) -> str:
        """US.AAPL → US.AAPL (Futu 格式), 兼容无 US. 前缀"""
        if symbol.startswith("US."):
            return symbol
        return f"US.{symbol}"

    def _strip_prefix(self, symbol: str) -> str:
        """US.AAPL → AAPL"""
        return symbol.replace("US.", "")

    def _setup_push_handlers(self):
        """
        设置轻量 Push Handler (2026-04-23 新增)

        Handler 只做一件事：收到推送后更新本地缓存。
        不做任何策略计算/交易逻辑，确保回调线程毫秒级返回。
        """
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase, TickerHandlerBase, OrderBookHandlerBase

        provider = self  # 闭包引用

        class PushQuoteHandler(StockQuoteHandlerBase):
            """轻量报价 Handler — 只更新缓存"""
            def on_recv_rsp(self, rsp_pb):
                try:
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == 0 and data is not None and len(data) > 0:
                        updated = 0
                        with provider._push_cache_lock:
                            for _, row in data.iterrows():
                                sym = row.get("code", "")
                                provider._push_quote_cache[sym] = {
                                    "price": row.get("last_price", 0),
                                    "open": row.get("open_price", 0),
                                    "high": row.get("high_price", 0),
                                    "low": row.get("low_price", 0),
                                    "prev_close": row.get("prev_close_price", 0),
                                    "volume": row.get("volume", 0),
                                    "turnover": row.get("turnover", 0),
                                    "bid": row.get("bid_price", 0),
                                    "ask": row.get("ask_price", 0),
                                    "bid_vol": row.get("bid_vol", 0),
                                    "ask_vol": row.get("ask_vol", 0),
                                    "timestamp": time.time(),
                                }
                                updated += 1
                        if updated > 0:
                            provider._push_quote_count += updated
                            if provider._push_quote_count <= len(data):  # 首次接收
                                syms = data["code"].tolist()[:5]
                                logger.info(f"[FutuLV3] ✅ Push 数据到达: {len(data)} 条, 首批: {syms}")
                    return ret, data
                except Exception:
                    pass
                return -1, None

        class PushKlineHandler(CurKlineHandlerBase):
            """轻量 K 线 Handler — 累积 K 线列表 (修复：之前存单条 dict 导致 get_kline 永远走 HTTP)"""
            def on_recv_rsp(self, rsp_pb):
                try:
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == 0 and data is not None and len(data) > 0:
                        with provider._push_cache_lock:
                            for _, row in data.iterrows():
                                sym = row.get("code", "")
                                kline_entry = {
                                    "time_key": row.get("time_key", ""),
                                    "open": float(row.get("open", 0)),
                                    "high": float(row.get("high", 0)),
                                    "low": float(row.get("low", 0)),
                                    "close": float(row.get("close", 0)),
                                    "volume": float(row.get("volume", 0)),
                                    "timestamp": time.time(),
                                }
                                # 累积到列表，而非覆盖单条
                                if sym not in provider._push_kline_cache:
                                    provider._push_kline_cache[sym] = []
                                cache_list = provider._push_kline_cache[sym]
                                # 如果最新 bar 时间相同，更新；否则追加
                                if cache_list and cache_list[-1].get("time_key", "") == kline_entry["time_key"]:
                                    cache_list[-1] = kline_entry
                                else:
                                    cache_list.append(kline_entry)
                                    # 保持合理大小 (最近 300 条 ≈ 5 小时 1M K 线)
                                    if len(cache_list) > 300:
                                        provider._push_kline_cache[sym] = cache_list[-300:]
                    return ret, data
                except Exception:
                    pass
                return -1, None

        class PushTickerHandler(TickerHandlerBase):
            """轻量逐笔 Handler — 累积最近 ticker 用于量能分析"""
            MAX_TICKERS_PER_SYMBOL = 500  # 最近 500 笔 ≈ 几分钟高频数据

            def on_recv_rsp(self, rsp_pb):
                try:
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == 0 and data is not None and len(data) > 0:
                        with provider._push_cache_lock:
                            for _, row in data.iterrows():
                                sym = row.get("code", "")
                                # 方向映射: ticker_direction 1=BUY 2=SELL 4=NEUTRAL
                                raw_dir = row.get("ticker_direction", 4)
                                if raw_dir == 1:
                                    direction = "BUY"
                                elif raw_dir == 2:
                                    direction = "SELL"
                                else:
                                    direction = "NEUTRAL"
                                ticker_entry = {
                                    "price": float(row.get("price", 0)),
                                    "volume": float(row.get("volume", 0)),
                                    "turnover": float(row.get("turnover", 0)),
                                    "direction": direction,
                                    "time": row.get("time", ""),
                                    "sequence": row.get("sequence", 0),
                                }
                                if sym not in provider._push_ticker_cache:
                                    provider._push_ticker_cache[sym] = []
                                buf = provider._push_ticker_cache[sym]
                                buf.append(ticker_entry)
                                # Ring buffer: 只保留最近 MAX_TICKERS_PER_SYMBOL 条
                                if len(buf) > self.MAX_TICKERS_PER_SYMBOL:
                                    provider._push_ticker_cache[sym] = buf[-self.MAX_TICKERS_PER_SYMBOL:]
                        provider._push_ticker_count += len(data)
                        if provider._push_ticker_count <= len(data):  # 首次接收
                            syms = data["code"].tolist()[:5]
                            logger.info(f"[FutuLV3] ✅ Ticker 数据到达: {len(data)} 笔, 首批: {syms}")
                    return ret, data
                except Exception:
                    pass
                return -1, None

        class PushOrderBookHandler(OrderBookHandlerBase):
            """轻量买卖盘 Handler — 更新最新 orderbook 快照"""

            def on_recv_rsp(self, rsp_pb):
                try:
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret == 0 and data is not None and not data.empty:
                        with provider._push_cache_lock:
                            # data is DataFrame with columns: code, BidPrice, BidVol,
                            # AskPrice, AskVol, BidNum, AskNum, BidOrderNum, AskOrderNum
                            # (Futu SDK OrderBook push format)
                            for _, row in data.iterrows():
                                sym = row.get("code", "")
                                provider._push_orderbook_cache[sym] = {
                                    "BidPrice": float(row.get("BidPrice", 0)),
                                    "BidVol": float(row.get("BidVol", 0)),
                                    "AskPrice": float(row.get("AskPrice", 0)),
                                    "AskVol": float(row.get("AskVol", 0)),
                                    "BidNum": float(row.get("BidNum", 0)),
                                    "AskNum": float(row.get("AskNum", 0)),
                                    "BidOrderNum": float(row.get("BidOrderNum", 0)),
                                    "AskOrderNum": float(row.get("AskOrderNum", 0)),
                                    "timestamp": time.time(),
                                }
                        provider._push_orderbook_count += 1
                        if provider._push_orderbook_count <= 1:  # 首次接收
                            syms = data["code"].tolist()[:5]
                            logger.info(f"[FutuLV3] ✅ OrderBook 数据到达: 首批: {syms}")
                    return ret, data
                except Exception:
                    pass
                return -1, None

        self._push_quote_handler = PushQuoteHandler()
        self._push_kline_handler = PushKlineHandler()
        self._push_ticker_handler = PushTickerHandler()
        self._push_orderbook_handler = PushOrderBookHandler()
        self._quote_ctx.set_handler(self._push_quote_handler)
        self._quote_ctx.set_handler(self._push_kline_handler)
        self._quote_ctx.set_handler(self._push_ticker_handler)
        self._quote_ctx.set_handler(self._push_orderbook_handler)
        logger.info("[FutuLV3] ✅ Push Handler 已注册 (Quote+Kline+Ticker+OrderBook)")

    def get_push_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从 Push 缓存获取实时报价 (2026-04-23 新增)

        由 Push Handler 异步更新，延迟 <100ms。
        如果缓存过期或不存在，返回 None (调用者应 fallback 到 snapshot)。
        """
        futu_symbol = self._to_futu_symbol(symbol)
        with self._push_cache_lock:
            cached = self._push_quote_cache.get(futu_symbol)
            if cached and (time.time() - cached.get("timestamp", 0)) < self.cache_ttl:
                return cached
        return None

    def get_push_tickers(self, symbol: str) -> Optional[List[Dict]]:
        """
        从 Push 缓存获取最近逐笔成交 (2026-05-20 新增)

        由 PushTickerHandler 异步更新，实时推送延迟 <100ms。
        返回最近 500 笔逐笔数据，供量能分析因子使用。
        """
        futu_symbol = self._to_futu_symbol(symbol)
        with self._push_cache_lock:
            cached = self._push_ticker_cache.get(futu_symbol)
            if cached:
                return list(cached)  # 返回副本防止外部修改
        return None

    def get_push_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从 Push 缓存获取最新买卖盘 (2026-05-20 新增)

        由 PushOrderBookHandler 异步更新，实时推送延迟 <100ms。
        返回最新一档 + 订单数信息，供深度分析因子使用。
        """
        futu_symbol = self._to_futu_symbol(symbol)
        with self._push_cache_lock:
            cached = self._push_orderbook_cache.get(futu_symbol)
            if cached and (time.time() - cached.get("timestamp", 0)) < self.cache_ttl:
                return dict(cached)  # 返回副本
        return None

    def subscribe(self, symbols: List[str], sub_types: List[str] = None) -> bool:
        """
        订阅美股实时行情 (Push 模式)

        Args:
            symbols: 股票代码列表 (US.AAPL 格式)
            sub_types: 订阅类型列表 (默认 ['QUOTE', 'K_1M'])

        Returns:
            是否成功
        """
        if not self._ensure_connected():
            return False

        futu_symbols = [self._to_futu_symbol(s) for s in symbols]
        new_symbols = [s for s in futu_symbols if s not in self._subscribed_symbols]

        if not new_symbols:
            logger.debug(f"[FutuLV3] 已订阅 {len(futu_symbols)} 只美股, 无需重复订阅")
            return True

        try:
            from futu import SubType

            # 默认订阅类型 (2026-05-20 增强: 新增 K_15M/K_30M 多时间框架对齐)
            if sub_types is None:
                sub_types = ['QUOTE', 'K_1M', 'K_15M', 'K_30M']

            sub_type_map = {
                'QUOTE': SubType.QUOTE,
                'K_1M': SubType.K_1M,
                'K_5M': SubType.K_5M,
                'K_15M': SubType.K_15M,
                'K_30M': SubType.K_30M,
                'K_60M': SubType.K_60M,
                'K_DAY': SubType.K_DAY,
                'ORDER_BOOK': SubType.ORDER_BOOK,  # 订单流深度 (Push Handler 已注册)
                'TICKER': SubType.TICKER,          # 逐笔成交 (Push Handler 已注册)
            }

            for st_name in sub_types:
                sub_type = sub_type_map.get(st_name)
                if sub_type is None:
                    logger.warning(f"[FutuLV3] 未知订阅类型: {st_name}")
                    continue
                ret, data = self._quote_ctx.subscribe(new_symbols, [sub_type], subscribe_push=True)
                if ret != 0:
                    logger.warning(
                        f"[FutuLV3] 订阅 {st_name} 部分失败: {data}, "
                        f"symbols={new_symbols}"
                    )
                else:
                    logger.info(
                        f"[FutuLV3] ✅ 已订阅 {len(new_symbols)} 只美股 {st_name}: "
                        f"{[self._strip_prefix(s) for s in new_symbols]}"
                    )

            self._subscribed_symbols.update(new_symbols)
            logger.info(
                f"[FutuLV3] 总订阅: {len(self._subscribed_symbols)} 只美股"
            )
            return True

        except Exception as e:
            logger.error(f"[FutuLV3] 订阅失败: {e}")
            return False

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时报价 (LV3)

        Args:
            symbol: 股票代码 (US.AAPL 格式)

        Returns:
            {
                "price": float,          # 最新价
                "open": float,           # 开盘价
                "high": float,           # 最高价
                "low": float,            # 最低价
                "prev_close": float,     # 昨收价
                "volume": float,         # 成交量
                "turnover": float,       # 成交额
                "change": float,         # 涨跌额
                "change_percent": float, # 涨跌幅 (%)
                "bid": float,            # 买价
                "ask": float,            # 卖价
                "bid_vol": float,        # 买量
                "ask_vol": float,        # 卖量
                "data_source": str,      # "futu_lv3" | "finnhub" | "alphavantage"
                "timestamp": float,      # Unix 时间戳
            }
        """
        futu_symbol = self._to_futu_symbol(symbol)

        # 1. Push 缓存检查 (实时推送，延迟 <100ms)
        push_data = self.get_push_quote(symbol)
        if push_data:
            push_data["data_source"] = "futu_lv3_push"
            push_data["symbol"] = symbol
            return push_data

        # 2. 旧缓存检查 (快照模式 fallback)
        cache_key = f"quote:{futu_symbol}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        # 3. 尝试 Futu LV3 快照
        if self._ensure_connected():
            try:
                ret, data = self._quote_ctx.get_market_snapshot([futu_symbol])
                if ret == 0 and data is not None and len(data) > 0:
                    row = data.iloc[0]
                    last_price = row.get("last_price", 0)

                    if last_price > 0:
                        prev_close = row.get("prev_close_price", last_price)
                        open_price = row.get("open_price", last_price)
                        high = row.get("high_price", last_price)
                        low = row.get("low_price", last_price)
                        volume = row.get("volume", 0)
                        turnover = row.get("turnover", 0)
                        bid = row.get("bid_price", 0)
                        ask = row.get("ask_price", 0)
                        bid_vol = row.get("bid_vol", 0)
                        ask_vol = row.get("ask_vol", 0)

                        change = last_price - prev_close
                        change_pct = (change / prev_close * 100) if prev_close > 0 else 0

                        result = {
                            "price": float(last_price),
                            "open": float(open_price),
                            "high": float(high),
                            "low": float(low),
                            "prev_close": float(prev_close),
                            "volume": float(volume),
                            "turnover": float(turnover),
                            "change": float(change),
                            "change_percent": float(change_pct),
                            "bid": float(bid) if bid > 0 else float(last_price),
                            "ask": float(ask) if ask > 0 else float(last_price),
                            "bid_vol": float(bid_vol),
                            "ask_vol": float(ask_vol),
                            "data_source": "futu_lv3",
                            "timestamp": time.time(),
                            "delay_ms": "<10",  # LV3 延迟 < 10ms
                        }

                        self._cache[cache_key] = result
                        self._cache_time[cache_key] = time.time()
                        self._success_count += 1

                        return result
                    else:
                        logger.warning(
                            f"[FutuLV3] {symbol} 快照返回 price=0, 可能非交易时段"
                        )

            except Exception as e:
                self._fail_count += 1
                self._last_error = str(e)
                logger.warning(f"[FutuLV3] 获取 {symbol} 快照失败: {e}, 尝试降级")

        # 降级: 使用 fallback provider
        if self.fallback:
            try:
                quote = self.fallback.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    quote["data_source"] = "finnhub_fallback"
                    quote["timestamp"] = time.time()
                    self._cache[cache_key] = quote
                    self._cache_time[cache_key] = time.time()
                    logger.info(
                        f"[FutuLV3] 🔄 降级: {symbol} 从 Finnhub 获取报价 "
                        f"${quote['price']:.2f}"
                    )
                    return quote
            except Exception as e:
                logger.warning(f"[FutuLV3] Finnhub 降级也失败: {e}")

        logger.error(f"[FutuLV3] ❌ {symbol} 所有数据源均失败")
        return None

    def get_full_market_context(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取完整市场上下文 (兼容 MarketDataRouter 接口)

        Args:
            symbol: 股票代码 (US.AAPL 格式)

        Returns:
            市场上下文字典
        """
        quote = self.get_quote(symbol)
        if not quote or quote.get("price", 0) <= 0:
            return None

        # 构建与 MarketDataRouter 兼容的上下文
        context = {
            "price": quote["price"],
            "open": quote.get("open", quote["price"]),
            "high": quote.get("high", quote["price"]),
            "low": quote.get("low", quote["price"]),
            "prev_close": quote.get("prev_close", quote["price"]),
            "volume": quote.get("volume", 0),
            "change_percent": quote.get("change_percent", 0),
            "change": quote.get("change", 0),
            "bid": quote.get("bid", quote["price"]),
            "ask": quote.get("ask", quote["price"]),
            "data_source": quote.get("data_source", "futu_lv3"),
            "timestamp": quote.get("timestamp", time.time()),
        }

        return context

    def get_batch_quote(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取报价 (2026-04-23 升级: Push 缓存优先)

        策略:
        1. 先从 Push 缓存读取所有有实时数据的标的
        2. 仅对缺失/过期的标的调用快照
        3. 合并返回

        Args:
            symbols: 股票代码列表 (US.AAPL 格式)

        Returns:
            {symbol: quote_dict}
        """
        results = {}
        missing = []

        # 1. Push 缓存检查 (实时推送)
        for sym in symbols:
            push_data = self.get_push_quote(sym)
            if push_data:
                futu_sym = self._to_futu_symbol(sym)
                push_data["data_source"] = "futu_lv3_push"
                push_data["symbol"] = sym
                results[futu_sym] = push_data
            else:
                missing.append(self._to_futu_symbol(sym))

        if not missing:
            return results  # 全部从 Push 缓存获取

        # 2. 对缺失的标的调用快照
        if not self._ensure_connected():
            return results

        try:
            ret, data = self._quote_ctx.get_market_snapshot(missing)
            snapshot_results = {}

            if ret == 0 and data is not None and len(data) > 0:
                for _, row in data.iterrows():
                    symbol_code = row.get("code", "")
                    last_price = row.get("last_price", 0)

                    if last_price > 0:
                        prev_close = row.get("prev_close_price", last_price)
                        change_pct = (
                            (last_price - prev_close) / prev_close * 100
                            if prev_close > 0 else 0
                        )

                        snapshot_results[symbol_code] = {
                            "price": float(last_price),
                            "open": float(row.get("open_price", last_price)),
                            "high": float(row.get("high_price", last_price)),
                            "low": float(row.get("low_price", last_price)),
                            "prev_close": float(prev_close),
                            "volume": float(row.get("volume", 0)),
                            "turnover": float(row.get("turnover", 0)),
                            "change_percent": float(change_pct),
                            "data_source": "futu_lv3",
                            "timestamp": time.time(),
                        }

                self._success_count += 1
                logger.debug(
                    f"[FutuLV3] 批量快照: {len(snapshot_results)}/{len(missing)} 成功, "
                    f"Push 缓存: {len(results)} 只"
                )
                results.update(snapshot_results)
                return results
            else:
                logger.warning(f"[FutuLV3] 批量快照失败: ret={ret}")
                return results  # 返回已有的 Push 缓存数据

        except Exception as e:
            self._fail_count += 1
            self._last_error = str(e)
            logger.error(f"[FutuLV3] 批量快照异常: {e}")

        # 降级: 逐个获取 (get_quote 内部会自动检查 Push 缓存)
        for sym in symbols:
            q = self.get_quote(sym)
            if q:
                results[self._to_futu_symbol(sym)] = q
        return results

    def _wait_kline_quota(self, symbol: str = None) -> bool:
        """
        非阻塞 K 线配额检查 (P0 修复 - 2026-05-06)
        
        双重门控: 1) 全局配额 60次/30s  2) 单标的冷却 10s
        调用方应处理 False 返回值（使用缓存数据）。
        """
        now = time.time()
        
        # ① 单标的冷却: 同一标的10秒内不重复请求
        if symbol:
            with self._kline_req_lock:
                last = self._kline_symbol_cooldown.get(symbol, 0)
                if now - last < self._kline_symbol_cooldown_sec:
                    return False  # 冷却中
        
        # ② 全局配额检查
        with self._kline_req_lock:
            self._kline_req_times = [t for t in self._kline_req_times if t > (now - 30.0)]
            if len(self._kline_req_times) >= self._kline_max_per_30s:
                return False  # 配额已满
            
            # ③ 同时记录
            self._kline_req_times.append(now)
            if symbol:
                self._kline_symbol_cooldown[symbol] = now
                # 清理过期的冷却记录
                expired = [s for s, t in self._kline_symbol_cooldown.items() if now - t > 60]
                for s in expired:
                    del self._kline_symbol_cooldown[s]
            return True

    def _get_cached_kline(self, symbol: str) -> Optional[list]:
        """从持久化缓存获取K线数据 (不消耗配额)"""
        futu_sym = self._to_futu_symbol(symbol)
        now = time.time()
        with self._kline_cache_lock:
            entry = self._kline_data_cache.get(futu_sym)
            if entry and (now - entry["ts"]) < self._kline_cache_ttl:
                return entry["data"]
        return None

    def _set_cached_kline(self, symbol: str, data: list):
        """将K线数据写入持久化缓存"""
        futu_sym = self._to_futu_symbol(symbol)
        with self._kline_cache_lock:
            self._kline_data_cache[futu_sym] = {"data": data, "ts": time.time()}

    def get_kline(self, symbol: str, ktype: str = "K_1M", count: int = 100) -> Optional[List[Dict]]:
        """
        获取 K 线数据 (P0 修复 - 2026-05-06)
        
        三级降级:
          1. 持久化缓存 (300s TTL, 不消耗配额)
          2. Push 缓存 (由 PushKlineHandler 累积 K 线列表，实时更新)
          3. HTTP 请求 (配额允许时)
        """
        if not self._ensure_connected():
            return None

        futu_symbol = self._to_futu_symbol(symbol)

        # ① 优先读持久化缓存 (60s TTL)
        cached = self._get_cached_kline(symbol)
        if cached and len(cached) >= min(count, 10):
            return cached[-count:]

        # ② 读 Push 缓存 (由 Handler 实时更新)
        with self._push_kline_cache_lock:
            if futu_symbol in self._push_kline_cache:
                push_data = self._push_kline_cache[futu_symbol]
                if len(push_data) >= min(count, 10):
                    # 同时写入持久化缓存
                    self._set_cached_kline(symbol, push_data[-count:])
                    return push_data[-count:]

        # ③ 配额允许才发 HTTP 请求
        if not self._wait_kline_quota(symbol):
            # 配额满/冷却中 → 返回已有缓存(哪怕不足)
            all_cached = self._get_cached_kline(symbol)
            if all_cached:
                return all_cached[-count:]
            return None

        try:
            from futu import KLType

            kl_type_map = {
                "K_1M": KLType.K_1M,
                "K_3M": KLType.K_3M,
                "K_5M": KLType.K_5M,
                "K_15M": KLType.K_15M,
                "K_30M": KLType.K_30M,
                "K_60M": KLType.K_60M,
                "K_DAY": KLType.K_DAY,
            }

            kl_type = kl_type_map.get(ktype, KLType.K_1M)

            ret, data, _ = self._quote_ctx.request_history_kline(
                code=futu_symbol, ktype=kl_type, max_count=count
            )

            if ret == 0 and data is not None and len(data) > 0:
                klines = []
                for _, row in data.iterrows():
                    klines.append({
                        "time": str(row.get("time_key", "")),
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0)),
                        "turnover": float(row.get("turnover", 0)),
                    })
                # 更新所有缓存
                with self._push_kline_cache_lock:
                    self._push_kline_cache[futu_symbol] = klines
                self._set_cached_kline(symbol, klines)
                return klines
            else:
                logger.debug(f"[FutuLV3] {symbol} K 线获取失败: ret={ret}")
                return self._get_cached_kline(symbol)  # 降级返回缓存

        except Exception as e:
            logger.debug(f"[FutuLV3] {symbol} K 线异常: {e}")
            return self._get_cached_kline(symbol)  # 降级返回缓存

    @property
    def is_connected(self) -> bool:
        """连接状态 (2026-04-20 新增)"""
        return self._connected and self._quote_ctx is not None

    def disconnect(self):
        """主动断开 Futu 连接 (2026-04-20 新增)

        港股交易时段主动断开，释放 OpenD 槽位给 FTSeek 港股系统。
        """
        with self._lock:
            if self._quote_ctx is not None:
                try:
                    self._quote_ctx.close()
                except Exception as e:
                    logger.debug(f"[FutuLV3] 断开连接异常: {e}")
                self._quote_ctx = None
            self._connected = False
            self._subscribed_symbols.clear()
            logger.info("[FutuLV3] 已断开 OpenD 连接")

    # ═══════════════════════════════════════════════════════════
    # P0 新增 (2026-05-09): 订单流深度数据 (ORDER_BOOK)
    # ═══════════════════════════════════════════════════════════

    def get_order_book(self, symbol: str) -> Optional[Dict]:
        """
        获取实时买卖盘深度数据

        Returns:
            {
                "bid_prices": [买1价, 买2价, ...],
                "bid_volumes": [买1量, 买2量, ...],
                "ask_prices": [卖1价, 卖2价, ...],
                "ask_volumes": [卖1量, 卖2量, ...],
                "bid_total_volume": 买盘总量,
                "ask_total_volume": 卖盘总量,
                "bid_ask_ratio": 买卖盘深度比,
            } or None
        """
        if not self._ensure_connected():
            return None

        try:
            from futu import OrderBookQuery
            futu_symbol = self._to_futu_symbol(symbol)

            ret, data = self._quote_ctx.get_order_book(
                futu_symbol, num=10  # 获取 10 档深度
            )

            if ret != 0 or data is None or data.empty:
                logger.debug(f"[FutuLV3] ORDER_BOOK 获取失败: {symbol}")
                return None

            row = data.iloc[0]

            # 提取买卖盘数据
            bid_prices = [row.get(f'BidPrice{i}', 0) for i in range(1, 11)]
            bid_volumes = [row.get(f'BidVol{i}', 0) for i in range(1, 11)]
            ask_prices = [row.get(f'AskPrice{i}', 0) for i in range(1, 11)]
            ask_volumes = [row.get(f'AskVol{i}', 0) for i in range(1, 11)]

            bid_total = sum(v for v in bid_volumes if v > 0)
            ask_total = sum(v for v in ask_volumes if v > 0)

            ratio = bid_total / ask_total if ask_total > 0 else 0

            return {
                "bid_prices": bid_prices,
                "bid_volumes": bid_volumes,
                "ask_prices": ask_prices,
                "ask_volumes": ask_volumes,
                "bid_total_volume": bid_total,
                "ask_total_volume": ask_total,
                "bid_ask_ratio": ratio,
            }

        except Exception as e:
            logger.debug(f"[FutuLV3] ORDER_BOOK 异常: {symbol}: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # P1 新增 (2026-05-09): 逐笔成交数据 (TICKER)
    # ═══════════════════════════════════════════════════════════

    def get_recent_tickers(self, symbol: str, count: int = 50) -> Optional[List[Dict]]:
        """
        获取最近逐笔成交数据

        Returns:
            [
                {
                    "price": 成交价,
                    "volume": 成交量,
                    "direction": "BUY"/"SELL"/"NEUTRAL",
                    "timestamp": 时间戳,
                    "turnover": 成交额,
                },
                ...
            ] or None
        """
        if not self._ensure_connected():
            return None

        try:
            from futu import SysNotifyHandler
            futu_symbol = self._to_futu_symbol(symbol)

            ret, data = self._quote_ctx.get_recent_tickers(futu_symbol, count=count)

            if ret != 0 or data is None or data.empty:
                logger.debug(f"[FutuLV3] TICKER 获取失败: {symbol}")
                return None

            tickers = []
            for _, row in data.iterrows():
                # 方向映射: 2=买入, 1=卖出, 4=中性 (富途定义)
                raw_dir = row.get('Direction', 4)
                if raw_dir == 2:
                    direction = "BUY"
                elif raw_dir == 1:
                    direction = "SELL"
                else:
                    direction = "NEUTRAL"

                tickers.append({
                    "price": float(row.get('Price', 0)),
                    "volume": float(row.get('Volume', 0)),
                    "direction": direction,
                    "timestamp": row.get('Time', ''),
                    "turnover": float(row.get('Turnover', 0)),
                })

            return tickers

        except Exception as e:
            logger.debug(f"[FutuLV3] TICKER 异常: {symbol}: {e}")
            return None

    def get_health_status(self) -> dict:
        """获取健康状态"""
        total = self._success_count + self._fail_count
        success_rate = (
            self._success_count / total * 100 if total > 0 else 0
        )

        return {
            "provider": "futu_lv3",
            "connected": self._connected,
            "enabled": self.enabled,
            "subscribed_symbols": len(self._subscribed_symbols),
            "success_rate": f"{success_rate:.1f}%",
            "total_requests": total,
            "success_count": self._success_count,
            "fail_count": self._fail_count,
            "push_quote_count": self._push_quote_count,
            "push_ticker_count": self._push_ticker_count,
            "push_orderbook_count": self._push_orderbook_count,
            "ticker_cache_symbols": len(self._push_ticker_cache),
            "orderbook_cache_symbols": len(self._push_orderbook_cache),
            "last_error": self._last_error,
            "last_connect_time": (
                datetime.fromtimestamp(self._last_connect_time).isoformat()
                if self._last_connect_time
                else None
            ),
        }

    def _is_cache_valid(self, key: str) -> bool:
        """检查缓存是否有效"""
        if key not in self._cache:
            return False
        age = time.time() - self._cache_time.get(key, 0)
        return age < self.cache_ttl

    def close(self):
        """关闭连接"""
        with self._lock:
            if self._quote_ctx is not None:
                try:
                    # 取消所有订阅 (2026-05-20: 覆盖所有已订阅类型)
                    if self._subscribed_symbols:
                        from futu import SubType
                        symbols_list = list(self._subscribed_symbols)
                        all_sub_types = [
                            SubType.QUOTE, SubType.K_1M, SubType.K_5M,
                            SubType.K_15M, SubType.K_30M, SubType.K_60M,
                            SubType.ORDER_BOOK, SubType.TICKER,
                        ]
                        for sub_type in all_sub_types:
                            try:
                                self._quote_ctx.unsubscribe(symbols_list, [sub_type])
                            except Exception:
                                pass

                    self._quote_ctx.close()
                    self._quote_ctx = None
                    self._connected = False
                    self._subscribed_symbols.clear()
                    with self._push_cache_lock:
                        self._push_quote_cache.clear()
                        self._push_kline_cache.clear()
                        self._push_ticker_cache.clear()
                        self._push_orderbook_cache.clear()
                    logger.info("[FutuLV3] 连接已关闭")
                except Exception as e:
                    logger.warning(f"[FutuLV3] 关闭连接异常: {e}")

    def unsubscribe_all(self, symbols: List[str] = None, sub_types: List[str] = None):
        """
        取消所有美股行情订阅 (2026-04-23 新增, 2026-05-20 增强)
        用于非交易时段释放富途 OpenD 槽位。

        Args:
            symbols: 股票代码列表 (为 None 时取消所有已订阅)
            sub_types: 订阅类型列表 (默认取消所有已订阅类型)
        """
        if not self._quote_ctx or not self._subscribed_symbols:
            logger.debug("[FutuLV3] 无活跃订阅，无需退订")
            return

        from futu import SubType

        symbols_list = symbols or list(self._subscribed_symbols)
        if sub_types is None:
            sub_types = ['QUOTE', 'K_1M', 'K_5M', 'K_15M', 'K_30M', 'K_60M', 'ORDER_BOOK', 'TICKER']

        sub_type_map = {
            'QUOTE': SubType.QUOTE,
            'K_1M': SubType.K_1M,
            'K_5M': SubType.K_5M,
            'K_15M': SubType.K_15M,
            'K_30M': SubType.K_30M,
            'K_60M': SubType.K_60M,
            'K_DAY': SubType.K_DAY,
            'ORDER_BOOK': SubType.ORDER_BOOK,
            'TICKER': SubType.TICKER,
        }

        logger.info(f"[FutuLV3] 开始退订 {len(symbols_list)} 只美股 (释放槽位)...")
        success = 0
        failed = 0

        for st_name in sub_types:
            sub_type = sub_type_map.get(st_name)
            if sub_type is None:
                continue
            try:
                ret, msg = self._quote_ctx.unsubscribe(symbols_list, [sub_type])
                if ret == 0:
                    success += 1
                else:
                    failed += 1
                    logger.warning(f"[FutuLV3] 退订 {st_name} 失败: {msg}")
            except Exception as e:
                failed += 1
                logger.warning(f"[FutuLV3] 退订 {st_name} 异常: {e}")

        self._subscribed_symbols.clear()
        with self._push_cache_lock:
            self._push_quote_cache.clear()
            self._push_kline_cache.clear()
            self._push_ticker_cache.clear()
            self._push_orderbook_cache.clear()
        logger.info(f"[FutuLV3] 退订完成: 成功={success}, 失败={failed} | 槽位已释放")


# ── 全局单例 ──────────────────────────────────────────────────────
_futu_lv3_instance: Optional[FutuLV3Provider] = None
_futu_lv3_lock = threading.Lock()


def get_futu_lv3_provider(
    host: str = "127.0.0.1",
    port: int = 11111,
    fallback_provider=None,
    cache_ttl_seconds: int = 5,
) -> FutuLV3Provider:
    """
    获取 Futu LV3 提供商单例

    Args:
        host: OpenD 主机
        port: OpenD 端口
        fallback_provider: 降级数据源
        cache_ttl_seconds: 缓存 TTL

    Returns:
        FutuLV3Provider 实例
    """
    global _futu_lv3_instance
    if _futu_lv3_instance is None:
        with _futu_lv3_lock:
            if _futu_lv3_instance is None:
                _futu_lv3_instance = FutuLV3Provider(
                    host=host,
                    port=port,
                    fallback_provider=fallback_provider,
                    cache_ttl_seconds=cache_ttl_seconds,
                )
    return _futu_lv3_instance


def reset_futu_lv3_provider():
    """重置单例 (用于测试或重新初始化)"""
    global _futu_lv3_instance
    with _futu_lv3_lock:
        if _futu_lv3_instance is not None:
            _futu_lv3_instance.close()
            _futu_lv3_instance = None
        logger.info("[FutuLV3] 单例已重置")