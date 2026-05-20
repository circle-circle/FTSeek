"""
Finnhub 行情 & 新闻情绪提供商

免费计划限速：60 次/分钟（约 1 次/s）
优势：实时报价（0 延迟）、公司新闻、情绪分析
文档：https://finnhub.io/docs/api

调用策略：
  - 令牌桶：60 令牌/分钟，每 1s 补充 1 个
  - 实时 quote：每只消耗 1 次
  - company_news：每只消耗 1 次（24h 内缓存）
  - VIX 用 TwelveData，Finnhub 不提供
"""
import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import requests

# P1 (2026-05-02): 抑制 urllib3 TLS 警告 (verify=False 是 Finnhub 免费 API 的已知问题)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env.us")

logger = logging.getLogger(__name__)

# 导入 API 限流监控
try:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from scripts.archive.api_limit_monitor import record_finnhub_call
    API_LIMIT_MONITOR_ENABLED = True
except Exception as e:
    logger.warning(f"[Finnhub] API 限流监控导入失败：{e}")
    API_LIMIT_MONITOR_ENABLED = False


BASE_URL = "https://finnhub.io/api/v1"
# 免费计划：60 次/分钟
FREE_CALLS_PER_MINUTE = 60
REFILL_INTERVAL_SEC = 60.0 / FREE_CALLS_PER_MINUTE  # 1s/call


class _RateLimiter:
    """令牌桶限速器（线程安全）"""

    def __init__(self, capacity: int, refill_interval_sec: float):
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_interval = refill_interval_sec
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return True
            wait = self._refill_interval * n
            if time.monotonic() + wait > deadline:
                logger.warning(f"[Finnhub] 限速等待超时 ({n} calls)")
                return False
            time.sleep(min(wait, 1.0))

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed / self._refill_interval
        if new_tokens >= 1:
            self._tokens = min(self._capacity, self._tokens + new_tokens)
            self._last_refill = now


# 预设股票情绪关键词（用于新闻评分）
_SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "US.AAPL":  ["Apple", "AAPL", "iPhone", "Tim Cook", "Mac"],
    "US.MSFT":  ["Microsoft", "MSFT", "Azure", "Satya Nadella"],
    "US.GOOGL": ["Google", "Alphabet", "GOOGL", "YouTube"],
    "US.AMZN":  ["Amazon", "AMZN", "AWS", "Andy Jassy"],
    "US.NVDA":  ["Nvidia", "NVDA", "GPU", "Jensen Huang", "AI chip"],
    "US.META":  ["Meta", "Facebook", "Instagram", "Zuckerberg"],
    "US.TSLA":  ["Tesla", "TSLA", "Elon Musk", "EV"],
    "US.BABA":  ["Alibaba", "BABA", "Taobao", "阿里"],
    "US.PDD":   ["PDD", "Pinduoduo", "Temu", "拼多多"],
    "US.JD":    ["JD.com", "JD", "京东"],
    "US.BIDU":  ["Baidu", "BIDU", "百度"],
    "US.NIO":   ["NIO", "蔚来"],
    "US.XPEV":  ["Xpeng", "小鹏"],
    "US.LI":    ["Li Auto", "理想汽车"],
}

# 情绪关键词词典（正负各20词）
_BULLISH_WORDS = [
    "beat", "surpass", "exceed", "record", "strong", "growth",
    "upgrade", "bullish", "rally", "surge", "outperform", "gain",
    "profit", "revenue", "expand", "breakthrough", "positive",
    "partnership", "acquisition", "innovation",
]
_BEARISH_WORDS = [
    "miss", "disappoint", "decline", "fall", "loss", "cut",
    "downgrade", "bearish", "plunge", "drop", "underperform",
    "warning", "risk", "layoff", "lawsuit", "fraud", "concern",
    "weak", "negative", "recall",
]


