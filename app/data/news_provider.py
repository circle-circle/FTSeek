"""
美股新闻情绪提供商

基于 RSS 抓取 + Qwen LLM 情绪分析
复用 FTSeek-HK news_push 链路思路
"""
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import feedparser

from app.llm.unified_service import UnifiedLLMService
from app.utils.timezone import now_utc

logger = logging.getLogger(__name__)


# 美股相关 RSS 源（已验证可访问）
RSS_FEEDS = {
    "market": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",            # WSJ World
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Markets
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # CNBC Finance
    ],
    "tech": [
        "https://feeds.a.dj.com/rss/WSJtechnology.rss",           # WSJ Tech
        "https://www.cnbc.com/id/100727362/device/rss/rss.html",  # CNBC Asia/Tech
    ],
    "us_economy": [
        "https://feeds.feedburner.com/typepad/alleyinsider/silicon_alley_insider",
    ],
}

# 股票关键词映射（用于过滤相关新闻）
SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "US.AAPL": ["Apple", "AAPL", "iPhone", "Tim Cook", "Mac", "iPad"],
    "US.MSFT": ["Microsoft", "MSFT", "Windows", "Azure", "Satya Nadella"],
    "US.GOOGL": ["Google", "GOOGL", "Alphabet", "YouTube", "Android"],
    "US.AMZN": ["Amazon", "AMZN", "AWS", "Jeff Bezos", "Andy Jassy"],
    "US.NVDA": ["Nvidia", "NVDA", "GPU", "Jensen Huang", "AI chip"],
    "US.META": ["Meta", "META", "Facebook", "Instagram", "Mark Zuckerberg"],
    "US.TSLA": ["Tesla", "TSLA", "Elon Musk", "EV", "electric vehicle"],
    "US.BABA": ["Alibaba", "BABA", "Jack Ma", "Taobao", "Tmall", "阿里巴巴"],
    "US.JD":   ["JD.com", "JD", "京东"],
    "US.PDD":  ["PDD", "Pinduoduo", "Temu", "拼多多"],
    "US.BIDU": ["Baidu", "BIDU", "百度"],
    "US.NIO":  ["NIO", "蔚来"],
    "US.XPEV": ["Xpeng", "XPEV", "小鹏"],
    "US.LI":   ["Li Auto", "LI", "理想汽车"],
    "US.SPY":  ["S&P 500", "SPX", "market rally", "stock market"],
    "US.QQQ":  ["Nasdaq", "QQQ", "tech stocks"],
}

# 正面/负面关键词（用于规则 fallback）
POSITIVE_KEYWORDS = [
    "surge", "rally", "gain", "beat", "exceed", "record high", "upgrade",
    "bullish", "growth", "profit", "revenue beat", "strong", "outperform",
    "buy", "opportunity", "breakout", "momentum", "above estimate",
]
NEGATIVE_KEYWORDS = [
    "crash", "drop", "fall", "miss", "below", "downgrade", "bearish",
    "loss", "decline", "warning", "cut", "reduce", "selloff", "concern",
    "risk", "below estimate", "layoff", "investigation", "penalty",
]


