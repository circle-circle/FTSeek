# FTSeek-US 数据源与限速优化设计

## 📊 当前架构分析

### 数据源现状

```
┌─────────────────────────────────────────────────────────────┐
│                    数据源架构 (三源)                           │
├─────────────────────────────────────────────────────────────┤
│  Primary:   TwelveData                                      │
│  - 技术指标 (SMA/RSI/MACD/布林带)                             │
│  - 历史K线 (1min/5min/15min/1day)                            │
│  - SPY/QQQ大盘数据                                           │
│  限速: 8 credits/min (免费计划)                               │
│  当前缓存: 技术指标60min, 报价?                               │
├─────────────────────────────────────────────────────────────┤
│  Secondary: Finnhub                                         │
│  - 实时报价 (quote)                                          │
│  - 公司新闻 + 情绪分析                                        │
│  限速: 60 calls/min (免费计划)                                │
│  当前缓存: 报价60s, 新闻2h                                    │
├─────────────────────────────────────────────────────────────┤
│  Fallback:  Alpha Vantage                                   │
│  - 备用报价                                                  │
│  - 真实VIX数据                                               │
│  限速: 5 calls/min, 500 calls/day (免费计划)                  │
│  当前缓存: VIX 10min, SPY 30min                               │
└─────────────────────────────────────────────────────────────┘
```

### 新闻源现状

```
┌─────────────────────────────────────────────────────────────┐
│                    新闻情绪系统                                │
├─────────────────────────────────────────────────────────────┤
│  主源: RSS Feed + Qwen LLM 情绪分析                           │
│  - WSJ Markets/World/Tech                                    │
│  - CNBC Markets/Finance/Asia                                 │
│  - Business Insider                                          │
│  缓存: 30分钟                                                 │
│  情绪分析: qwen-turbo (轻量模型)                               │
├─────────────────────────────────────────────────────────────┤
│  辅源: Finnhub 新闻 + 关键词规则                               │
│  - 公司新闻 (company_news)                                    │
│  - 情绪打分 (keyword-based)                                   │
│  缓存: 2小时                                                  │
├─────────────────────────────────────────────────────────────┤
│  融合策略: RSS(60%) + Finnhub(40%) 取加权平均                  │
└─────────────────────────────────────────────────────────────┘
```

---

## ⚠️ 当前问题诊断

### 1. **限速管理不统一**
- ❌ 各数据提供商各自实现限速,缺乏全局协调
- ❌ 缺少令牌桶算法的可视化监控
- ❌ 限速耗尽时缺少预警机制

### 2. **缓存策略待优化**
- ❌ 技术指标缓存60min过长,可能导致信号延迟
- ❌ 报价缓存时间不一致 (Finnhub 60s vs TwelveData ?)
- ❌ 新闻缓存30min可能错过突发新闻

### 3. **新闻源覆盖不足**
- ❌ RSS源固定,无法动态添加新源
- ❌ 缺少社交媒体情绪 (Twitter/X, Reddit)
- ❌ Finnhub新闻关键词规则过于简单

### 4. **数据获取效率低**
- ❌ 未实现批量请求 (逐个symbol调用)
- ❌ 重复获取相同数据 (投票/辩论/决策多次调用)
- ❌ 缺少预取机制 (盘前批量加载)

---

## 🎯 优化设计方案

### 方案1: 统一限速管理器

创建全局限速管理器,统一协调所有数据源:

