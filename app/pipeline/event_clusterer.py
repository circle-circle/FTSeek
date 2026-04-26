"""事件聚类模块

将清洗后的新闻条目按"关联标的 + 主题相似度"聚类成事件。
一个事件 = 多条新闻 (同事件多来源报道) → 后续合并为事件包。

聚类策略:
  Pass 1: 按 related_stocks 分组 (同一股票的新闻聚一类)
  Pass 2: 对无股票关联的新闻按主题关键词分组
  Merge:  合并重叠的聚类 (两个 cluster 有共同新闻)
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set, Any
from collections import defaultdict

logger = logging.getLogger("ftseek.pipeline.event_clusterer")


# ============================================================
#  事件聚类数据结构
# ============================================================
@dataclass
class EventCluster:
    """事件聚类: 一组相关新闻条目聚合为一个事件"""

    id: str                           # "EVT-YYYYMMDD-NNN"
    theme: str                        # 自动生成的主题描述
    category: str                     # earnings/ma/regulation/product/macro/geopolitical/analyst/market_move
    related_stocks: List[str] = field(default_factory=list)
    articles: List[Any] = field(default_factory=list)  # NewsItem list
    time_span_minutes: int = 0
    sentiment_consensus: float = 0.0  # 加权平均情绪分
    sentiment_dispersion: float = 0.0  # 情绪标准差
    source_count: int = 0             # 独立来源数
    quality_score: float = 0.0        # 聚类平均质量分
    first_seen: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    is_breaking: bool = False

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "theme": self.theme,
            "category": self.category,
            "related_stocks": self.related_stocks,
            "article_count": len(self.articles),
            "time_span_minutes": self.time_span_minutes,
            "sentiment_consensus": round(self.sentiment_consensus, 3),
            "sentiment_dispersion": round(self.sentiment_dispersion, 3),
            "source_count": self.source_count,
            "quality_score": round(self.quality_score, 3),
            "is_breaking": self.is_breaking,
            "first_seen": self.first_seen.isoformat(),
        }


@dataclass
class EventClusterer:
    """事件聚类引擎

    两阶段聚类:
      1. 按关联股票分组 (同一股票的新闻优先聚合)
      2. 对无股票关联的新闻按主题关键词分组
      3. 合并重叠聚类 + 主题生成 + 分类
    """

    config: Dict = field(default_factory=dict)
    time_window_minutes: int = 120     # 最大时间跨度
    min_cluster_size: int = 1          # 最小事件条数
    max_clusters: int = 10             # 最大聚类数
    theme_generation: str = "rule"     # "rule" 或 "llm"

    def __post_init__(self):
        self.config = self.config or {}
        self.time_window_minutes = self.config.get(
            "time_window_minutes", self.time_window_minutes
        )
        self.max_clusters = self.config.get("max_clusters", self.max_clusters)
        self.theme_generation = self.config.get(
            "theme_generation", self.theme_generation
        )

    def cluster(self, clean_news: List[Any]) -> List[EventCluster]:
        """对清洗后的新闻进行事件聚类

        Args:
            clean_news: 已清洗的新闻列表

        Returns:
            事件聚类列表 (按质量分降序, 最多 max_clusters 个)
        """
        if not clean_news:
            return []

        logger.info(f"[EventClusterer] 开始聚类: {len(clean_news)} 条新闻")

        # Pass 1: 按股票分组
        stock_clusters = self._cluster_by_stock(clean_news)

        # Pass 2: 对未归类的新闻按主题分组
        classified_ids = {nid for cl in stock_clusters.values() for nid in cl}
        unclassified = [n for n in clean_news if n.id not in classified_ids]
        topic_clusters = self._cluster_by_topic(unclassified) if unclassified else {}

        # 合并所有聚类
        all_clusters = {**stock_clusters, **topic_clusters}

        # 合并重叠聚类
        merged_clusters = self._merge_overlapping(all_clusters)

        # 构建 EventCluster 对象
        clusters = []
        for cluster_id, article_ids in merged_clusters.items():
            articles = [n for n in clean_news if n.id in article_ids]
            if len(articles) < self.min_cluster_size:
                continue

            cluster = self._build_event_cluster(cluster_id, articles)
            clusters.append(cluster)

        # 按质量分降序, 截断到 max_clusters
        clusters.sort(key=lambda c: c.quality_score, reverse=True)
        result = clusters[: self.max_clusters]

        logger.info(
            f"[EventClusterer] 聚类完成: {len(result)} 个事件 "
            f"(原始: {len(all_clusters)}, 合并后: {len(merged_clusters)})"
        )
        return result

    # ── 内部方法 ─────────────────────────────────────────────────

    def _cluster_by_stock(self, news: List[Any]) -> Dict[str, List[str]]:
        """按关联股票分组。每个股票一个 cluster, 一条新闻可能属于多个股票的 cluster。"""
        stock_groups: Dict[str, List[str]] = defaultdict(list)
        other_news: List[str] = []

        for n in news:
            stocks = getattr(n, "related_stocks", []) or []
            if stocks:
                for code in stocks:
                    stock_groups[code].append(n.id)
            else:
                other_news.append(n.id)

        # 有股票的 cluster 以股票代码命名
        return stock_groups

    def _cluster_by_topic(self, news: List[Any]) -> Dict[str, List[str]]:
        """对无股票关联的新闻按主题关键词分组"""
        # 使用内置关键词 (字符串 key, 避免循环依赖)
        raw_map = self._get_keywords()
        # 统一转为字符串 key
        keyword_map = {}
        for k, v in raw_map.items():
            key = k.value if hasattr(k, 'value') else str(k)
            keyword_map[key] = v

        topic_groups: Dict[str, List[str]] = defaultdict(list)
        uncategorized: List[str] = []

        for n in news:
            text = f"{getattr(n, 'title', '')} {getattr(n, 'content', '')}".lower()
            matched = False

            for topic, keywords in keyword_map.items():
                for kw in keywords:
                    if kw.lower() in text:
                        topic_groups[f"topic_{topic}"].append(n.id)
                        matched = True
                        break
                if matched:
                    break

            if not matched:
                uncategorized.append(n.id)

        # 剩余未分类的各自独立成组
        for uid in uncategorized:
            topic_groups[f"misc_{uid[:8]}"] = [uid]

        return topic_groups

    def _get_keywords(self) -> Dict:
        """复用 EventDetector.KEYWORDS"""
        try:
            from app.strategy.event_driven import EventType, EventDetector
            return EventDetector.KEYWORDS
        except ImportError:
            return self._get_default_keywords()

    def _get_default_keywords(self) -> Dict:
        """内置关键词 fallback (使用字符串 key 避免循环依赖)"""
        return {
            "earnings": ["财报", "业绩", "盈利", "亏损", "营收", "净利润", "earnings"],
            "ma": ["并购", "收购", "重组", "合并", "acquisition", "merger"],
            "regulation": ["监管", "政策", "调查", "处罚", "regulation"],
            "product": ["新产品", "发布", "技术", "专利", "launch"],
            "analyst": ["评级", "目标价", "增持", "减持", "upgrade", "downgrade"],
            "macro": ["GDP", "通胀", "利率", "美联储", "央行", "inflation"],
            "geopolitical": ["贸易战", "关税", "制裁", "地缘", "trade war"],
        }

    def _merge_overlapping(self, clusters: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """合并有重叠的聚类 (union-find)"""
        if not clusters:
            return {}

        # 构建新闻 ID 到 cluster 的映射
        news_to_cluster: Dict[str, str] = {}
        for cid, article_ids in clusters.items():
            for aid in article_ids:
                news_to_cluster[aid] = cid

        # 找出重叠: 如果一条新闻出现在多个 cluster 中, 合并这些 cluster
        overlap_map: Dict[str, Set[str]] = defaultdict(set)
        for n in news_to_cluster:
            cluster_id = news_to_cluster[n]
            overlap_map[cluster_id].add(cluster_id)

        # 简单合并: 对每个 cluster, 找到所有与之有交集的 cluster 并合并
        merged: Dict[str, Set[str]] = {}
        processed: Set[str] = set()

        for cid, article_ids in clusters.items():
            if cid in processed:
                continue

            union = set(article_ids)
            to_merge = [cid]
            processed.add(cid)

            # 找重叠的 cluster
            for aid in list(article_ids):
                other_cid = news_to_cluster.get(aid)
                if other_cid and other_cid != cid and other_cid not in processed:
                    union.update(clusters[other_cid])
                    processed.add(other_cid)
                    to_merge.append(other_cid)

            merged[f"merged_{'_'.join(sorted(to_merge)[:3])}"] = union

        return merged

    def _build_event_cluster(self, cluster_id: str, articles: List[Any]) -> EventCluster:
        """从文章列表构建 EventCluster 对象"""
        # 收集股票
        all_stocks: Set[str] = set()
        for a in articles:
            for s in getattr(a, "related_stocks", []) or []:
                all_stocks.add(s)

        # 计算时间跨度
        times = []
        for a in articles:
            pub = getattr(a, "published_at", "")
            if pub:
                try:
                    times.append(datetime.fromisoformat(pub.replace("Z", "+00:00")))
                except (ValueError, TypeError):
                    pass

        time_span = 0
        if len(times) >= 2:
            delta = max(times) - min(times)
            time_span = int(delta.total_seconds() / 60)

        # 情绪共识
        sentiments = [getattr(a, "sentiment_score", 0.0) for a in articles]
        if sentiments:
            consensus = sum(sentiments) / len(sentiments)
            variance = sum((s - consensus) ** 2 for s in sentiments) / len(sentiments)
            dispersion = variance ** 0.5
        else:
            consensus = 0.0
            dispersion = 0.0

        # 来源数
        sources = set(getattr(a, "source", "") for a in articles)

        # 质量分
        quality_scores = [getattr(a, "quality_score", 0.5) for a in articles]
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.5

        # 是否突发
        is_breaking = any(getattr(a, "is_breaking", False) for a in articles)

        # 分类
        category = self._assign_category(articles)

        # 主题生成
        theme = self._generate_theme(articles, category)

        # 生成 ID
        short_id = f"EVT-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

        return EventCluster(
            id=short_id,
            theme=theme,
            category=category,
            related_stocks=list(all_stocks),
            articles=articles,
            time_span_minutes=time_span,
            sentiment_consensus=consensus,
            sentiment_dispersion=dispersion,
            source_count=len(sources),
            quality_score=avg_quality,
            is_breaking=is_breaking,
            first_seen=min(times) if times else datetime.now(),
            updated_at=datetime.now(),
        )

    def _assign_category(self, articles: List[Any]) -> str:
        """基于关键词分配事件分类"""
        keyword_map = self._get_keywords()
        category_counts: Dict[str, int] = defaultdict(int)

        for a in articles:
            text = f"{getattr(a, 'title', '')} {getattr(a, 'content', '')}".lower()
            for cat, keywords in keyword_map.items():
                # 支持 enum 和 string key
                cat_key = cat.value if hasattr(cat, 'value') else str(cat)
                for kw in keywords:
                    if kw.lower() in text:
                        category_counts[cat_key] += 1
                        break

        if category_counts:
            return max(category_counts, key=category_counts.get)
        return "market_move"

    def _generate_theme(self, articles: List[Any], category: str) -> str:
        """生成事件主题描述

        rule 模式: 使用最高质量文章的标题
        llm 模式: 调用 LLM 生成 (暂未实现)
        """
        if not articles:
            return "未知事件"

        # 按质量分排序, 取最高质量的标题
        articles.sort(key=lambda a: getattr(a, "quality_score", 0.5), reverse=True)
        best_title = getattr(articles[0], "title", "")

        # 截断到合理长度
        if len(best_title) > 60:
            best_title = best_title[:57] + "..."

        # 如果有多来源, 添加来源数标注
        if len(articles) > 1:
            best_title += f" ({len(articles)} 条报道)"

        return best_title