class USNewsProvider:
    """美股新闻情绪提供商"""

    # 新闻缓存 TTL（秒）
    CACHE_TTL = 900  # 15 分钟 (原30分钟,优化为更及时)

    def __init__(self):
        self._news_cache: Dict[str, Tuple[datetime, List[str]]] = {}
        self._sentiment_cache: Dict[str, Tuple[datetime, float]] = {}

        # LLM 情绪分析已禁用: 投票系统已覆盖情绪分析, 避免冗余调用
        self.qwen_enabled = False

    def fetch_recent_news(self, symbol: str, hours: int = 12) -> List[str]:
        """
        获取最近 N 小时的相关新闻标题列表

        Args:
            symbol: 股票代码，如 "US.AAPL"
            hours: 时间窗口（小时）

        Returns:
            新闻标题列表
        """
        cache_key = f"{symbol}_{hours}"
        if cache_key in self._news_cache:
            ts, headlines = self._news_cache[cache_key]
            if (datetime.now() - ts).total_seconds() < self.CACHE_TTL:
                return headlines

        keywords = SYMBOL_KEYWORDS.get(symbol, [symbol.replace("US.", "")])
        headlines: List[str] = []
        cutoff = now_utc().replace(tzinfo=None) - timedelta(hours=hours)  # feedparser 返回 UTC 时间

        for category, urls in RSS_FEEDS.items():
            for url in urls:
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:30]:
                        title = entry.get("title", "")
                        summary = entry.get("summary", "")
                        content = f"{title} {summary}"

                        # 时间过滤（published_parsed 是 UTC struct_time）
                        published = entry.get("published_parsed")
                        if published:
                            pub_dt = datetime(*published[:6])  # 已是 UTC naive
                            if pub_dt < cutoff:
                                continue

                        # 关键词匹配
                        if any(kw.lower() in content.lower() for kw in keywords):
                            if title and title not in headlines:
                                headlines.append(title)
                except Exception as e:
                    logger.debug(f"RSS抓取失败 {url}: {e}")

        # 去重 + 限制数量
        headlines = list(dict.fromkeys(headlines))[:20]
        self._news_cache[cache_key] = (datetime.now(), headlines)
        logger.info(f"[新闻] {symbol} 近{hours}h 找到 {len(headlines)} 条相关新闻")
        return headlines

    def get_sentiment_score(self, symbol: str) -> float:
        """
        获取情绪分（-1.0 ~ +1.0）

        优先使用 Qwen LLM 分析，失败时 fallback 到关键词规则
        """
        # 检查缓存
        if symbol in self._sentiment_cache:
            ts, score = self._sentiment_cache[symbol]
            if (datetime.now() - ts).total_seconds() < self.CACHE_TTL:
                return score

        headlines = self.fetch_recent_news(symbol)
        if not headlines:
            logger.debug(f"[新闻] {symbol} 无相关新闻，情绪分=0.0")
            return 0.0

        if self.qwen_enabled:
            score = self._analyze_with_qwen(symbol, headlines)
        else:
            score = self._analyze_with_rules(headlines)

        score = max(-1.0, min(1.0, score))
        self._sentiment_cache[symbol] = (datetime.now(), score)
        logger.info(f"[新闻情绪] {symbol} = {score:.2f} (共{len(headlines)}条标题)")
        return score

    def _analyze_with_qwen(self, symbol: str, headlines: List[str]) -> float:
        """调用 LLM 分析情绪 (SSOT 路由)"""
        try:
            headlines_text = "\n".join(f"- {h}" for h in headlines[:10])
            prompt = f"""请分析以下关于 {symbol} 的新闻标题，判断整体市场情绪。

新闻标题：
{headlines_text}

请返回一个 JSON 对象，包含：
- sentiment_score: -1.0 到 1.0 之间的小数（-1.0=极度悲观, 0=中性, 1.0=极度乐观）
- confidence: 0.0 到 1.0 的置信度
- summary: 一句话总结情绪原因（20字以内）

只返回 JSON，不要其他文字：{{"sentiment_score": <数值>, "confidence": <数值>, "summary": "<文字>"}}"""

            res = UnifiedLLMService.call(
                "sentiment",
                prompt,
                system="你是专业的金融情绪分析师，请用中立客观的态度分析新闻情绪。",
                temperature=0.1,
                max_tokens=150,
                json_mode=True,
                timeout=15,
            )
            if not res or not res.success:
                logger.warning(f"[LLM情绪] {symbol} 调用失败，使用规则fallback")
                return self._analyze_with_rules(headlines)

            data = res.response if isinstance(res.response, dict) else json.loads(res.response)
            score = float(data.get("sentiment_score", 0))
            logger.debug(f"[LLM情绪] {symbol} score={score:.2f} summary={data.get('summary','')}")
            return score
        except Exception as e:
            logger.warning(f"[LLM情绪] {symbol} 解析失败，使用规则fallback: {e}")

        return self._analyze_with_rules(headlines)

    def _analyze_with_rules(self, headlines: List[str]) -> float:
        """基于关键词规则计算情绪分"""
        if not headlines:
            return 0.0

        pos_count = 0
        neg_count = 0
        text = " ".join(headlines).lower()

        for kw in POSITIVE_KEYWORDS:
            pos_count += text.count(kw.lower())
        for kw in NEGATIVE_KEYWORDS:
            neg_count += text.count(kw.lower())

        total = pos_count + neg_count
        if total == 0:
            return 0.0

        # 归一化到 -1 ~ 1
        score = (pos_count - neg_count) / total
        return round(score, 3)

    def get_market_news_summary(self, hours: int = 6) -> Dict[str, Any]:
        """
        获取整体市场新闻摘要（非个股）

        Returns:
            {"sentiment": float, "top_headlines": List[str], "headline_count": int}
        """
        all_headlines: List[str] = []
        cutoff = now_utc().replace(tzinfo=None) - timedelta(hours=hours)  # feedparser 返回 UTC 时间

        for category in ["market"]:
            for url in RSS_FEEDS.get(category, []):
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:20]:
                        title = entry.get("title", "")
                        published = entry.get("published_parsed")
                        if published:
                            pub_dt = datetime(*published[:6])
                            if pub_dt < cutoff:
                                continue
                        if title:
                            all_headlines.append(title)
                except Exception as e:
                    logger.debug(f"RSS抓取失败 {url}: {e}")

        all_headlines = list(dict.fromkeys(all_headlines))[:20]
        sentiment = self._analyze_with_rules(all_headlines) if all_headlines else 0.0

        return {
            "sentiment": sentiment,
            "top_headlines": all_headlines[:5],
            "headline_count": len(all_headlines),
            "timestamp": datetime.now().isoformat(),
        }