```python
"""
统一限速管理器 - 令牌桶算法实现
"""
import time
import threading
from typing import Dict, Optional
from datetime import datetime

class RateLimiter:
    """令牌桶限速器"""
    
    def __init__(self, max_tokens: int, refill_interval: float):
        """
        Args:
            max_tokens: 桶容量 (最大调用次数)
            refill_interval: 补充间隔 (秒/token)
        """
        self.max_tokens = max_tokens
        self.tokens = max_tokens
        self.refill_interval = refill_interval
        self.last_refill = time.time()
        self.lock = threading.Lock()
        
        # 统计信息
        self.total_calls = 0
        self.total_wait_time = 0
        self.last_reset = datetime.now()
    
    def acquire(self, tokens: int = 1) -> bool:
        """获取令牌"""
        with self.lock:
            self._refill()
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                self.total_calls += 1
                return True
            else:
                # 等待令牌补充
                wait_time = (tokens - self.tokens) * self.refill_interval
                self.total_wait_time += wait_time
                time.sleep(wait_time)
                self._refill()
                self.tokens -= tokens
                self.total_calls += 1
                return True
    
    def _refill(self):
        """补充令牌"""
        now = time.time()
        elapsed = now - self.last_refill
        new_tokens = int(elapsed / self.refill_interval)
        
        if new_tokens > 0:
            self.tokens = min(self.max_tokens, self.tokens + new_tokens)
            self.last_refill = now
    
    def get_status(self) -> Dict:
        """获取限速状态"""
        return {
            "tokens_remaining": self.tokens,
            "max_tokens": self.max_tokens,
            "usage_pct": (1 - self.tokens / self.max_tokens) * 100,
            "total_calls": self.total_calls,
            "total_wait_time_sec": round(self.total_wait_time, 2),
            "last_reset": self.last_reset.strftime("%H:%M:%S"),
        }


class GlobalRateLimiter:
    """全局限速管理器"""
    
    def __init__(self):
        # TwelveData: 8 credits/min = 每7.5秒1个credit
        self.twelvedata = RateLimiter(
            max_tokens=8,
            refill_interval=7.5
        )
        
        # Finnhub: 60 calls/min = 每1秒1个call
        self.finnhub = RateLimiter(
            max_tokens=60,
            refill_interval=1.0
        )
        
        # Alpha Vantage: 5 calls/min = 每12秒1个call
        self.alphavantage = RateLimiter(
            max_tokens=5,
            refill_interval=12.0
        )
    
    def get_all_status(self) -> Dict:
        """获取所有限速器状态"""
        return {
            "twelvedata": self.twelvedata.get_status(),
            "finnhub": self.finnhub.get_status(),
            "alphavantage": self.alphavantage.get_status(),
        }
    
    def check_and_alert(self) -> List[str]:
        """检查并返回告警信息"""
        alerts = []
        status = self.get_all_status()
        
        for source, info in status.items():
            usage = info["usage_pct"]
            if usage > 90:
                alerts.append(f"⚠️ {source} 限速使用率 {usage:.0f}%, 剩余 {info['tokens_remaining']} tokens")
            elif usage > 75:
                alerts.append(f"ℹ️ {source} 限速使用率 {usage:.0f}%")
        
        return alerts


# 全局单例
_global_limiter: Optional[GlobalRateLimiter] = None

def get_rate_limiter() -> GlobalRateLimiter:
    """获取全局限速器"""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = GlobalRateLimiter()
    return _global_limiter
```

### 方案2: 智能缓存策略

根据数据类型和使用场景,优化缓存时间:

