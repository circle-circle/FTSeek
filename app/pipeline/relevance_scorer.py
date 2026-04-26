"""相关性评分模块

对每个事件聚类进行多维度相关性评分，过滤低价值事件。

评分维度:
  - stock_relevance (权重 0.4): 与关注股票/持仓的关联度
  - policy_relevance (权重 0.2): 政策/监管/宏观影响
  - earnings_relevance (权重 0.2): 财报/业绩/分析师影响
  - urgency (权重 0.2): 时间敏感性 (突发新闻 + 距开盘时间)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger("ftseek.pipeline.relevance_scorer")


@dataclass
class RelevanceScorer:
    """多维度相关性评分器

    对每个 EventCluster 计算 4 维相关性分 + 综合分。
    """

    config: Dict = field(default_factory=dict)
    watchlist: List[str] = field(default_factory=list)
    weights: Dict[str, float] = field(default_factory=lambda: {
        "stock": 0.4,
        "policy": 0.2,
        "earnings": 0.2,
        "urgency": 0.2,
    })
    min_overall_score: float = 0.2

    def __post_init__(self):
        self.config = self.config or {}
        self.min_overall_score = self.config.get(
            "min_overall_score", self.min_overall_score
        )
        weights_cfg = self.config.get("weights", {})
        for k, v in weights_cfg.items():
            self.weights[k] = float(v)

        # 加载 watchlist
        if not self.watchlist:
            try:
                from app.config import WATCH_LIST
                self.watchlist = WATCH_LIST
            except ImportError:
                self.watchlist = []

    def score(self, cluster: Any) -> Dict[str, float]:
        """对单个事件聚类进行相关性评分

        Returns:
            {stock_relevance, policy_relevance, earnings_relevance, urgency, overall}
        """
        stock_rel = self._stock_relevance(cluster)
        policy_rel = self._policy_relevance(cluster)
        earnings_rel = self._earnings_relevance(cluster)
        urgency = self._urgency(cluster)

        overall = (
            self.weights["stock"] * stock_rel
            + self.weights["policy"] * policy_rel
            + self.weights["earnings"] * earnings_rel
            + self.weights["urgency"] * urgency
        )

        return {
            "stock_relevance": round(stock_rel, 3),
            "policy_relevance": round(policy_rel, 3),
            "earnings_relevance": round(earnings_rel, 3),
            "urgency": round(urgency, 3),
            "overall": round(min(1.0, overall), 3),
        }

    def filter_relevant(self, clusters: List[Any]) -> List[Any]:
        """过滤低相关性事件，返回 relevance >= min_overall_score 的聚类"""
        scored = []
        for c in clusters:
            scores = self.score(c)
            if scores["overall"] >= self.min_overall_score:
                c._relevance_scores = scores  # 附加到 cluster 对象
                scored.append(c)

        scored.sort(key=lambda c: c._relevance_scores["overall"], reverse=True)
        logger.info(
            f"[RelevanceScorer] 过滤: {len(clusters)} -> {len(scored)} 个相关事件 "
            f"(阈值: {self.min_overall_score})"
        )
        return scored

    # ── 内部评分方法 ─────────────────────────────────────────────

    def _stock_relevance(self, cluster: Any) -> float:
        """与关注股票的关联度"""
        stocks = getattr(cluster, "related_stocks", []) or []
        if not stocks:
            return 0.1  # 无股票关联, 但可能有宏观影响

        # 直接匹配 watchlist
        watchlist_set = set(self.watchlist)
        direct_matches = [s for s in stocks if s in watchlist_set]

        if direct_matches:
            return min(1.0, 0.7 + 0.1 * len(direct_matches))

        # 检查是否为目标股票池中的行业
        # HK 代码的行业判断: 简化处理 — 有股票关联就给基础分
        return 0.5

    def _policy_relevance(self, cluster: Any) -> float:
        """政策/监管/宏观影响"""
        category = getattr(cluster, "category", "")
        articles = getattr(cluster, "articles", []) or []

        policy_categories = {"regulation", "macro", "geopolitical"}
        if category in policy_categories:
            return 0.9

        # 从文章关键词检测
        policy_keywords = ["监管", "政策", "央行", "美联储", "利率", "制裁", "关税",
                           "regulation", "policy", "fed", "central bank", "sanction",
                           "tariff", "trade war"]

        text = ""
        for a in articles[:3]:  # 检查前 3 篇
            text += f"{getattr(a, 'title', '')} {getattr(a, 'content', '')} "
        text = text.lower()

        match_count = sum(1 for kw in policy_keywords if kw.lower() in text)
        return min(1.0, match_count * 0.15)

    def _earnings_relevance(self, cluster: Any) -> float:
        """财报/业绩/分析师影响"""
        category = getattr(cluster, "category", "")
        articles = getattr(cluster, "articles", []) or []

        earnings_categories = {"earnings", "analyst"}
        if category in earnings_categories:
            return 0.9

        earnings_keywords = ["财报", "业绩", "盈利", "亏损", "营收", "净利润", "超预期",
                             "earnings", "revenue", "profit", "beat", "miss", "guidance",
                             "评级", "目标价", "upgrade", "downgrade"]

        text = ""
        for a in articles[:3]:
            text += f"{getattr(a, 'title', '')} {getattr(a, 'content', '')} "
        text = text.lower()

        match_count = sum(1 for kw in earnings_keywords if kw.lower() in text)
        return min(1.0, match_count * 0.15)

    def _urgency(self, cluster: Any) -> float:
        """时间敏感性"""
        # 突发新闻最高 urgency
        is_breaking = getattr(cluster, "is_breaking", False)
        if is_breaking:
            return 0.95

        # 时间衰减: 距发布越久 urgency 越低
        first_seen = getattr(cluster, "first_seen", datetime.now())
        if isinstance(first_seen, str):
            try:
                first_seen = datetime.fromisoformat(first_seen)
            except (ValueError, TypeError):
                first_seen = datetime.now()

        age_minutes = (datetime.now() - first_seen).total_seconds() / 60
        if age_minutes < 30:
            return 0.9
        elif age_minutes < 60:
            return 0.7
        elif age_minutes < 120:
            return 0.5
        elif age_minutes < 240:
            return 0.3
        elif age_minutes < 480:
            return 0.15
        else:
            return 0.05
