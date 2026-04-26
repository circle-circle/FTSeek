"""事件包构造模块

将评分后的事件聚类组装为结构化的"事件包"，供 LLM 分析使用。

事件包包含:
  - 精选文章摘要 (最多 max_articles_per_pack 篇)
  - 关联股票列表
  - 多维度相关性评分
  - 市场上下文 (VIX, 美股收盘, 港股盘前)
  - 时效标签
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger("ftseek.pipeline.event_pack_builder")


@dataclass
class EventPack:
    """结构化的事件包，包含 LLM 分析所需的全部上下文"""

    id: str                           # "EP-YYYYMMDD-NNNN"
    cluster_id: str
    theme: str
    category: str
    focus_symbols: List[str]          # 重点关注股票
    relevance: Dict[str, float]       # 多维度相关性评分
    articles_summary: List[Dict]      # [{title, summary, source, sentiment, quality}]
    sentiment_summary: Dict           # {consensus, dispersion, source_count}
    market_context: Dict              # {vix, us_close_changes, hsi_preopen}
    time_to_open_minutes: int = 0     # 距开盘分钟数
    priority: str = "medium"          # critical/high/medium/low
    trace_id: str = ""

    def to_llm_context(self) -> str:
        """格式化为 LLM 可读的文本段落"""
        parts = []
        parts.append(f"## 事件包: {self.theme}")
        parts.append(f"分类: {self.category} | 优先级: {self.priority}")
        parts.append(f"关联标的: {', '.join(self.focus_symbols) or '无具体标的'}")
        parts.append(f"相关性: {self.relevance.get('overall', 0):.2f}")
        parts.append("")

        parts.append("### 新闻摘要")
        for i, art in enumerate(self.articles_summary, 1):
            parts.append(
                f"{i}. [{art.get('source', '?')}] {art.get('title', '')} "
                f"(情绪: {art.get('sentiment', 0):+.2f}, 质量: {art.get('quality', 0):.2f})"
            )
            if art.get("summary"):
                parts.append(f"   {art.get('summary')}")

        if self.market_context:
            parts.append("")
            parts.append("### 市场上下文")
            if "vix" in self.market_context:
                parts.append(f"VIX: {self.market_context['vix']}")
            if "spy_change" in self.market_context:
                parts.append(f"SPY: {self.market_context['spy_change']:+.2f}%")
            if "qqq_change" in self.market_context:
                parts.append(f"QQQ: {self.market_context['qqq_change']:+.2f}%")

        parts.append("")
        return "\n".join(parts)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "cluster_id": self.cluster_id,
            "theme": self.theme,
            "category": self.category,
            "focus_symbols": self.focus_symbols,
            "relevance": self.relevance,
            "article_count": len(self.articles_summary),
            "sentiment_summary": self.sentiment_summary,
            "market_context": self.market_context,
            "time_to_open_minutes": self.time_to_open_minutes,
            "priority": self.priority,
            "trace_id": self.trace_id,
        }


@dataclass
class EventPackBuilder:
    """事件包构造器

    将 EventCluster + RelevanceScores 转换为 LLM 可读的 EventPack。
    """

    config: Dict = field(default_factory=dict)
    max_articles_per_pack: int = 5
    include_market_context: bool = True

    def __post_init__(self):
        self.config = self.config or {}
        self.max_articles_per_pack = self.config.get(
            "max_articles_per_pack", self.max_articles_per_pack
        )
        self.include_market_context = self.config.get(
            "include_market_context", self.include_market_context
        )

    def build(self, cluster: Any, relevance: Dict[str, float]) -> EventPack:
        """从单个事件聚类构建事件包"""
        articles = getattr(cluster, "articles", []) or []

        # 精选文章: 按 quality_score * recency 排序, 取 top N
        sorted_articles = self._sort_articles(articles)
        top_articles = sorted_articles[: self.max_articles_per_pack]

        # 聚焦标的
        focus_symbols = getattr(cluster, "related_stocks", []) or []

        # 市场上下文
        market_ctx = self._gather_market_context() if self.include_market_context else {}

        # 时效标签
        time_to_open = self._time_to_open_minutes()
        priority = self._assign_priority(cluster, relevance)

        # 生成 ID
        pack_id = f"EP-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

        return EventPack(
            id=pack_id,
            cluster_id=getattr(cluster, "id", ""),
            theme=getattr(cluster, "theme", ""),
            category=getattr(cluster, "category", ""),
            focus_symbols=focus_symbols,
            relevance=relevance,
            articles_summary=self._format_articles(top_articles),
            sentiment_summary={
                "consensus": getattr(cluster, "sentiment_consensus", 0.0),
                "dispersion": getattr(cluster, "sentiment_dispersion", 0.0),
                "source_count": getattr(cluster, "source_count", 0),
            },
            market_context=market_ctx,
            time_to_open_minutes=time_to_open,
            priority=priority,
            trace_id=f"EPK-{uuid.uuid4().hex[:8].upper()}",
        )

    def build_batch(
        self, clusters: List[Any], relevance_scores: List[Dict[str, float]]
    ) -> List[EventPack]:
        """批量构建事件包"""
        packs = []
        for cluster, scores in zip(clusters, relevance_scores):
            pack = self.build(cluster, scores)
            packs.append(pack)

        logger.info(f"[EventPackBuilder] 构建 {len(packs)} 个事件包")
        return packs

    # ── 内部方法 ─────────────────────────────────────────────────

    def _sort_articles(self, articles: List[Any]) -> List[Any]:
        """按质量分 * 时效性排序"""
        def score_fn(a):
            quality = getattr(a, "quality_score", 0.5)
            age = getattr(a, "age_minutes", 0) or 0
            # 时效性: 30 分钟内满分, 超过快速衰减
            recency = max(0.1, 1.0 - age / 480)
            return quality * recency

        return sorted(articles, key=score_fn, reverse=True)

    def _format_articles(self, articles: List[Any]) -> List[Dict]:
        """格式化文章为摘要列表"""
        result = []
        for a in articles:
            result.append({
                "title": getattr(a, "title", ""),
                "summary": (getattr(a, "summary", "") or getattr(a, "content", ""))[:200],
                "source": getattr(a, "source", ""),
                "sentiment": getattr(a, "sentiment_score", 0.0),
                "quality": getattr(a, "quality_score", 0.5),
                "url": getattr(a, "url", ""),
            })
        return result

    def _gather_market_context(self) -> Dict:
        """获取跨市场上下文"""
        ctx = {}

        # 尝试从现有模块获取市场数据
        try:
            # 尝试获取 LLM 市场分析师的缓存跨市场数据
            from app.ai.llm_market_analyst import CrossMarketContext
            ctx["_note"] = "market context placeholder - integrate with data feed"
        except ImportError:
            pass

        # 简化实现: 标记需要接入实时数据
        ctx["available"] = False
        ctx["note"] = "待接入实时市场数据 (VIX/SPY/QQQ/HSI)"

        return ctx

    def _time_to_open_minutes(self) -> int:
        """计算距港股开盘的分钟数 (港股 09:30 HKT)"""
        now = datetime.now()
        # 简化: 使用本地时间近似 HKT
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now > open_time:
            # 已过开盘, 可能是第二次运行
            return 0
        return int((open_time - now).total_seconds() / 60)

    def _assign_priority(self, cluster: Any, relevance: Dict[str, float]) -> str:
        """分配优先级标签"""
        overall = relevance.get("overall", 0.0)
        is_breaking = getattr(cluster, "is_breaking", False)

        if is_breaking and overall >= 0.5:
            return "critical"
        elif overall >= 0.7:
            return "high"
        elif overall >= 0.4:
            return "medium"
        else:
            return "low"