```python
"""
智能缓存管理器
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import threading

class SmartCache:
    """智能缓存管理器"""
    
    # 缓存TTL配置 (秒)
    CACHE_TTL = {
        # 高频数据 (实时交易需要)
        "quote": 30,              # 实时报价 30s (原60s)
        "orderbook": 10,          # 订单簿 10s
        
        # 中频数据 (决策分析需要)
        "technical_indicators": 300,  # 技术指标 5min (原60min)
        "kline_1m": 60,           # 1分钟K线 1min
        "kline_5m": 300,          # 5分钟K线 5min
        "kline_15m": 600,         # 15分钟K线 10min
        "kline_1d": 3600,         # 日线 1h
        
        # 低频数据 (市场环境)
        "vix": 600,               # VIX指数 10min
        "spy_context": 900,       # SPY大盘 15min
        "market_regime": 1800,    # 市场状态 30min
        
        # 新闻情绪
        "news_rss": 900,          # RSS新闻 15min (原30min)
        "news_finnhub": 3600,     # Finnhub新闻 1h (原2h)
        "sentiment_score": 1800,  # 情绪打分 30min
        
        # AI决策缓存
        "ai_decision": 300,       # AI决策结果 5min
        "voting_result": 300,     # 投票结果 5min
    }
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        with self._lock:
            if key not in self._cache:
                return None
            
            item = self._cache[key]
            now = datetime.now()
            
            # 检查是否过期
            if now > item["expires_at"]:
                del self._cache[key]
                return None
            
            return item["data"]
    
    def set(self, key: str, data: Any, ttl: Optional[int] = None, 
            data_type: str = "quote"):
        """设置缓存"""
        with self._lock:
            if ttl is None:
                ttl = self.CACHE_TTL.get(data_type, 300)
            
            self._cache[key] = {
                "data": data,
                "created_at": datetime.now(),
                "expires_at": datetime.now() + timedelta(seconds=ttl),
                "ttl": ttl,
            }
    
    def invalidate(self, pattern: str = None):
        """清除缓存"""
        with self._lock:
            if pattern is None:
                self._cache.clear()
            else:
                keys_to_delete = [k for k in self._cache if pattern in k]
                for key in keys_to_delete:
                    del self._cache[key]
    
    def get_stats(self) -> Dict:
        """获取缓存统计"""
        with self._lock:
            now = datetime.now()
            total = len(self._cache)
            expired = sum(1 for item in self._cache.values() 
                        if now > item["expires_at"])
            active = total - expired
            
            return {
                "total_entries": total,
                "active_entries": active,
                "expired_entries": expired,
                "memory_usage_kb": len(str(self._cache)) / 1024,
            }
```

### 方案3: 批量预取机制

在盘前批量加载所有数据,减少盘中API调用:

```python
"""
盘前数据预取器
"""
import asyncio
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor

class PreMarketDataFetcher:
    """盘前数据预取器"""
    
    def __init__(self, router, cache):
        self.router = router
        self.cache = cache
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    def prefetch_all(self, symbols: List[str]) -> Dict:
        """
        盘前预取所有数据
        
        Args:
            symbols: 股票列表 ["US.TSLA", "US.NVDA", ...]
        
        Returns:
            预取统计
        """
        stats = {
            "symbols_processed": 0,
            "cache_hits": 0,
            "api_calls_made": 0,
            "errors": 0,
        }
        
        # 1. 批量获取报价 (Finnhub批量接口)
        try:
            quotes = self._batch_get_quotes(symbols)
            stats["api_calls_made"] += 1
            stats["symbols_processed"] += len(quotes)
        except Exception as e:
            logger.error(f"批量获取报价失败: {e}")
            stats["errors"] += 1
        
        # 2. 批量获取技术指标 (TwelveData)
        try:
            for symbol in symbols:
                tech = self.router.get_technical_indicators(symbol)
                if tech:
                    stats["symbols_processed"] += 1
                    stats["api_calls_made"] += 1
        except Exception as e:
            logger.error(f"技术指标预取失败: {e}")
            stats["errors"] += 1
        
        # 3. 批量获取新闻 (RSS + Finnhub)
        try:
            for symbol in symbols:
                news = self.router.get_news_sentiment(symbol)
                if news:
                    stats["symbols_processed"] += 1
        except Exception as e:
            logger.error(f"新闻预取失败: {e}")
            stats["errors"] += 1
        
        # 4. 获取市场环境数据 (VIX, SPY)
        try:
            vix = self.router.get_vix()
            spy = self.router.get_spy_context()
            stats["api_calls_made"] += 2
        except Exception as e:
            logger.error(f"市场环境数据预取失败: {e}")
            stats["errors"] += 1
        
        return stats
    
    def _batch_get_quotes(self, symbols: List[str]) -> Dict:
        """批量获取报价 (使用Finnhub批量接口)"""
        # Finnhub支持批量查询,一次调用获取多个股票
        results = {}
        for symbol in symbols:
            quote = self.router.get_quote(symbol)
            if quote:
                results[symbol] = quote
        return results
```

