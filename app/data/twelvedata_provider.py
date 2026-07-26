"""
TwelveData 行情提供商

免费计划限速：8 次/分钟（每次请求需间隔 ≥ 7.5s）
优势：支持批量请求（单次拉取多个标的），历史 OHLCV + 技术指标
文档：https://twelvedata.com/docs

调用策略：
  - 批量 quote：一次最多 8 个 symbol → 消耗 1 次配额（free 计划 1 次 = 1 symbol，批量按 symbol 数计）
  - time_series：每只 1 次
  - 限速器：令牌桶，上限 8 令牌/分钟，初始 8 个令牌，令牌每 7.5s 补充 1 个
"""
import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env.us")

logger = logging.getLogger(__name__)

# 导入 API 限流监控 (在初始化时检查是否应该使用 TwelveData)
try:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from scripts.api_limit_monitor import should_use_twelvedata, record_twelvedata_call
    API_LIMIT_MONITOR_ENABLED = True
except Exception as e:
    logger.warning(f"[TwelveData] API 限流监控导入失败：{e}")
    API_LIMIT_MONITOR_ENABLED = False

# ── 常量 ─────────────────────────────────────────────────────────
BASE_URL = "https://api.twelvedata.com"
# 免费计划：8 credits/分钟；批量请求按标的数计 credits
FREE_CREDITS_PER_MINUTE = 8
REFILL_INTERVAL_SEC = 60.0 / FREE_CREDITS_PER_MINUTE   # 7.5s/credit


