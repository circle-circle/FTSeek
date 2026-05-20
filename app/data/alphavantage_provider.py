"""
Alpha Vantage 数据提供商

免费美股行情数据源 (实时 + 历史数据)
替换 Yahoo Finance，作为 TwelveData + Finnhub 的备用数据源

免费计划限速：
- 标准 API Key: 5 次/分钟，500 次/天
- 高级 API Key: 75 次/分钟，75,000 次/天

文档：https://www.alphavantage.co/documentation/
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

# 加载环境变量
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env.us")

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────
BASE_URL = "https://www.alphavantage.co/query"
# 免费计划：5 次/分钟，500 次/天
FREE_CALLS_PER_MINUTE = 5
FREE_CALLS_PER_DAY = 500
REFILL_INTERVAL_SEC = 60.0 / FREE_CALLS_PER_MINUTE  # 12s/call


class _RateLimiter:
    """令牌桶限速器（线程安全）"""

    def __init__(self, capacity: int, refill_interval_sec: float, daily_limit: int = 500):
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_interval = refill_interval_sec
        self._last_refill = time.monotonic()
        self._daily_limit = daily_limit
        self._daily_calls = 0
        self._daily_reset_date = datetime.now().date()
        self._lock = threading.Lock()

    def _refill(self):
        """补充令牌"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        tokens_to_add = elapsed / self._refill_interval
        self._tokens = min(self._capacity, self._tokens + tokens_to_add)
        self._last_refill = now

    def _check_daily_limit(self) -> bool:
        """检查每日限额"""
        today = datetime.now().date()
        if today != self._daily_reset_date:
            self._daily_calls = 0
            self._daily_reset_date = today
        return self._daily_calls < self._daily_limit

    def acquire(self, n: int = 1, timeout: float = 120.0) -> bool:
        """
        消耗 n 个令牌，不足时等待直到超时。
        Returns True 成功，False 超时或超出每日限额。
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                
                # 检查每日限额
                if not self._check_daily_limit():
                    logger.warning(f"[AlphaVantage] 超出每日限额 {self._daily_limit} 次")
                    return False
                
                if self._tokens >= n:
                    self._tokens -= n
                    self._daily_calls += n
                    return True
            
            # 等待令牌补充
            if time.monotonic() >= deadline:
                logger.error(f"[AlphaVantage] 等待令牌超时 ({timeout}s)")
                return False
            
            time.sleep(0.5)

    def get_status(self) -> Dict[str, Any]:
        """获取限速器状态"""
        with self._lock:
            self._refill()
            return {
                "capacity": self._capacity,
                "tokens_remaining": round(self._tokens, 1),
                "refill_interval_sec": self._refill_interval,
                "daily_limit": self._daily_limit,
                "daily_calls": self._daily_calls,
                "daily_remaining": self._daily_limit - self._daily_calls,
            }


class AlphaVantageError(Exception):
    """Alpha Vantage API 错误"""
    pass


class RateLimitError(AlphaVantageError):
    """速率限制错误"""
    pass


class DataNotFoundError(AlphaVantageError):
    """数据未找到错误"""
    pass


class AlphaVantageProvider:
    """
    Alpha Vantage 数据提供商
    
    支持功能:
    - 实时报价 (GLOBAL_QUOTE)
    - 历史 K 线 (TIME_SERIES_DAILY)
    - 技术指标 (SMA/EMA/RSI/MACD 等)
    - 大盘指数 (SPY/QQQ)
    - VIX 恐慌指数
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_ttl_seconds: int = 900,  # 15 分钟缓存
        premium: bool = False,
    ):
        """
        初始化 Alpha Vantage 提供商
        
        Args:
            api_key: API Key (从环境变量读取)
            cache_ttl_seconds: 缓存 TTL
            premium: 是否高级账户 (75 次/分钟)
        """
        self.api_key = api_key or os.getenv("ALPHAVANTAGE_API_KEY", "")
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self.premium = premium
        
        # 限速器
        if premium:
            self._limiter = _RateLimiter(
                capacity=75,
                refill_interval_sec=60.0/75,
                daily_limit=75000
            )
        else:
            self._limiter = _RateLimiter(
                capacity=5,
                refill_interval_sec=12.0,
                daily_limit=500
            )
        
        # 缓存
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_time: Dict[str, datetime] = {}
        
        # HTTP Session (连接复用)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "FTSeek-US/1.0 (Trading System)",
        })
        
        self.enabled = bool(self.api_key)
        
        if self.enabled:
            logger.info(
                f"[AlphaVantage] 初始化完成 | "
                f"API Key=✅ | "
                f"Premium={premium} | "
                f"Cache TTL={cache_ttl_seconds}s"
            )
        else:
            logger.warning("[AlphaVantage] API Key 未配置，服务已禁用")
    
    # ── 内部方法 ───────────────────────────────────────────────────
    
    def _request(self, function: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        发送 API 请求
        
        Args:
            function: API 函数名
            params: 请求参数
        
        Returns:
            API 响应数据或 None
        
        Raises:
            RateLimitError: 超出速率限制
            DataNotFoundError: 数据未找到
        """
        if not self.enabled:
            logger.warning("[AlphaVantage] 服务未启用")
            return None
        
        # 检查限速
        if not self._limiter.acquire(timeout=30.0):
            raise RateLimitError("超出速率限制或每日限额")
        
        # 构建请求
        request_params = {
            "function": function,
            "apikey": self.api_key,
            **params
        }
        
        try:
            response = self._session.get(BASE_URL, params=request_params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # 检查错误信息
            if "Error Message" in data:
                raise DataNotFoundError(f"API 错误：{data['Error Message']}")
            
            if "Note" in data:
                # 速率限制提示
                logger.warning(f"[AlphaVantage] 速率限制：{data['Note']}")
                raise RateLimitError(data["Note"])
            
            return data
            
        except requests.exceptions.Timeout:
            logger.error("[AlphaVantage] 请求超时")
            raise AlphaVantageError("请求超时")
        except requests.exceptions.RequestException as e:
            logger.error(f"[AlphaVantage] 网络错误：{e}")
            raise AlphaVantageError(f"网络错误：{e}")
        except ValueError as e:
            logger.error(f"[AlphaVantage] JSON 解析错误：{e}")
            raise AlphaVantageError(f"JSON 解析错误：{e}")
    
    def _is_cache_valid(self, key: str, min_age_seconds: int = 0) -> bool:
        """检查缓存是否有效"""
        if key not in self.cache:
            return False
        
        cache_time = self.cache_time.get(key)
        if not cache_time:
            return False
        
        age = datetime.now() - cache_time
        
        # 最小年龄检查 (避免频繁刷新)
        if age.total_seconds() < min_age_seconds:
            return True
        
        # 最大年龄检查 (超时失效)
        return age.total_seconds() < self.cache_ttl.total_seconds()
    
    def _symbol_to_ticker(self, symbol: str) -> str:
        """转换代码格式 (US.AAPL → AAPL)"""
        return symbol.replace("US.", "")
    
    # ── 公开方法 ───────────────────────────────────────────────────
    
    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时报价 (GLOBAL_QUOTE)
        
        Args:
            symbol: 股票代码 (US.AAPL 格式)
        
        Returns:
            报价字典或 None
        """
        try:
            # 检查缓存
            cache_key = f"quote_{symbol}"
            if self._is_cache_valid(cache_key):
                logger.debug(f"[AlphaVantage] 使用缓存报价：{symbol}")
                return self.cache[cache_key]
            
            ticker = self._symbol_to_ticker(symbol)
            
            # 请求 API
            data = self._request("GLOBAL_QUOTE", {"symbol": ticker})
            if not data:
                return None
            
            quote_data = data.get("Global Quote", {})
            
            if not quote_data:
                logger.warning(f"[AlphaVantage] 无报价数据：{symbol}")
                return None
            
            quote = {
                "symbol": symbol,
                "price": float(quote_data.get("05. price", 0)),
                "change": float(quote_data.get("09. change", 0)),
                "change_percent": float(quote_data.get("10. change percent", "0%").replace("%", "")) / 100,
                "volume": int(quote_data.get("06. volume", 0)),
                "high": float(quote_data.get("03. high", 0)),
                "low": float(quote_data.get("04. low", 0)),
                "open": float(quote_data.get("02. open", 0)),
                "previous_close": float(quote_data.get("08. previous close", 0)),
                "timestamp": datetime.now().isoformat(),
            }
            
            # 更新缓存
            self.cache[cache_key] = quote
            self.cache_time[cache_key] = datetime.now()
            
            logger.info(f"[AlphaVantage] 获取报价：{symbol} ${quote['price']:.2f} ({quote['change_percent']:+.2%})")
            return quote
            
        except (RateLimitError, DataNotFoundError) as e:
            logger.warning(f"[AlphaVantage] 获取报价失败 {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"[AlphaVantage] 获取报价异常 {symbol}: {e}")
            return None
    
    def get_time_series(self, symbol: str, outputsize: str = "compact") -> Optional[Dict[str, Any]]:
        """
        获取历史 K 线数据 (TIME_SERIES_DAILY)
        
        Args:
            symbol: 股票代码
            outputsize: "compact" (100 天) 或 "full" (20 年)
        
        Returns:
            时间序列数据或 None
        """
        try:
            cache_key = f"timeseries_{symbol}_{outputsize}"
            if self._is_cache_valid(cache_key, min_age_seconds=300):
                return self.cache[cache_key]
            
            ticker = self._symbol_to_ticker(symbol)
            
            data = self._request(
                "TIME_SERIES_DAILY",
                {"symbol": ticker, "outputsize": outputsize}
            )
            if not data:
                return None
            
            series_data = data.get("Time Series (Daily)", {})
            
            if not series_data:
                logger.warning(f"[AlphaVantage] 无时间序列数据：{symbol}")
                return None
            
            # 转换为列表格式 (按日期排序)
            timeseries = []
            for date_str in sorted(series_data.keys(), reverse=True):
                d = series_data[date_str]
                timeseries.append({
                    "date": date_str,
                    "open": float(d.get("1. open", 0)),
                    "high": float(d.get("2. high", 0)),
                    "low": float(d.get("3. low", 0)),
                    "close": float(d.get("4. close", 0)),
                    "volume": int(d.get("5. volume", 0)),
                })
            
            result = {
                "symbol": symbol,
                "timeseries": timeseries,
                "metadata": data.get("Meta Data", {}),
            }
            
            # 更新缓存
            self.cache[cache_key] = result
            self.cache_time[cache_key] = datetime.now()
            
            logger.debug(f"[AlphaVantage] 获取时间序列：{symbol} ({len(timeseries)} 天)")
            return result
            
        except Exception as e:
            logger.error(f"[AlphaVantage] 获取时间序列失败 {symbol}: {e}")
            return None
    
    def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取完整市场数据 (用于信号评分)
        
        Args:
            symbol: 股票代码
        
        Returns:
            市场数据字典
        """
        try:
            cache_key = f"market_{symbol}"
            if self._is_cache_valid(cache_key, min_age_seconds=300):
                return self.cache[cache_key]
            
            # 获取时间序列
            ts_data = self.get_time_series(symbol, outputsize="compact")
            if not ts_data or len(ts_data["timeseries"]) < 50:
                logger.warning(f"[AlphaVantage] 历史数据不足 {symbol}")
                return None
            
            timeseries = ts_data["timeseries"]
            current = timeseries[0]  # 最新数据
            
            # 计算移动平均
            closes = [d["close"] for d in timeseries[:50]]
            ma20 = sum(closes[:20]) / 20 if len(closes) >= 20 else current["close"]
            ma50 = sum(closes[:50]) / 50 if len(closes) >= 50 else ma20
            
            # 成交量均值
            volumes = [d["volume"] for d in timeseries[:20]]
            avg_volume = sum(volumes) / len(volumes) if volumes else current["volume"]
            
            # 计算指标
            current_price = current["close"]
            volume_ratio = current["volume"] / avg_volume if avg_volume > 0 else 1.0
            momentum = (current_price - ma20) / ma20 if ma20 > 0 else 0
            
            market_data = {
                "symbol": symbol,
                "price": current_price,
                "volume": current["volume"],
                "volume_ratio": volume_ratio,
                "momentum": momentum,
                "trend": "UP" if current_price > ma50 else "DOWN",
                "ma20": ma20,
                "ma50": ma50,
                "price_vs_ma20": (current_price - ma20) / ma20,
                "price_vs_ma50": (current_price - ma50) / ma50,
                "high_52w": max(d["high"] for d in timeseries[:252]) if len(timeseries) >= 252 else current["high"],
                "low_52w": min(d["low"] for d in timeseries[:252]) if len(timeseries) >= 252 else current["low"],
                "timestamp": datetime.now().isoformat(),
            }
            
            # 更新缓存
            self.cache[cache_key] = market_data
            self.cache_time[cache_key] = datetime.now()
            
            logger.info(f"[AlphaVantage] 获取市场数据：{symbol} 趋势={market_data['trend']} 动量={momentum:.2%}")
            return market_data
            
        except Exception as e:
            logger.error(f"[AlphaVantage] 获取市场数据失败 {symbol}: {e}")
            return None
    
    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取报价 (逐个请求，注意限速)
        
        Args:
            symbols: 股票代码列表
        
        Returns:
            报价字典
        """
        results = {}
        failed = []
        
        for symbol in symbols:
            quote = self.get_quote(symbol)
            if quote:
                results[symbol] = quote
            else:
                failed.append(symbol)
            
            # 限速等待
            if not self.premium:
                time.sleep(12.0)  # 免费账户 5 次/分钟
        
        if failed:
            logger.warning(f"[AlphaVantage] 批量获取失败：{failed}")
        
        return results
    
    def get_vix(self) -> float:
        """
        获取 VIX 恐慌指数
        
        尝试多种数据源：
        1. Alpha Vantage (^VIX)
        2. FRED API (VIXCLS) - 更稳定
        3. 默认值 20.0
        
        Returns:
            VIX 值或默认值 20.0
        """
        try:
            cache_key = "vix"
            if self._is_cache_valid(cache_key, min_age_seconds=600):
                return self.cache.get(cache_key, {}).get("vix", 20.0)
            
            # 方法1：尝试 Alpha Vantage ^VIX
            data = self._request("GLOBAL_QUOTE", {"symbol": "^VIX"})
            if data and data.get("Global Quote"):
                vix_str = data["Global Quote"].get("05. price", "")
                if vix_str:
                    vix = float(vix_str)
                    result = {"vix": vix}
                    self.cache[cache_key] = result
                    self.cache_time[cache_key] = datetime.now()
                    logger.info(f"[AlphaVantage] 获取 VIX 成功: {vix:.2f}")
                    return vix
            
            # 方法2：尝试 FRED API VIX (VIXCLS)
            try:
                fred_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
                response = requests.get(fred_url, timeout=10)
                if response.status_code == 200:
                    lines = response.text.strip().split('\n')
                    if len(lines) > 1:
                        # 获取最后一行数据
                        last_line = lines[-1]
                        parts = last_line.split(',')
                        if len(parts) >= 2 and parts[1]:
                            vix = float(parts[1])
                            result = {"vix": vix}
                            self.cache[cache_key] = result
                            self.cache_time[cache_key] = datetime.now()
                            logger.info(f"[FRED] 获取 VIX 成功: {vix:.2f}")
                            return vix
            except Exception as fred_e:
                logger.debug(f"[FRED] VIX 获取失败: {fred_e}")
            
            logger.warning("[AlphaVantage] VIX 数据获取失败，使用默认值 20.0")
            
        except Exception as e:
            logger.warning(f"[AlphaVantage] 获取 VIX 异常，使用默认值 20.0：{e}")
        
        return 20.0  # 默认值
    
    def get_spy_context(self) -> Dict[str, Any]:
        """
        获取大盘方向上下文（SPY + QQQ）
        
        Returns:
            大盘数据字典
        """
        try:
            cache_key = "spy_context"
            if self._is_cache_valid(cache_key, min_age_seconds=1800):  # 30min 缓存，节省免费配额
                return self.cache.get(cache_key, {})
            
            # 获取 SPY 数据
            # SPY 直接使用代码 (Alpha Vantage 不需要 US.前缀)
            spy_ts = self.get_time_series("SPY", outputsize="compact")
            if not spy_ts or len(spy_ts["timeseries"]) < 20:
                logger.warning("[AlphaVantage] SPY 数据不足")
                return {}
            
            spy_series = spy_ts["timeseries"]
            spy_price = spy_series[0]["close"]
            spy_prev = spy_series[1]["close"] if len(spy_series) > 1 else spy_price
            
            closes = [d["close"] for d in spy_series[:50]]
            spy_ma20 = sum(closes[:20]) / 20 if len(closes) >= 20 else spy_price
            spy_ma50 = sum(closes[:50]) / 50 if len(closes) >= 50 else spy_ma20
            
            # 获取 QQQ 数据
            qqq_change = 0.0
            try:
                qqq_ts = self.get_time_series("QQQ", outputsize="compact")
                if qqq_ts and len(qqq_ts["timeseries"]) > 1:
                    qqq_price = qqq_ts["timeseries"][0]["close"]
                    qqq_prev = qqq_ts["timeseries"][1]["close"]
                    qqq_change = (qqq_price - qqq_prev) / qqq_prev if qqq_prev > 0 else 0
            except Exception as e:
                logger.warning(f"[AlphaVantage] 获取 QQQ 失败：{e}")
            
            # 5 日涨跌幅
            price_change_5d = 0.0
            if len(spy_series) >= 5:
                price_5d_ago = spy_series[4]["close"]
                price_change_5d = (spy_price - price_5d_ago) / price_5d_ago if price_5d_ago > 0 else 0
            
            context = {
                "spy_price": spy_price,
                "spy_ma20": spy_ma20,
                "spy_ma50": spy_ma50,
                "spx_change_percent": (spy_price - spy_prev) / spy_prev if spy_prev > 0 else 0,
                "qqq_change_percent": qqq_change,
                "price_change_5d": price_change_5d,
            }
            
            self.cache[cache_key] = context
            self.cache_time[cache_key] = datetime.now()
            
            logger.info(f"[AlphaVantage] 大盘方向 SPY={spy_price:.2f} MA20={spy_ma20:.2f} 涨跌={context['spx_change_percent']:.2%}")
            return context
            
        except Exception as e:
            logger.error(f"[AlphaVantage] 获取大盘上下文失败：{e}")
            return {}
    
    def compute_atr(self, symbol: str, period: int = 14) -> Dict[str, float]:
        """
        计算 ATR（平均真实波幅）
        
        Args:
            symbol: 股票代码
            period: ATR 周期
        
        Returns:
            ATR 数据字典
        """
        try:
            cache_key = f"atr_{symbol}_{period}"
            if self._is_cache_valid(cache_key, min_age_seconds=1800):
                return self.cache.get(cache_key, {})
            
            ts_data = self.get_time_series(symbol, outputsize="compact")
            if not ts_data or len(ts_data["timeseries"]) < period + 5:
                return {"atr": 0, "avg_atr": 0, "volatility": 0, "avg_volatility": 0}
            
            timeseries = ts_data["timeseries"]
            
            # 计算 TR (True Range)
            tr_values = []
            for i in range(1, min(len(timeseries), period + 5)):
                current = timeseries[i - 1]
                prev = timeseries[i]
                
                high = current["high"]
                low = current["low"]
                prev_close = prev["close"]
                
                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close)
                )
                tr_values.append(tr)
            
            # 计算 ATR
            atr = sum(tr_values[:period]) / period if len(tr_values) >= period else 0
            avg_atr = sum(tr_values) / len(tr_values) if tr_values else 0
            
            # 波动率
            current_price = timeseries[0]["close"]
            volatility = atr / current_price if current_price > 0 else 0
            avg_price = sum(d["close"] for d in timeseries[:20]) / 20
            avg_volatility = avg_atr / avg_price if avg_price > 0 else 0
            
            result = {
                "atr": atr,
                "avg_atr": avg_atr,
                "volatility": volatility,
                "avg_volatility": avg_volatility,
            }
            
            self.cache[cache_key] = result
            self.cache_time[cache_key] = datetime.now()
            
            return result
            
        except Exception as e:
            logger.warning(f"[AlphaVantage] 计算 ATR 失败 {symbol}: {e}")
            return {"atr": 0, "avg_atr": 0, "volatility": 0, "avg_volatility": 0}
    
    def get_full_market_context(self, symbol: str) -> Dict[str, Any]:
        """
        获取完整市场上下文（用于 SignalScorer）
        
        整合：技术指标 + ATR + VIX + 大盘方向
        
        Returns:
            满足 SignalScorer 所有因子需要的上下文字典
        """
        # 基础行情数据
        market_data = self.get_market_data(symbol) or {}
        
        # ATR / 波动率
        atr_data = self.compute_atr(symbol)
        
        # VIX
        vix = self.get_vix()
        
        # 大盘方向
        spy_ctx = self.get_spy_context()
        
        # 判断趋势方向
        trend_direction = market_data.get("trend", "NEUTRAL")
        
        # 市场状态判断
        from app.core.models import MarketRegime
        if vix > 25 or atr_data.get("volatility", 0) > atr_data.get("avg_volatility", 0) * 1.5:
            regime = MarketRegime.HIGH_VOL.value
        elif trend_direction == "UP":
            regime = MarketRegime.TREND_UP.value
        elif trend_direction == "DOWN":
            regime = MarketRegime.TREND_DOWN.value
        elif vix < 15:
            regime = MarketRegime.LOW_VOL.value
        else:
            regime = MarketRegime.RANGE.value
        
        context = {
            # 基础数据
            "symbol": symbol,
            "price": market_data.get("price", 0),
            "volume": market_data.get("volume", 0),
            "volume_ratio": market_data.get("volume_ratio", 1.0),
            "momentum": market_data.get("momentum", 0),
            "ma20": market_data.get("ma20", 0),
            "ma50": market_data.get("ma50", 0),
            # 趋势
            "trend_direction": trend_direction,
            # 波动率
            "volatility": atr_data.get("volatility", 0),
            "avg_volatility": atr_data.get("avg_volatility", 0),
            "atr": atr_data.get("atr", 0),
            "avg_atr": atr_data.get("avg_atr", 0),
            # VIX + 大盘
            "vix": vix,
            "spy_price": spy_ctx.get("spy_price", 0),
            "spy_ma20": spy_ctx.get("spy_ma20", 0),
            "spy_ma50": spy_ctx.get("spy_ma50", 0),
            "spx_change_percent": spy_ctx.get("spx_change_percent", 0),
            "qqq_change_percent": spy_ctx.get("qqq_change_percent", 0),
            "price_change_5d": spy_ctx.get("price_change_5d", 0),
            # 市场状态
            "market_regime": regime,
            # 占位（由 news_provider 注入）
            "news_sentiment": 0.0,
            # 无重大事件默认值
            "major_event_hours": 999,
            "liquidity_ratio": 1.0,
            "spread_ratio": 0.001,
        }
        
        return context
    
    def get_rate_limit_status(self) -> Dict[str, Any]:
        """获取限速器状态"""
        return self._limiter.get_status()
    
    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()
        self.cache_time.clear()
        logger.info("[AlphaVantage] 缓存已清空")
    
    def close(self):
        """关闭连接"""
        self._session.close()
        logger.info("[AlphaVantage] 连接已关闭")