### 方案4: 新闻源增强

添加更多新闻源和情绪分析:

```python
"""
增强版新闻提供商
"""

# 扩展RSS源
ENHANCED_RSS_FEEDS = {
    "market": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",            # WSJ World
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Markets
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # CNBC Finance
        "https://feeds.bloomberg.com/markets/news.rss",           # Bloomberg Markets (新增)
        "https://www.reutersfinance.com/rss/technologyNews",      # Reuters Tech (新增)
    ],
    "tech": [
        "https://feeds.a.dj.com/rss/WSJtechnology.rss",           # WSJ Tech
        "https://www.cnbc.com/id/100727362/device/rss/rss.html",  # CNBC Asia/Tech
        "https://techcrunch.com/feed/",                           # TechCrunch (新增)
        "https://www.wired.com/feed/rss",                         # Wired (新增)
    ],
    "crypto": [  # 新增加密货币
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    ],
}

# 扩展股票关键词
ENHANCED_SYMBOL_KEYWORDS = {
    # ... 原有映射 ...
    "US.MU": ["Micron", "MU", "memory chip", "DRAM", "HBM", "美光"],  # 新增
    "US.GOOG": ["Google", "GOOG", "GOOGL", "Alphabet", "Waymo"],  # 新增GOOG
}

# 社交媒体情绪 (可选,需要Twitter API)
SOCIAL_MEDIA_SOURCES = {
    "twitter": {
        "enabled": False,  # 需要Twitter API Key
        "endpoint": "https://api.twitter.com/2/tweets/search/recent",
        "keywords": ["$TSLA", "$NVDA", "$MU", "#AI", "#semiconductor"],
    },
    "reddit": {
        "enabled": True,  # Reddit API免费
        "subreddits": ["wallstreetbets", "stocks", "investing", "SecurityAnalysis"],
    },
}
```

---

## 📋 实施计划

### Phase 1: 统一限速管理 (优先级: P0)

**任务**:
1. 创建 `app/utils/rate_limiter.py` - 全局限速管理器
2. 修改 `twelvedata_provider.py` - 集成令牌桶
3. 修改 `finnhub_provider.py` - 集成令牌桶
4. 修改 `alphavantage_provider.py` - 集成令牌桶
5. 添加限速监控API端点

**预期效果**:
- ✅ 限速使用率可视化
- ✅ 自动等待令牌补充
- ✅ 限速耗尽前预警

**时间**: 2-3小时

### Phase 2: 智能缓存优化 (优先级: P0)

**任务**:
1. 创建 `app/utils/smart_cache.py` - 智能缓存管理器
2. 调整各数据类型缓存TTL
3. 添加缓存预热机制
4. 监控缓存命中率

**预期效果**:
- ✅ 技术指标从60min降至5min,信号更及时
- ✅ 新闻从30min降至15min,捕捉突发新闻
- ✅ 缓存命中率>80%,减少API调用

**时间**: 2-3小时

### Phase 3: 批量预取机制 (优先级: P1)

**任务**:
1. 创建 `app/data/premarket_fetcher.py` - 盘前预取器
2. 在scheduler中添加定时预取任务
3. 实现Finnhub批量报价接口
4. 优化TwelveData批量请求

**预期效果**:
- ✅ 盘前一次性加载所有数据
- ✅ 盘中减少70% API调用
- ✅ 决策响应时间<3秒

**时间**: 3-4小时

### Phase 4: 新闻源增强 (优先级: P1)

**任务**:
1. 扩展RSS源 (Bloomberg, Reuters, TechCrunch)
2. 添加Reddit情绪分析
3. 优化Qwen情绪分析prompt
4. 实现新闻去重和优先级排序