class _RateLimiter:
    """令牌桶限速器（线程安全）"""

    def __init__(self, capacity: int, refill_interval_sec: float):
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_interval = refill_interval_sec
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: int = 1, timeout: float = 120.0) -> bool:
        """
        消耗 n 个令牌，不足时等待直到超时。
        Returns True 成功，False 超时。
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return True
            wait = self._refill_interval * n
            if time.monotonic() + wait > deadline:
                logger.warning(f"[TwelveData] 限速等待超时 ({n} credits)")
                return False
            logger.debug(f"[TwelveData] 等待 {wait:.1f}s 获取 {n} credits")
            time.sleep(min(wait, deadline - time.monotonic()))

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed / self._refill_interval
        if new_tokens >= 1:
            self._tokens = min(self._capacity, self._tokens + new_tokens)
            self._last_refill = now


class TwelveDataProvider:
    """
    TwelveData 行情提供商（免费计划限速适配版）

    主要接口：
      get_quote(symbol)           → 实时报价（含涨跌幅）
      get_time_series(symbol)     → 日线历史 OHLCV（最近 60 条）
      get_full_market_context(symbol)  → 综合行情上下文（供投票用）
      get_batch_quotes(symbols)   → 批量报价（需 len(symbols) credits）
    
    属性:
      enabled: 服务是否启用 (API Key 存在)
    """
    
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
        """初始化 TwelveData 提供商
        
        Args:
            api_key: TwelveData API Key
            cache_ttl_seconds: 默认缓存TTL (秒), 默认300秒 (5分钟)
        """
        self.api_key = api_key or os.getenv("TWELVEDATA_API_KEY", "")
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self.enabled = bool(self.api_key)
        
        # 限速器
        self._limiter = _RateLimiter(
            capacity=FREE_CREDITS_PER_MINUTE,
            refill_interval_sec=REFILL_INTERVAL_SEC
        )
        # 2026-06-30: 429 熔断时间戳
        self._rate_limited_until: float = 0.0
        
        # 缓存
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_time: Dict[str, datetime] = {}
        
        # HTTP Session
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "FTSeek-US/1.0 (Trading System)",
        })
        
        if self.enabled:
            logger.info(f"[TwelveData] 初始化完成 | API Key=✅ | Cache TTL={cache_ttl_seconds}s")
        else:
            logger.warning("[TwelveData] API Key 未配置，服务已禁用")

    # ── 内部工具 ──────────────────────────────────────────────────

    def _calculate_rsi(self, prices: 'np.ndarray', period: int = 14) -> float:
        """
        P0优化: 计算RSI (Relative Strength Index)
        
        Args:
            prices: 价格数组
            period: 周期 (默认14)
        
        Returns:
            RSI值 (0-100)
        """
        import numpy as np
        
        if len(prices) < period + 1:
            return 50.0  # 数据不足,返回中性值
        
        # 计算价格变化
        deltas = np.diff(prices)
        
        # 分离上涨和下跌
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        # 计算平均涨幅和跌幅 (使用EMA)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0  # 只涨不跌
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return float(rsi)
    
    def _calculate_macd(self, prices: 'np.ndarray', fast: int = 12, slow: int = 26, signal: int = 9):
        """
        P0优化: 计算MACD (Moving Average Convergence Divergence)
        
        Args:
            prices: 价格数组
            fast: 快速EMA周期 (默认12)
            slow: 慢速EMA周期 (默认26)
            signal: 信号线周期 (默认9)
        
        Returns:
            (macd_line, signal_line, histogram)
        """
        import numpy as np
        
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0  # 数据不足
        
        def ema(data, period):
            """计算EMA"""
            ema_values = np.zeros_like(data)
            multiplier = 2 / (period + 1)
            ema_values[period-1] = np.mean(data[:period])
            for i in range(period, len(data)):
                ema_values[i] = (data[i] - ema_values[i-1]) * multiplier + ema_values[i-1]
            return ema_values
        
        # 计算快速和慢速EMA
        ema_fast = ema(prices, fast)
        ema_slow = ema(prices, slow)
        
        # MACD线
        macd_line = ema_fast - ema_slow
        
        # 信号线 (MACD的EMA)
        # 从MACD有值的地方开始计算
        macd_valid = macd_line[slow-1:]
        signal_ema = ema(macd_valid, signal)
        
        # 最新值
        latest_macd = macd_line[-1]
        latest_signal = signal_ema[-1] if len(signal_ema) > 0 else 0.0
        latest_histogram = latest_macd - latest_signal
        
        return float(latest_macd), float(latest_signal), float(latest_histogram)

    def _is_cache_valid(self, key: str, ttl_override: Optional[int] = None) -> bool:
        if key not in self._cache:
            return False
        age = (datetime.now() - self._cache_time.get(key, datetime.min)).total_seconds()
        ttl = ttl_override if ttl_override is not None else self.cache_ttl.total_seconds()
        return age < ttl

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_time[key] = datetime.now()

    def _to_ticker(self, symbol: str) -> str:
        """US.NVDA → NVDA"""
        return symbol.replace("US.", "").replace("HK.", "")

    def _get(self, endpoint: str, params: Dict) -> Optional[Dict]:
        """发送 GET 请求，附带 API Key，返回 JSON 或 None。"""
        if not self.api_key:
            return None
        params["apikey"] = self.api_key
        # 2026-06-30: 429 熔断 — 收到429后10分钟内不再请求
        _now = time.monotonic()
        if hasattr(self, '_rate_limited_until') and _now < self._rate_limited_until:
            return None
        try:
            resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15, verify=False)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "error" or data.get("code") in (400, 401, 429):
                if data.get("code") == 429 or "429" in str(data.get("message", "")):
                    self._rate_limited_until = _now + 600  # 10分钟熔断
                    logger.warning(f"[TwelveData] 429 限流, 熔断10分钟")
                else:
                    logger.warning(f"[TwelveData] API错误: {data.get('message', data)}")
                return None
            return data
        except requests.RequestException as e:
            if "429" in str(e) or "Too Many" in str(e):
                self._rate_limited_until = _now + 600  # 10分钟熔断
                logger.warning(f"[TwelveData] 429 限流(异常), 熔断10分钟")
            else:
                logger.error(f"[TwelveData] 请求失败 {endpoint}: {e}")
            return None

    # ── 公开接口 ──────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时报价（消耗 1 credit）。
        缓存有效期内直接返回缓存，避免重复扣额。
        """
        # 检查是否应该使用 TwelveData (API 限流监控)
        if API_LIMIT_MONITOR_ENABLED and not should_use_twelvedata():
            logger.warning(f"[TwelveData] 已切换到备用数据源，跳过 {symbol}")
            return None
        
        cache_key = f"quote_{symbol}"
        if self._is_cache_valid(cache_key, ttl_override=300):  # 5分钟内复用
            return self._cache[cache_key]

        if not self._limiter.acquire(n=1):
            return None

        ticker = self._to_ticker(symbol)
        data = self._get("quote", {"symbol": ticker})
        if not data:
            return None

        # 记录 API 调用
        if API_LIMIT_MONITOR_ENABLED:
            record_twelvedata_call(1)

        try:
            price = float(data.get("close") or data.get("price") or 0)
            prev_close = float(data.get("previous_close") or price)
            change_pct = float(data.get("percent_change") or 0) / 100

            quote = {
                "symbol": symbol,
                "price": price,
                "open": float(data.get("open") or price),
                "high": float(data.get("high") or price),
                "low": float(data.get("low") or price),
                "previous_close": prev_close,
                "change_percent": change_pct,
                "volume": int(float(data.get("volume") or 0)),
                "timestamp": datetime.now().isoformat(),
                "source": "twelvedata",
            }
            self._set_cache(cache_key, quote)
            # P1 (2026-05-02): 非交易时段降级日志级别，避免 INFO 刷屏
            phase_str = "OVERNIGHT"
            try:
                from app.core.scheduler_triggers import get_market_phase, MarketPhase
                phase = get_market_phase()
                phase_str = phase.value if hasattr(phase, 'value') else str(phase)
            except Exception:
                pass
            if phase_str in ("WEEKEND", "OVERNIGHT"):
                logger.debug(f"[TwelveData] {symbol} ${price:.2f} ({change_pct:+.2%})")
            else:
                logger.info(f"[TwelveData] {symbol} ${price:.2f} ({change_pct:+.2%})")
            return quote
        except Exception as e:
            logger.error(f"[TwelveData] 解析报价失败 {symbol}: {e}")
            return None

    def get_daily_bars(self, symbol: str, count: int = 30) -> Optional[List[Dict[str, Any]]]:
        """获取日K线历史 bar 列表 (供 MarketDataRouter.get_daily_bars 的 Futu fallback).

        与 get_time_series 的区别: 后者返回处理后的指标 dict (含 ma20/atr/rsi), 消耗大;
        本方法只返回原始 bar 列表 [{time, open, high, low, close, volume}], 升序,
        time 归一化为 "YYYY-MM-DD" (兼容 ic_tracker 精确匹配 + shadow_tracker [:10]).

        Finnhub /stock/candle 在免费版返回 403, 故 TwelveData 是 Futu 失效时的有效日线 fallback.
        """
        if API_LIMIT_MONITOR_ENABLED and not should_use_twelvedata():
            logger.warning(f"[TwelveData] 已切换到备用数据源，跳过 {symbol}")
            return None
        if not self.enabled:
            return None

        cache_key = f"dailybars_{symbol}"
        if self._is_cache_valid(cache_key, ttl_override=3600):  # 日线缓存 60min
            cached = self._cache.get(cache_key)
            if cached:
                return cached[-count:] if count < len(cached) else cached

        if not self._limiter.acquire(n=1):
            return None

        ticker = self._to_ticker(symbol)
        data = self._get("time_series", {
            "symbol": ticker,
            "interval": "1day",
            "outputsize": max(count, 30),
            "order": "ASC",
            "format": "JSON",
        })
        if not data or "values" not in data:
            return None
        if API_LIMIT_MONITOR_ENABLED:
            record_twelvedata_call(1)

        try:
            bars = []
            for v in data["values"]:
                t = str(v.get("datetime", ""))
                if len(t) > 10:
                    t = t[:10]  # "2026-06-23 00:00:00" → "2026-06-23"
                bars.append({
                    "time": t,
                    "open": float(v.get("open", 0) or 0),
                    "high": float(v.get("high", 0) or 0),
                    "low": float(v.get("low", 0) or 0),
                    "close": float(v.get("close", 0) or 0),
                    "volume": float(v.get("volume", 0) or 0),
                })
            if not bars:
                return None
            self._set_cache(cache_key, bars)
            logger.debug(f"[TwelveData] {symbol} 日线 {len(bars)} bars (fallback)")
            return bars[-count:] if count < len(bars) else bars
        except Exception as e:
            logger.error(f"[TwelveData] 解析日线失败 {symbol}: {e}")
            return None

    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取报价（单次 API 调用，消耗 len(symbols) credits）。
        免费计划最多 8 个/次，超出则自动分批。
        """
        # 检查是否应该使用 TwelveData (API 限流监控)
        if API_LIMIT_MONITOR_ENABLED and not should_use_twelvedata():
            logger.warning("[TwelveData] 已切换到备用数据源，跳过批量获取")
            return {}
        
        results: Dict[str, Dict] = {}
        batch_size = FREE_CREDITS_PER_MINUTE  # 每批最多 8 个

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i: i + batch_size]

            # 检查缓存，过滤已有的
            need_fetch = []
            for sym in batch:
                cache_key = f"quote_{sym}"
                if self._is_cache_valid(cache_key, ttl_override=300):
                    results[sym] = self._cache[cache_key]
                else:
                    need_fetch.append(sym)

            if not need_fetch:
                continue

            # 批量请求消耗 len(need_fetch) credits
            if not self._limiter.acquire(n=len(need_fetch)):
                logger.warning(f"[TwelveData] 批量限速，跳过 {need_fetch}")
                continue

            tickers = ",".join(self._to_ticker(s) for s in need_fetch)
            data = self._get("quote", {"symbol": tickers})
            if not data:
                continue

            # 单个结果时 data 直接是 dict；多个时是 {ticker: dict}
            if len(need_fetch) == 1:
                parsed = {need_fetch[0]: data}
            else:
                parsed = {f"US.{k}": v for k, v in data.items() if isinstance(v, dict)}

            # 记录 API 调用
            if API_LIMIT_MONITOR_ENABLED:
                record_twelvedata_call(len(need_fetch))
            
            for sym, d in parsed.items():
                try:
                    price = float(d.get("close") or d.get("price") or 0)
                    if price <= 0:
                        continue
                    prev_close = float(d.get("previous_close") or price)
                    change_pct = float(d.get("percent_change") or 0) / 100
                    quote = {
                        "symbol": sym,
                        "price": price,
                        "open": float(d.get("open") or price),
                        "high": float(d.get("high") or price),
                        "low": float(d.get("low") or price),
                        "previous_close": prev_close,
                        "change_percent": change_pct,
                        "volume": int(float(d.get("volume") or 0)),
                        "timestamp": datetime.now().isoformat(),
                        "source": "twelvedata",
                    }
                    self._set_cache(f"quote_{sym}", quote)
                    results[sym] = quote
                except Exception as e:
                    logger.debug(f"[TwelveData] 批量解析跳过 {sym}: {e}")

        return results

    def get_time_series(self, symbol: str, outputsize: int = 60) -> Optional[Dict[str, Any]]:
        """
        获取日线历史 OHLCV（消耗 1 credit）。
        返回包含 close_prices / ma20 / ma50 / atr / volatility 的字典。
        缓存 30 分钟（日线数据变化慢）。
        """
        # 检查是否应该使用 TwelveData (API 限流监控)
        if API_LIMIT_MONITOR_ENABLED and not should_use_twelvedata():
            logger.warning(f"[TwelveData] 已切换到备用数据源，跳过 {symbol}")
            return None
        
        cache_key = f"ts_{symbol}"
        if self._is_cache_valid(cache_key, ttl_override=3600):  # 日线数据缓存 60 分钟
            return self._cache[cache_key]

        if not self._limiter.acquire(n=1):
            return None

        ticker = self._to_ticker(symbol)
        data = self._get("time_series", {
            "symbol": ticker,
            "interval": "1day",
            "outputsize": outputsize,
            "order": "ASC",   # 升序，最新在最后
        })
        if not data or "values" not in data:
            return None

        # 记录 API 调用
        if API_LIMIT_MONITOR_ENABLED:
            record_twelvedata_call(1)

        try:
            import numpy as np
            values = data["values"]
            closes = [float(v["close"]) for v in values]
            highs  = [float(v["high"])  for v in values]
            lows   = [float(v["low"])   for v in values]
            volumes = [float(v.get("volume", 0)) for v in values]

            closes_arr = np.array(closes)
            n = len(closes_arr)

            ma20 = float(closes_arr[-20:].mean()) if n >= 20 else float(closes_arr.mean())
            ma50 = float(closes_arr[-50:].mean()) if n >= 50 else float(closes_arr.mean())

            # P0优化: 添加RSI计算 (14日)
            rsi_14 = self._calculate_rsi(closes_arr, period=14)
            
            # P0优化: 添加MACD计算 (12,26,9)
            macd_line, signal_line, macd_histogram = self._calculate_macd(closes_arr)

            # ATR（Wilder 14日）
            highs_arr = np.array(highs)
            lows_arr  = np.array(lows)
            tr = np.maximum(
                highs_arr[1:] - lows_arr[1:],
                np.maximum(
                    np.abs(highs_arr[1:] - closes_arr[:-1]),
                    np.abs(lows_arr[1:]  - closes_arr[:-1]),
                )
            )
            atr = float(tr[-14:].mean()) if len(tr) >= 14 else float(tr.mean()) if len(tr) > 0 else 0.0

            current_price = closes[-1]
            avg_volume    = float(np.mean(volumes)) if volumes else 1.0
            volume_ratio  = volumes[-1] / avg_volume if avg_volume > 0 else 1.0

            result = {
                "symbol": symbol,
                "price": current_price,
                "ma20": ma20,
                "ma50": ma50,
                "rsi_14": rsi_14,  # P0优化: RSI指标
                "macd": macd_line,  # P0优化: MACD线
                "macd_signal": signal_line,  # P0优化: 信号线
                "macd_histogram": macd_histogram,  # P0优化: 柱状图
                "atr": atr,
                "volatility": atr / current_price if current_price > 0 else 0.0,
                "volume": volumes[-1],
                "volume_ratio": volume_ratio,
                "momentum": (current_price - ma20) / ma20 if ma20 > 0 else 0.0,
                "trend": "UP" if current_price > ma50 else "DOWN",
                "close_prices": closes,
                "timestamp": datetime.now().isoformat(),
                "source": "twelvedata",
            }
            self._set_cache(cache_key, result)
            logger.info(f"[TwelveData-P0] {symbol} 历史数据 {n}条 MA20={ma20:.2f} RSI={rsi_14:.1f} MACD={macd_line:.2f} ATR={atr:.2f}")
            return result
        except Exception as e:
            logger.error(f"[TwelveData] 解析时间序列失败 {symbol}: {e}")
            return None

    def get_full_market_context(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        综合行情上下文（供 VotingCoordinator 使用）。
        先取 time_series（含技术指标），再用 quote 更新最新价。
        两次调用共消耗 2 credits（如缓存命中则 0 次）。
        """
        ts = self.get_time_series(symbol)
        if not ts:
            return None

        quote = self.get_quote(symbol)
        price = (quote or {}).get("price") or ts["price"]
        change_pct = (quote or {}).get("change_percent", 0.0)

        context = {
            **ts,
            "price": price,
            "change_percent": change_pct,
            "news_sentiment": 0.0,        # 由 FinnhubProvider 注入
            "vix": 20.0,                  # 由 FinnhubProvider 注入
            "spy_price": 0.0,
            "spy_ma20": 0.0,
            "spy_ma50": 0.0,
            "spx_change_percent": 0.0,
            "qqq_change_percent": 0.0,
            "price_change_5d": 0.0,
            "major_event_hours": 999,
            "liquidity_ratio": 1.0,
            "spread_ratio": 0.001,
            "market_regime": "RANGE",
        }
        return context

    def get_vix(self) -> float:
        """获取 VIX（免费计划不支持 VIX 查询，直接返回默认值 20.0）"""
        # TwelveData 免费计划不支持 VIX 指数，调用会返回 symbol invalid 错误
        # VIX 由 FinnhubProvider 或 YahooFinanceProvider 提供
        logger.debug("[TwelveData] VIX 查询跳过（免费计划不支持），返回默认值 20.0")
        return 20.0

    def get_spy_context(self) -> Dict[str, Any]:
        """获取 SPY 大盘上下文（消耗 1 credit，缓存 15 分钟）"""
        cache_key = "__spy_td__"
        if self._is_cache_valid(cache_key, ttl_override=900):
            return self._cache.get(cache_key, {})

        ts = self.get_time_series("US.SPY", outputsize=60)
        if not ts:
            return {}

        prices = ts.get("close_prices", [])
        spy_price = ts["price"]
        spy_prev  = prices[-2] if len(prices) >= 2 else spy_price
        price_5d  = prices[-6] if len(prices) >= 6 else spy_price

        context = {
            "spy_price": spy_price,
            "spy_ma20": ts["ma20"],
            "spy_ma50": ts["ma50"],
            "spx_change_percent": (spy_price - spy_prev) / spy_prev if spy_prev > 0 else 0.0,
            "qqq_change_percent": 0.0,   # QQQ 单独拿需再消耗 1 credit，暂留 0
            "price_change_5d": (spy_price - price_5d) / price_5d if price_5d > 0 else 0.0,
        }
        self._set_cache(cache_key, context)
        return context
