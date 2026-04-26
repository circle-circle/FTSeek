"""新闻清洗与去重模块

核心职责:
1. 模糊标题去重 (rapidfuzz, 阈值可配置)
2. 质量评分与低质量过滤 (来源可信度 + 内容长度 + 实质检测)
3. 垃圾新闻过滤 (标题党/广告/无实质内容)
4. 内容规范化 (HTML 清理 + 编码统一 + 长度截断)
5. 增强股票实体映射 (扩展 STOCK_ALIASES + 正则匹配股票代码)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Any

logger = logging.getLogger("ftseek.pipeline.news_cleaner")


# ============================================================
#  标题党/垃圾新闻模式
# ============================================================
CLICKBAIT_PATTERNS = [
    r"^震惊[！!]", r"^重磅[！!]", r"^突发[！!]",
    r"^速看[！!]", r"^刚刚[！!]", r"^爆了[！!]",
    r"点击查看", r"点击阅读全文",
    r"^\[广告\]", r"^\[推广\]",
    r"限时.*优惠", r"免费.*领",
]

GARBAGE_PATTERNS = [
    r"^[A-Za-z0-9]{20,}$",    # 随机字符串
    r"^.{0,3}$",                # 极短标题
    r"(.*重复.*){3,}",          # 过度重复词汇
]

# 可信来源白名单 (高可信度来源质量加分)
TRUSTED_SOURCES = {
    "cls": 0.95,            # 财联社
    "wallstreetcn": 0.90,   # 华尔街见闻
    "sec": 0.95,            # SEC 官方
    "fed": 0.95,            # 美联储
    "cnbc": 0.85,
    "cnbc_asia": 0.85,
    "wsj": 0.90,
    "reuters": 0.90,
    "hkex": 0.95,           # 港交所
    "blockbeats": 0.70,
    "sina": 0.75,
    "eastmoney": 0.75,
    "aastocks": 0.80,
    "scmp": 0.80,
    "futu_rsshub": 0.70,
    "rss": 0.50,            # 未知 RSS 源默认低分
}


@dataclass
class QualityScorer:
    """新闻质量评分器

    综合多维度信号给出 0.0-1.0 质量分。
    """

    source_weights: Dict[str, float] = field(default_factory=lambda: TRUSTED_SOURCES)
    min_content_length: int = 8
    max_content_length: int = 2000

    def score(self, news: Any) -> float:
        """对单条新闻进行质量评分 (0.0-1.0)"""
        scores = []

        # 1. 来源可信度 (权重 0.35)
        source_score = self._source_credibility(news)
        scores.append(("source", source_score, 0.35))

        # 2. 内容长度合理性 (权重 0.20)
        length_score = self._length_score(news)
        scores.append(("length", length_score, 0.20))

        # 3. 实质内容检测 (权重 0.25)
        substance_score = self._has_substance(news)
        scores.append(("substance", substance_score, 0.25))

        # 4. 已有情绪信号强度 (权重 0.10)
        sentiment_signal = self._sentiment_signal_strength(news)
        scores.append(("sentiment", sentiment_signal, 0.10))

        # 5. 股票实体匹配 (权重 0.10)
        entity_score = self._entity_match_score(news)
        scores.append(("entity", entity_score, 0.10))

        # 加权求和
        quality = sum(s * w for _, s, w in scores)
        return round(min(1.0, max(0.0, quality)), 3)

    def _source_credibility(self, news: Any) -> float:
        source = getattr(news, "source", "").lower()
        return self.source_weights.get(source, 0.40)

    def _length_score(self, news: Any) -> float:
        title_len = len(getattr(news, "title", ""))
        content_len = len(getattr(news, "content", "") or getattr(news, "summary", ""))

        if title_len < self.min_content_length:
            return 0.0
        if content_len < 20:
            return 0.3
        if content_len > self.max_content_length:
            return 0.8
        # 适中长度最佳
        return min(1.0, content_len / 200)

    def _has_substance(self, news: Any) -> float:
        title = getattr(news, "title", "").strip()
        content = getattr(news, "content", "") or getattr(news, "summary", "")

        if not title:
            return 0.0

        # 标题党检测
        for pattern in CLICKBAIT_PATTERNS:
            if re.search(pattern, title):
                return 0.1

        # 垃圾模式检测
        for pattern in GARBAGE_PATTERNS:
            if re.search(pattern, title):
                return 0.05

        # 有实质内容加分
        if content and len(content.strip()) > 30:
            return 0.9
        if content and len(content.strip()) > 10:
            return 0.6
        return 0.4

    def _sentiment_signal_strength(self, news: Any) -> float:
        """已有 sentiment_score 的绝对值作为信号强度参考"""
        score = getattr(news, "sentiment_score", 0.0)
        return min(1.0, abs(score) + 0.3)  # 有情绪信号至少 0.3

    def _entity_match_score(self, news: Any) -> float:
        """匹配到具体股票实体加分"""
        stocks = getattr(news, "related_stocks", [])
        if len(stocks) >= 2:
            return 1.0
        if len(stocks) == 1:
            return 0.7
        return 0.2


@dataclass
class NewsCleaner:
    """新闻清洗流水线

    处理流程:
      1. 内容规范化 (HTML 清理, 编码统一)
      2. 模糊去重 (rapidfuzz 标题相似度)
      3. 点击标题党/垃圾新闻过滤
      4. 质量评分
      5. 增强股票实体映射
    """

    config: Dict = field(default_factory=dict)
    dedup_threshold: float = 0.85
    min_content_length: int = 8
    max_content_length: int = 2000
    filter_clickbait: bool = True

    def __post_init__(self):
        self.config = self.config or {}
        self.dedup_threshold = self.config.get("dedup_threshold", self.dedup_threshold)
        self.min_content_length = self.config.get(
            "min_content_length", self.min_content_length
        )
        self.max_content_length = self.config.get(
            "max_content_length", self.max_content_length
        )
        self.filter_clickbait = self.config.get(
            "filter_clickbait", self.filter_clickbait
        )

        self._quality_scorer = QualityScorer(
            min_content_length=self.min_content_length,
            max_content_length=self.max_content_length,
        )

        # 延迟加载 rapidfuzz
        self._rapidfuzz_available = False
        try:
            from rapidfuzz import fuzz
            self._rapidfuzz_available = True
        except ImportError:
            logger.warning(
                "rapidfuzz 未安装，模糊去重将降级为精确匹配。"
                "运行: pip install rapidfuzz"
            )

    def clean(self, raw_news: List[Any]) -> List[Any]:
        """清洗新闻列表

        Args:
            raw_news: 原始新闻列表 (NewsItem 或 dict-like)

        Returns:
            清洗后的新闻列表 (按质量分降序)
        """
        if not raw_news:
            return []

        logger.info(f"[NewsCleaner] 开始清洗: {len(raw_news)} 条原始新闻")
        stats = {"input": len(raw_news)}

        # Stage 1: 内容规范化
        news = [self._normalize_content(n) for n in raw_news]

        # Stage 2: 模糊去重
        news = self._deduplicate_fuzzy(news)
        stats["after_dedup"] = len(news)

        # Stage 3: 标题党/垃圾过滤
        if self.filter_clickbait:
            news = self._remove_clickbait_and_garbage(news)
            stats["after_filter"] = len(news)

        # Stage 4: 质量评分 + 低质量过滤
        scored_news = []
        for n in news:
            q = self._quality_scorer.score(n)
            # 质量分 < 0.15 的视为噪音
            if q >= 0.15:
                n.quality_score = q
                scored_news.append(n)
        stats["after_quality"] = len(scored_news)

        # Stage 5: 增强股票映射
        scored_news = [self._enrich_stock_mapping(n) for n in scored_news]

        # 按质量分降序排列
        scored_news.sort(key=lambda x: getattr(x, "quality_score", 0.5), reverse=True)

        logger.info(
            f"[NewsCleaner] 清洗完成: {stats['input']} -> {len(scored_news)} 条 "
            f"(去重: {stats.get('after_dedup', 'N/A')}, "
            f"过滤: {stats.get('after_filter', 'N/A')}, "
            f"质量: {stats.get('after_quality', 'N/A')})"
        )
        return scored_news

    # ── 内部方法 ─────────────────────────────────────────────────

    def _normalize_content(self, news: Any) -> Any:
        """内容规范化: HTML 清理 + 编码统一 + 截断"""
        if hasattr(news, "content") and news.content:
            # 清理 HTML 标签
            cleaned = re.sub(r"<[^>]+>", "", news.content)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            news.content = cleaned[: self.max_content_length]

        if hasattr(news, "summary") and news.summary:
            cleaned = re.sub(r"<[^>]+>", "", news.summary)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            news.summary = cleaned[:500]

        if hasattr(news, "title") and news.title:
            news.title = re.sub(r"\s+", " ", news.title).strip()

        return news

    def _deduplicate_fuzzy(self, news: List[Any]) -> List[Any]:
        """模糊去重: 标题相似度 >= threshold 视为重复"""
        if not news:
            return []

        if self._rapidfuzz_available:
            return self._dedup_with_rapidfuzz(news)
        else:
            return self._dedup_exact(news)

    def _dedup_with_rapidfuzz(self, news: List[Any]) -> List[Any]:
        from rapidfuzz import fuzz

        unique = []
        seen_titles: List[str] = []

        for n in news:
            title = getattr(n, "title", "")
            if not title:
                continue

            is_duplicate = False
            for seen in seen_titles:
                ratio = fuzz.ratio(title, seen) / 100.0
                if ratio >= self.dedup_threshold:
                    is_duplicate = True
                    break

            if not is_duplicate:
                seen_titles.append(title)
                unique.append(n)

        return unique

    def _dedup_exact(self, news: List[Any]) -> List[Any]:
        """降级方案: 精确标题去重"""
        seen: Set[str] = set()
        unique = []
        for n in news:
            title = getattr(n, "title", "")
            if title and title not in seen:
                seen.add(title)
                unique.append(n)
        return unique

    def _remove_clickbait_and_garbage(self, news: List[Any]) -> List[Any]:
        """过滤标题党和垃圾新闻"""
        filtered = []
        for n in news:
            title = getattr(n, "title", "")
            if not title:
                continue

            is_clickbait = False
            for pattern in CLICKBAIT_PATTERNS:
                if re.search(pattern, title):
                    is_clickbait = True
                    break

            is_garbage = False
            for pattern in GARBAGE_PATTERNS:
                if re.search(pattern, title):
                    is_garbage = True
                    break

            if not is_clickbait and not is_garbage:
                filtered.append(n)
            else:
                logger.debug(f"[NewsCleaner] 过滤: {title[:40]}")

        return filtered

    def _enrich_stock_mapping(self, news: Any) -> Any:
        """增强股票实体映射"""
        text = f"{getattr(news, 'title', '')} {getattr(news, 'content', '')}"

        # 使用 STOCK_ALIASES 做映射 (复用现有配置)
        try:
            from app.news_push.config import STOCK_ALIASES

            matched = set()
            for code, aliases in STOCK_ALIASES.items():
                for alias in aliases:
                    if alias.lower() in text.lower():
                        matched.add(code)
                        break

            # 合并到 existing related_stocks
            existing = set(getattr(news, "related_stocks", []) or [])
            news.related_stocks = list(existing | matched)

            # 额外: 用正则匹配 HK.XXXXX 格式的代码
            hk_pattern = re.findall(r"HK\.(\d{5})", text)
            for code in hk_pattern:
                full_code = f"HK.{code}"
                if full_code not in news.related_stocks:
                    news.related_stocks.append(full_code)

        except ImportError:
            logger.debug("[NewsCleaner] STOCK_ALIASES 未找到，跳过增强映射")

        return news