**预期效果**:
- ✅ 新闻覆盖率+50%
- ✅ 情绪分析准确率>85%
- ✅ 突发新闻延迟<10分钟

**时间**: 4-5小时

---

## 🎯 关键指标

### 限速优化
| 指标 | 当前 | 目标 |
|------|------|------|
| TwelveData使用率 | 未知 | <70% |
| Finnhub使用率 | 未知 | <60% |
| Alpha Vantage使用率 | 未知 | <40% |
| 限速告警 | 无 | 使用率>75%触发 |

### 缓存优化
| 指标 | 当前 | 目标 |
|------|------|------|
| 技术指标缓存 | 60min | 5min |
| 新闻缓存 | 30min | 15min |
| 报价缓存 | 60s | 30s |
| 缓存命中率 | 未知 | >80% |

### 性能优化
| 指标 | 当前 | 目标 |
|------|------|------|
| 单次决策API调用 | ~20次 | ~8次 |
| 决策响应时间 | ~15秒 | <5秒 |
| 盘前数据加载 | 无 | 1分钟内完成 |

---

## 💡 最佳实践

### 1. 数据获取策略

```python
# ✅ 推荐: 批量获取
symbols = ["US.TSLA", "US.NVDA", "US.MU"]
quotes = router.batch_get_quotes(symbols)  # 1次API调用

# ❌ 不推荐: 逐个获取
for symbol in symbols:
    quote = router.get_quote(symbol)  # 3次API调用
```

### 2. 缓存使用策略

```python
# ✅ 推荐: 优先读缓存
quote = cache.get(f"quote_{symbol}")
if not quote:
    quote = router.get_quote(symbol)
    cache.set(f"quote_{symbol}", quote, data_type="quote")

# ❌ 不推荐: 每次都调用API
quote = router.get_quote(symbol)  # 可能触发限速
```

### 3. 限速监控

```python
# ✅ 推荐: 定期检查限速状态
limiter = get_rate_limiter()
status = limiter.get_all_status()

for source, info in status.items():
    if info["usage_pct"] > 75:
        logger.warning(f"{source} 限速使用率过高: {info['usage_pct']}%")
```

---

## 🔐 安全与成本控制

### API Key管理
- ✅ 所有Key存储在 `.env.us`
- ✅ 不在代码中硬编码
- ✅ 定期轮换 (建议每季度)

### 成本控制
| 数据源 | 免费额度 | 预估月消耗 | 升级成本 |
|--------|---------|-----------|---------|
| TwelveData | 800 credits/day | ~500 credits | $29/月 (15k credits) |
| Finnhub | 60 calls/min | ~30k calls | $35/月 (180k calls) |
| Alpha Vantage | 500 calls/day | ~200 calls | $49.99/月 (750 calls) |
| Qwen LLM | 按量计费 | ~$5/月 | - |

**总成本**: ~$5-10/月 (当前) → ~$100/月 (升级后)

---

## 📊 监控告警

### 限速告警
```python
# 钉钉推送限速告警
def notify_rate_limit_alert(source: str, usage_pct: float):
    content = f"""
⚠️ 数据源限速告警

数据源: {source}
使用率: {usage_pct:.0f}%
剩余: {tokens_remaining} tokens
时间: {datetime.now().strftime('%H:%M')}

建议:
- 降低数据获取频率
- 增加缓存时间
- 考虑升级API计划
"""
    dingtalk.send_text(content)
```

### 缓存监控
```python
# 每小时检查缓存命中率
def check_cache_health():
    stats = cache.get_stats()
    hit_rate = stats["hit_rate"]
    
    if hit_rate < 70:
        logger.warning(f"缓存命中率过低: {hit_rate:.0f}%")
        # 调整缓存策略
```

---

**总结**: 通过统一限速管理、智能缓存优化、批量预取和新闻源增强,可以将API调用减少50%以上,同时提高数据新鲜度和决策响应速度。