# 测试脚本
if __name__ == "__main__":
    import json
    
    provider = AlphaVantageProvider()
    
    if not provider.enabled:
        print("❌ Alpha Vantage API Key 未配置")
        exit(1)
    
    print("=" * 60)
    print("Alpha Vantage 数据提供商测试")
    print("=" * 60)
    
    # 测试单只股票报价
    print("\n1. 测试单只股票报价:")
    quote = provider.get_quote("US.AAPL")
    if quote:
        print(f"   AAPL: ${quote['price']:.2f} ({quote['change_percent']:+.2%})")
    
    # 测试市场数据
    print("\n2. 测试市场数据:")
    market = provider.get_market_data("US.AAPL")
    if market:
        print(f"   价格：${market['price']:.2f}")
        print(f"   趋势：{market['trend']}")
        print(f"   动量：{market['momentum']:.2%}")
    
    # 测试 VIX
    print("\n3. 测试 VIX:")
    vix = provider.get_vix()
    print(f"   VIX: {vix:.2f}")
    
    # 测试大盘上下文
    print("\n4. 测试大盘上下文:")
    spy_ctx = provider.get_spy_context()
    if spy_ctx:
        print(f"   SPY: ${spy_ctx['spy_price']:.2f}")
        print(f"   SPY 涨跌：{spy_ctx['spx_change_percent']:.2%}")
    
    # 测试 ATR
    print("\n5. 测试 ATR:")
    atr = provider.compute_atr("US.AAPL")
    print(f"   ATR: {atr['atr']:.2f} (波动率：{atr['volatility']:.2%})")
    
    # 测试限速状态
    print("\n6. 限速状态:")
    status = provider.get_rate_limit_status()
    print(f"   剩余令牌：{status['tokens_remaining']}/{status['capacity']}")
    print(f"   今日剩余：{status['daily_remaining']}/{status['daily_limit']}")
    
    print("\n" + "=" * 60)
    print("测试完成!")
