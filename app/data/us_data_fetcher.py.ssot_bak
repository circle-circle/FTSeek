"""
美股数据获取模块 (US Market Data Fetcher)

数据源架构 (参考 FTSeek-US):
  Primary:   TwelveData  — 技术指标、历史 K 线、SPY/QQQ 大盘
  Secondary: Finnhub     — 实时报价、新闻情绪
  Fallback:  Alpha Vantage — 备用数据源 + VIX

限速小结:
  - TwelveData:   8 credits/min (免费)
  - Finnhub:     60 calls/min (免费)
  - Alpha Vantage: 5 calls/min, 500 calls/day

特点:
  - 使用集中配置管理器 (app.data.us_data_config)
  - 自动加载 API Key (环境变量 → .env.us 共享)
  - 内置限速器和缓存
  - 优雅降级 fallback
  - 健康检查接口

更新时间: 2026-04-13
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from threading import Lock

logger = logging.getLogger("ftseek.us_data")


class _RateLimiter:
    """令牌桶限速器 (线程安全)"""

    def __init__(self, capacity: int, refill_interval_sec: float):
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_interval = refill_interval_sec
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def acquire(self, n: int = 1, timeout: float = 30.0) -> bool:
        """获取 n 个令牌，不足时等待直到超时"""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return True
            wait = self._refill_interval * n
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, deadline - time.monotonic()))

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed / self._refill_interval
        if new_tokens >= 1:
            self._tokens = min(self._capacity, self._tokens + new_tokens)
            self._last_refill = now


class TwelveDataProvider:
    """TwelveData 数据提供商 (Primary)"""

    def __init__(self, config: Dict):
        self.config = config
        self.api_key = config.get('api_key', '')
        self.enabled = config.get('enabled', False) and bool(self.api_key)
        self.base_url = config.get('base_url', 'https://api.twelvedata.com')

        # 限速
        rate_limit = config.get('rate_limit', {})
        self._limiter = _RateLimiter(
            capacity=rate_limit.get('credits_per_minute', 8),
            refill_interval_sec=rate_limit.get('refill_interval_sec', 7.5)
        )

        # 缓存
        self._cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = config.get('cache_ttl_seconds', 900)

    def _is_cached(self, key: str) -> bool:
        if key not in self._cache:
            return False
        return (time.time() - self._cache_time.get(key, 0)) < self._cache_ttl

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_time[key] = time.time()

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """获取实时报价"""
        if not self.enabled:
            return None

        cache_key = f"quote_{symbol}"
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        if not self._limiter.acquire(1):
            logger.warning("[TwelveData] 限速，跳过")
            return None

        try:
            ticker = symbol.replace("US.", "")
            url = f"{self.base_url}/quote"
            params = {"symbol": ticker, "apikey": self.api_key}

            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if "status" in data and data.get("status") != "ok":
                logger.warning(f"[TwelveData] 获取报价失败 {symbol}: {data.get('message', '')}")
                return None

            quote = {
                "symbol": symbol,
                "price": float(data.get("close", 0)),
                "open": float(data.get("open", 0)),
                "high": float(data.get("high", 0)),
                "low": float(data.get("low", 0)),
                "volume": int(data.get("volume", 0)),
                "change_pct": float(data.get("percent_change", 0)),
                "previous_close": float(data.get("previous_close", 0)),
                "name": data.get("name", ""),
            }

            self._set_cache(cache_key, quote)
            return quote

        except Exception as e:
            logger.error(f"[TwelveData] 获取报价异常 {symbol}: {e}")
            return None

    def get_history(self, symbol: str, interval: str = "1day", days: int = 60) -> List[Dict]:
        """获取历史 K 线数据"""
        if not self.enabled:
            return []

        cache_key = f"history_{symbol}_{interval}_{days}"
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        if not self._limiter.acquire(1):
            return []

        try:
            ticker = symbol.replace("US.", "")
            url = f"{self.base_url}/time_series"
            params = {
                "symbol": ticker,
                "interval": interval,
                "outputsize": days,
                "apikey": self.api_key,
            }

            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if "values" not in data:
                return []

            result = []
            for bar in data["values"]:
                result.append({
                    "datetime": bar.get("datetime", ""),
                    "open": float(bar.get("open", 0)),
                    "high": float(bar.get("high", 0)),
                    "low": float(bar.get("low", 0)),
                    "close": float(bar.get("close", 0)),
                    "volume": int(bar.get("volume", 0)),
                })

            self._set_cache(cache_key, result)
            return result

        except Exception as e:
            logger.error(f"[TwelveData] 获取历史数据异常 {symbol}: {e}")
            return []

    def get_spy_context(self) -> Dict:
        """获取 SPY/QQQ 大盘上下文"""
        cache_key = "spy_context"
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        try:
            spy_hist = self.get_history("US.SPY", "1day", 60)
            qqq_hist = self.get_history("US.QQQ", "1day", 5)

            if not spy_hist or len(spy_hist) < 20:
                return {}

            spy_price = spy_hist[-1]["close"]
            spy_prev = spy_hist[-2]["close"]
            spy_ma20 = sum(b["close"] for b in spy_hist[-20:]) / 20
            spy_ma50 = sum(b["close"] for b in spy_hist[-min(50, len(spy_hist)):]) / min(50, len(spy_hist))

            qqq_price = qqq_hist[-1]["close"] if len(qqq_hist) > 1 else 0
            qqq_prev = qqq_hist[-2]["close"] if len(qqq_hist) > 1 else qqq_price

            context = {
                "spy_price": spy_price,
                "spy_ma20": spy_ma20,
                "spy_ma50": spy_ma50,
                "spx_change_percent": (spy_price - spy_prev) / spy_prev if spy_prev > 0 else 0,
                "qqq_change_percent": (qqq_price - qqq_prev) / qqq_prev if qqq_prev > 0 else 0,
            }

            self._set_cache(cache_key, context)
            return context

        except Exception as e:
            logger.error(f"[TwelveData] 获取大盘上下文异常: {e}")
            return {}

    def get_rate_limit_status(self) -> Dict:
        """获取限速状态"""
        return {
            "tokens": int(self._limiter._tokens),
            "capacity": self._limiter._capacity,
        }


class FinnhubProvider:
    """Finnhub 数据提供商 (Secondary)"""

    def __init__(self, config: Dict):
        self.config = config
        self.api_key = config.get('api_key', '')
        self.enabled = config.get('enabled', False) and bool(self.api_key)
        self.base_url = config.get('base_url', 'https://finnhub.io/api/v1')

        # 缓存
        self._cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = config.get('cache_ttl_seconds', 60)

    def _is_cached(self, key: str) -> bool:
        if key not in self._cache:
            return False
        return (time.time() - self._cache_time.get(key, 0)) < self._cache_ttl

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_time[key] = time.time()

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """获取实时报价"""
        if not self.enabled:
            return None

        cache_key = f"quote_{symbol}"
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        try:
            ticker = symbol.replace("US.", "")
            url = f"{self.base_url}/quote"
            params = {"symbol": ticker, "token": self.api_key}

            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("c", 0) <= 0:
                return None

            quote = {
                "symbol": symbol,
                "price": float(data.get("c", 0)),
                "open": float(data.get("o", 0)),
                "high": float(data.get("h", 0)),
                "low": float(data.get("l", 0)),
                "previous_close": float(data.get("pc", 0)),
                "change_pct": ((data.get("c", 0) - data.get("pc", 0)) / data.get("pc", 1) * 100) if data.get("pc", 0) > 0 else 0,
                "volume": 0,
                "name": "",
            }

            self._set_cache(cache_key, quote)
            return quote

        except Exception as e:
            logger.error(f"[Finnhub] 获取报价异常 {symbol}: {e}")
            return None

    def get_rate_limit_status(self) -> Dict:
        """获取限速状态"""
        return {"info": "60 calls/min (free plan)"}


class AlphaVantageProvider:
    """Alpha Vantage 数据提供商 (Fallback)"""

    def __init__(self, config: Dict):
        self.config = config
        self.api_key = config.get('api_key', '')
        self.enabled = config.get('enabled', False) and bool(self.api_key)
        self.base_url = config.get('base_url', 'https://www.alphavantage.co/query')

        # 限速
        rate_limit = config.get('rate_limit', {})
        self._max_per_min = rate_limit.get('calls_per_minute', 5)
        self._call_count = 0
        self._last_call_time = 0

        # 缓存
        self._cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = config.get('cache_ttl_seconds', 1800)

    def _rate_limit_check(self) -> bool:
        """简单限速: 5 calls/min"""
        now = time.time()
        if now - self._last_call_time < 60:
            if self._call_count >= self._max_per_min:
                logger.warning("[AlphaVantage] 限速中，跳过")
                return False
        else:
            self._call_count = 0
            self._last_call_time = now
        self._call_count += 1
        return True

    def _is_cached(self, key: str) -> bool:
        if key not in self._cache:
            return False
        return (time.time() - self._cache_time.get(key, 0)) < self._cache_ttl

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_time[key] = time.time()

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """获取实时报价 (GLOBAL_QUOTE)"""
        if not self.enabled:
            return None

        cache_key = f"quote_{symbol}"
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        if not self._rate_limit_check():
            return None

        try:
            ticker = symbol.replace("US.", "")
            params = {"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": self.api_key}

            resp = requests.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            quote_data = data.get("Global Quote", {})
            if not quote_data:
                return None

            price = float(quote_data.get("05. price", 0))
            change_pct_str = quote_data.get("10. change percent", "0%").replace("%", "")

            quote = {
                "symbol": symbol,
                "price": price,
                "open": float(quote_data.get("02. open", 0)),
                "high": float(quote_data.get("03. high", 0)),
                "low": float(quote_data.get("04. low", 0)),
                "volume": int(quote_data.get("06. volume", 0)),
                "change_pct": float(change_pct_str) if change_pct_str else 0,
                "previous_close": float(quote_data.get("08. previous close", 0)),
                "name": "",
            }

            self._set_cache(cache_key, quote)
            return quote

        except Exception as e:
            logger.error(f"[AlphaVantage] 获取报价异常 {symbol}: {e}")
            return None

    def get_vix(self) -> float:
        """获取 VIX 恐慌指数"""
        cache_key = "vix"
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        if not self._rate_limit_check():
            return 20.0

        try:
            params = {"function": "GLOBAL_QUOTE", "symbol": "VIX", "apikey": self.api_key}
            resp = requests.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            quote_data = data.get("Global Quote", {})
            if quote_data:
                vix = float(quote_data.get("05. price", 20.0))
                self._set_cache(cache_key, vix)
                return vix
        except Exception as e:
            logger.warning(f"[AlphaVantage] 获取 VIX 异常: {e}")

        return 20.0

    def get_rate_limit_status(self) -> Dict:
        """获取限速状态"""
        now = time.time()
        if now - self._last_call_time > 60:
            remaining = self._max_per_min
        else:
            remaining = self._max_per_min - self._call_count
        return {
            "calls_remaining_this_min": remaining,
            "daily_remaining": 500,  # 粗略估计
        }


class USMarketDataRouter:
    """
    美股数据路由器 (参考 FTSeek-US 架构)

    优先级:
    1. Finnhub (实时报价，最快，支持盘前)
    2. Alpha Vantage (备用 + VIX)
    3. TwelveData (已禁用，2026-04-22)
    """

    def __init__(self, config: Optional[Dict] = None):
        # 从配置管理器获取配置
        if config is None:
            from app.data.us_data_config import get_us_data_config
            cfg_manager = get_us_data_config()
            config = cfg_manager._config

        # 初始化数据提供商 (注入 API Key)
        tw_cfg = config.get('us_data_sources', {}).get('twelvedata', {})
        tw_cfg['api_key'] = self._get_api_key_from_config(config, 'twelvedata')
        self.twelvedata = TwelveDataProvider(tw_cfg)

        fh_cfg = config.get('us_data_sources', {}).get('finnhub', {})
        fh_cfg['api_key'] = self._get_api_key_from_config(config, 'finnhub')
        self.finnhub = FinnhubProvider(fh_cfg)

        av_cfg = config.get('us_data_sources', {}).get('alphavantage', {})
        av_cfg['api_key'] = self._get_api_key_from_config(config, 'alphavantage')
        self.alphavantage = AlphaVantageProvider(av_cfg)

        # 盘前配置
        self._premarket_config = config.get('premarket', {})

        enabled = []
        if self.twelvedata.enabled:
            enabled.append("TwelveData")
        if self.finnhub.enabled:
            enabled.append("Finnhub")
        if self.alphavantage.enabled:
            enabled.append("AlphaVantage")

        logger.info(f"[USMarketDataRouter] 初始化完成 | 数据源: {enabled}")

    def _get_api_key_from_config(self, config: Dict, provider: str) -> str:
        """从配置管理器获取 API Key"""
        from app.data.us_data_config import get_us_data_config
        return get_us_data_config().get_api_key(provider)

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """
        获取报价: Finnhub → TwelveData → AlphaVantage
        """
        # 1. Finnhub (最快)
        if self.finnhub.enabled:
            quote = self.finnhub.get_quote(symbol)
            if quote and quote.get("price", 0) > 0:
                return quote

        # 2. TwelveData
        if self.twelvedata.enabled:
            quote = self.twelvedata.get_quote(symbol)
            if quote and quote.get("price", 0) > 0:
                return quote

        # 3. AlphaVantage (fallback)
        if self.alphavantage.enabled:
            quote = self.alphavantage.get_quote(symbol)
            if quote and quote.get("price", 0) > 0:
                return quote

        logger.warning(f"[USDataRouter] {symbol} 所有数据源均失败")
        return None

    def get_premarket_context(self, us_codes: List[str]) -> str:
        """
        构建盘前分析用的美股市场上下文

        Args:
            us_codes: 美股代码列表

        Returns:
            格式化的美股市场上下文字符串
        """
        max_stocks = self._premarket_config.get('max_stocks_per_fetch', 15)
        fallback_msg = self._premarket_config.get('fallback', {}).get(
            'error_message', '⚠️ 美股数据获取失败，请检查 API Key 配置'
        )

        lines = ["## 🇺🇸 美股市场收盘数据"]
        lines.append("")

        has_data = False

        # 获取 SPY/QQQ 大盘数据
        if self.twelvedata.enabled:
            spy_ctx = self.twelvedata.get_spy_context()
            if spy_ctx:
                has_data = True
                lines.append("### 大盘指数")
                spx_change = spy_ctx.get("spx_change_percent", 0) * 100
                qqq_change = spy_ctx.get("qqq_change_percent", 0) * 100
                lines.append(
                    f"- **S&P 500**: ${spy_ctx.get('spy_price', 0):.2f} ({spx_change:+.2f}%)"
                )
                lines.append(
                    f"- **纳斯达克 100**: ${spy_ctx.get('spy_price', 0):.2f} (QQQ {qqq_change:+.2f}%)"
                )
                lines.append("")

        # 获取 VIX
        if self.alphavantage.enabled:
            vix = self.alphavantage.get_vix()
            lines.append(f"- **VIX**: {vix:.1f}")
        else:
            lines.append("- **VIX**: 20.0 (默认)")
        lines.append("")
        lines.append("### 重点个股")

        # 获取个股数据
        for code in us_codes[:max_stocks]:
            quote = self.get_quote(code)
            if quote:
                has_data = True
                change = quote.get("change_pct", 0)
                direction = "🟢" if change >= 0 else "🔴"
                name = quote.get("name", "")
                lines.append(
                    f"{direction} **{code}** ({name}): "
                    f"${quote['price']:.2f} ({change:+.2f}%)"
                )

        if not has_data:
            return fallback_msg

        return "\n".join(lines)

    def get_health_status(self) -> Dict:
        """获取数据源健康状态"""
        return {
            "twelvedata": {
                "enabled": self.twelvedata.enabled,
                "api_key_configured": bool(self.twelvedata.api_key),
                "rate_limit": self.twelvedata.get_rate_limit_status(),
            },
            "finnhub": {
                "enabled": self.finnhub.enabled,
                "api_key_configured": bool(self.finnhub.api_key),
                "rate_limit": self.finnhub.get_rate_limit_status(),
            },
            "alphavantage": {
                "enabled": self.alphavantage.enabled,
                "api_key_configured": bool(self.alphavantage.api_key),
                "rate_limit": self.alphavantage.get_rate_limit_status(),
            },
        }


# ============================================================
#  全局单例
# ============================================================
_router: Optional[USMarketDataRouter] = None


def get_us_data_fetcher() -> USMarketDataRouter:
    """获取美股数据路由器单例"""
    global _router
    if _router is None:
        _router = USMarketDataRouter()
    return _router


def reset_fetcher():
    """重置单例 (用于测试)"""
    global _router
    _router = None