class FinnhubProvider:
    """
    Finnhub 行情 & 新闻情绪提供商（免费计划限速适配版）

    主要接口：
      get_quote(symbol)           → 实时报价（<1s 延迟）
      get_sentiment_score(symbol) → 新闻情绪分 [-1, 1]
      get_company_news(symbol)    → 最近 24h 新闻列表
      get_market_news_sentiment() → 大盘整体情绪
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_ttl_seconds: int = 60,
    ):
        """初始化 Finnhub 提供商
        
        Args:
            api_key: Finnhub API Key
            cache_ttl_seconds: 默认缓存TTL (秒), 默认60秒 (1分钟)
        """
        self.api_key = api_key or os.getenv("FINNHUB_API_KEY", "")
        self.cache_ttl = cache_ttl_seconds
        # 2026-04-20: 检测 *** 占位符, 视为无效 Key
        self.enabled = bool(self.api_key) and self.api_key != "***"
        
        if not self.enabled:
            logger.debug("[Finnhub] API Key 未配置或无效 (***), 服务已禁用")
        else:
            logger.info(f"[Finnhub] 初始化完成 | API Key=✅ | Cache TTL={cache_ttl_seconds}s")
        self._cache: Dict[str, Any] = {}
        self._cache_time: Dict[str, datetime] = {}

        # 免费计划：60 calls/分钟
        self._limiter = _RateLimiter(
            capacity=FREE_CALLS_PER_MINUTE,
            refill_interval_sec=REFILL_INTERVAL_SEC,
        )

    # ── 内部工具 ──────────────────────────────────────────────────

    def _is_cache_valid(self, key: str, ttl_override: Optional[int] = None) -> bool:
        if key not in self._cache:
            return False
        age = (datetime.now() - self._cache_time.get(key, datetime.min)).total_seconds()
        return age < (ttl_override if ttl_override is not None else self.cache_ttl)

    def _set_cache(self, key: str, value: Any):
        self._cache[key] = value
        self._cache_time[key] = datetime.now()

    def _to_ticker(self, symbol: str) -> str:
        """US.NVDA → NVDA"""
        return symbol.replace("US.", "").replace("HK.", "")

    def _get(self, endpoint: str, params: Dict) -> Optional[Any]:
        """GET 请求（含限速，Header-based auth 防止 token 泄露）"""
        if not self.api_key:
            return None
        if not self._limiter.acquire(n=1):
            return None
        # P1-8 修复 (2026-05-03): 使用 Header 传递 API Key，避免 token 出现在 URL 日志中
        headers = {"X-Finnhub-Token": self.api_key}
        try:
            resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, headers=headers, timeout=30, verify=False)
            if resp.status_code == 429:
                logger.warning("[Finnhub] 触发 429 限速，等待 5s")
                time.sleep(5)
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            # 日志脱敏：不输出完整的 URL（可能包含 query params）
            logger.error(f"[Finnhub] 请求失败 {endpoint}: {type(e).__name__} {resp.status_code if hasattr(e, 'response') and e.response else ''}")
            return None

    # ── 公开接口 ──────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时报价（消耗 1 call，缓存 TTL 自适应）。

        Finnhub /quote 返回：
          c = current price, o = open, h = high, l = low,
          pc = previous close, d = change, dp = change%

        缓存策略 (基于市场阶段):
          - 交易中 (09:30-16:00 ET):  缓存 60s
          - 盘前/盘后 (工作日非交易): 缓存 30min
          - 深夜/周末: 缓存 24h (完全不请求)
        """
        if not self.enabled:
            return None  # API Key 无效, 静默跳过
        
        cache_key = f"fh_quote_{symbol}"

        # 基于市场阶段的自适应 TTL
        # P0 修复 (2026-04-17): 修复跨项目 import 失败时 MarketPhase 未定义导致的 NameError
        phase_str = "OVERNIGHT"  # 默认值
        try:
            from app.core.scheduler_triggers import get_market_phase, MarketPhase
            phase = get_market_phase()
            phase_str = phase.value if hasattr(phase, 'value') else str(phase)
        except Exception:
            pass  # 使用默认 phase_str = "OVERNIGHT"

        if phase_str in ("WEEKEND",):
            effective_ttl = 86400  # 24h - 周末完全不休市，价格不变
        elif phase_str in ("OVERNIGHT",):
            effective_ttl = 86400  # 24h - 深夜也基本不变
        elif phase_str in ("PRE_MARKET", "AFTER_HOURS"):
            effective_ttl = 1800   # 30min - 盘前/盘后有波动可能
        else:
            effective_ttl = 60     # 60s - 交易中实时

        if self._is_cache_valid(cache_key, ttl_override=effective_ttl):
            return self._cache[cache_key]

        ticker = self._to_ticker(symbol)
        data = self._get("quote", {"symbol": ticker})
        if not data:
            return None

        # 记录 API 调用
        if API_LIMIT_MONITOR_ENABLED:
            record_finnhub_call(1)

        try:
            price = float(data.get("c") or 0)
            if price <= 0:
                logger.warning(f"[Finnhub] {symbol} 价格为 0")
                return None

            prev_close = float(data.get("pc") or price)
            change_pct = float(data.get("dp") or 0) / 100

            quote = {
                "symbol": symbol,
                "price": price,
                "open": float(data.get("o") or price),
                "high": float(data.get("h") or price),
                "low": float(data.get("l") or price),
                "previous_close": prev_close,
                "change_percent": change_pct,
                "volume": 0,   # /quote 不含成交量
                "timestamp": datetime.now().isoformat(),
                "source": "finnhub",
            }
            self._set_cache(cache_key, quote)
            # P1 (2026-05-02): 非交易时段降级日志级别，避免 INFO 刷屏
            if phase_str in ("WEEKEND", "OVERNIGHT"):
                logger.debug(f"[Finnhub] {symbol} ${price:.2f} ({change_pct:+.2%})")
            else:
                logger.info(f"[Finnhub] {symbol} ${price:.2f} ({change_pct:+.2%})")
            return quote
        except Exception as e:
            logger.error(f"[Finnhub] 解析报价失败 {symbol}: {e}")
            return None

    def get_company_news(self, symbol: str, days: int = 1) -> List[Dict]:
        """获取公司新闻"""
        if not self.enabled:
            return []
        """
        获取近 N 天公司新闻（消耗 1 call，缓存 2h）。
        """
        cache_key = f"fh_news_{symbol}_{days}"
        if self._is_cache_valid(cache_key, ttl_override=7200):
            return self._cache[cache_key]

        ticker = self._to_ticker(symbol)
        end_date   = datetime.utcnow().strftime("%Y-%m-%d")
        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

        data = self._get("company-news", {
            "symbol": ticker,
            "from": start_date,
            "to": end_date,
        })
        if data and isinstance(data, list):
            self._set_cache(cache_key, data)
            logger.debug(f"[Finnhub] {symbol} 获取到 {len(data)} 条新闻")
            return data
        return []

    def get_sentiment_score(self, symbol: str) -> float:
        """基于近期公司新闻的情绪评分 [-1, 1]"""
        if not self.enabled:
            return 0.0
        """
        基于近期公司新闻的情绪评分，范围 [-1, 1]。
        复用 company_news 缓存，不额外消耗 credits。
        """
        cache_key = f"fh_sentiment_{symbol}"
        if self._is_cache_valid(cache_key, ttl_override=3600):
            return self._cache.get(cache_key, 0.0)

        news_list = self.get_company_news(symbol, days=2)
        if not news_list:
            return 0.0

        keywords = _SYMBOL_KEYWORDS.get(symbol, [self._to_ticker(symbol)])
        score = 0.0
        count = 0

        for article in news_list[:20]:  # 最多分析最新20条
            text = (
                (article.get("headline") or "") + " " +
                (article.get("summary") or "")
            ).lower()

            # 过滤与标的无关的文章
            relevant = any(k.lower() in text for k in keywords)
            if not relevant:
                continue

            bull = sum(1 for w in _BULLISH_WORDS if w in text)
            bear = sum(1 for w in _BEARISH_WORDS if w in text)
            if bull + bear > 0:
                score += (bull - bear) / (bull + bear)
                count += 1

        result = max(-1.0, min(1.0, score / count)) if count > 0 else 0.0
        self._set_cache(cache_key, result)
        logger.debug(f"[Finnhub] {symbol} 情绪分={result:.2f} (分析{count}条新闻)")
        return result

    def get_market_news_summary(self, hours: int = 6) -> Dict[str, Any]:
        """
        获取大盘市场整体新闻情绪（消耗 1 call，缓存 2h）。
        """
        if not self.enabled:
            return {"sentiment": 0.0, "count": 0}
        cache_key = f"fh_market_news_{hours}"
        if self._is_cache_valid(cache_key, ttl_override=7200):
            return self._cache.get(cache_key, {"sentiment": 0.0, "count": 0})

        data = self._get("news", {"category": "general"})
        if not data or not isinstance(data, list):
            return {"sentiment": 0.0, "count": 0}

        score = 0.0
        count = 0
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        for article in data[:30]:
            ts = article.get("datetime", 0)
            if ts and datetime.utcfromtimestamp(ts) < cutoff:
                continue
            text = (
                (article.get("headline") or "") + " " +
                (article.get("summary") or "")
            ).lower()
            bull = sum(1 for w in _BULLISH_WORDS if w in text)
            bear = sum(1 for w in _BEARISH_WORDS if w in text)
            if bull + bear > 0:
                score += (bull - bear) / (bull + bear)
                count += 1

        result = {
            "sentiment": max(-1.0, min(1.0, score / count)) if count > 0 else 0.0,
            "count": count,
        }
        self._set_cache(cache_key, result)
        logger.info(f"[Finnhub] 大盘情绪={result['sentiment']:.2f} ({count}条新闻)")
        return result

    def get_vix_estimate(self) -> float:
        """
        Finnhub 不提供 VIX，返回默认 20.0。
        建议由 TwelveDataProvider.get_vix() 提供。
        """
        return 20.0

    def get_market_news(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取通用市场新闻"""
        if not self.enabled:
            return []
        """
        P0 新增 (2026-04-17): 获取原始市场新闻列表（用于 RSSHub 降级）
        
        Args:
            limit: 返回新闻数量限制
        
        Returns:
            新闻列表
        """
        cache_key = f"fh_market_news_raw"
        if self._is_cache_valid(cache_key, ttl_override=3600):
            return self._cache.get(cache_key, [])

        data = self._get("news", {"category": "general"})
        if not data or not isinstance(data, list):
            return []

        news = []
        for article in data[:limit]:
            news.append({
                "headline": article.get("headline", ""),
                "summary": article.get("summary", ""),
                "url": article.get("url", ""),
                "datetime": article.get("datetime", 0),
                "source": article.get("source", ""),
                "relatedSymbols": article.get("relatedSymbols", []),
            })
        
        self._set_cache(cache_key, news)
        return news

