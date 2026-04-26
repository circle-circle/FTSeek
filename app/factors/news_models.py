"""
FTSeek 统一新闻管线 (Unified News Pipeline) - 数据模型
2026-04-13 创建

功能:
1. 定义跨源、跨模块的标准新闻数据结构 UnifiedNewsItem。
2. 提供内容哈希 (ContentHash) 计算，用于全局去重。
3. 提供适配器，兼容旧版 NewsItem (from app.news_push.collector) 和 RSSHub 字典数据。
"""

import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict, Any

logger = logging.getLogger("ftseek.factors.news_models")


@dataclass
class UnifiedNewsItem:
    """
    统一新闻条目 (Unified News Item)

    所有来源 (RSSHub, NewsPush, Futu Flash) 的新闻在进入管线后，
    必须转换为此结构，以便后续去重、分析和推送。
    """

    # ── 核心标识 ──────────────────────────────────────────────
    id: str  # 全局唯一 ID (通常等于 content_hash)
    source: str  # 来源标识 (e.g., "rsshub/cls", "news_push/futu")

    # ── 内容 ──────────────────────────────────────────────────
    title: str
    content: str  # 摘要或全文
    url: str = ""

    # ── 时间 ──────────────────────────────────────────────────
    published_at: str = ""  # ISO 8601
    fetched_at: str = ""

    # ── 标签与分析 (由下游管线填充) ───────────────────────────
    tags: List[str] = field(default_factory=list)  # e.g. ["macro", "tech_sanction"]
    watchlist_codes: List[str] = field(default_factory=list)  # 关联的监控股票
    impact_score: float = 0.0  # LLM 计算的影响分 (-1.0 to 1.0)
    sentiment: str = "neutral"  # "positive", "neutral", "negative"

    # ── 内部属性 ──────────────────────────────────────────────
    content_hash: str = ""  # 用于去重的 SHA-256 Hash

    def __post_init__(self):
        # 自动计算 Hash (如果未提供)
        if not self.content_hash:
            self.content_hash = self.calculate_hash()
        if not self.id:
            self.id = self.content_hash
        if not self.fetched_at:
            self.fetched_at = datetime.now().isoformat()

    def calculate_hash(self) -> str:
        """计算内容指纹，用于全局去重"""
        # 使用 标题 + 来源 + 正文前 100 字符 生成 Hash
        content_snippet = self.content[:100] if self.content else ""
        text = f"{self.source}::{self.title}::{content_snippet}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典 (用于 JSON 存储) — 包含 _extra 字段 + 数值时间戳"""
        d = asdict(self)
        # 合并 _extra 中的关键元数据 (is_major, detected_markets, pushed, weight)
        extra = getattr(self, '_extra', {})
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in d:  # 不覆盖标准字段
                    d[k] = v
        # 添加数值型 timestamp (秒级 Unix 时间戳)，兼容下游管线的时间比较逻辑
        if not d.get('timestamp') and d.get('published_at'):
            try:
                from datetime import datetime as _dt
                pa = d['published_at']
                if isinstance(pa, str) and pa:
                    dt = _dt.fromisoformat(pa)
                    d['timestamp'] = int(dt.timestamp())
                    d['publish_time'] = int(dt.timestamp())
            except Exception:
                pass
        return d

    # ── Dict 兼容方法 (Duck-Typing) ─────────────────────────
    _FIELD_MAP = {
        'title': 'title',
        'content': 'content',
        'url': 'url',
        'link': 'url',
        'source': 'source',
        'source_name': 'source',
        'published_at': 'published_at',
        'publish_time': 'published_at',
        'published': 'published_at',
        'fetched_at': 'fetched_at',
        'tags': 'tags',
        'detected_markets': 'tags',
        'stocks': 'watchlist_codes',
        'watchlist_codes': 'watchlist_codes',
        'related_stocks': 'watchlist_codes',
        'id': 'id',
        'content_hash': 'content_hash',
        'impact_score': 'impact_score',
        'sentiment': 'sentiment',
    }

    def get(self, key: str, default=None) -> Any:
        """兼容 dict.get() 调用 — 下游推送管线的关键修复"""
        mapped = self._FIELD_MAP.get(key, key)
        # 优先从标准字段获取
        if hasattr(self, mapped):
            val = getattr(self, mapped)
            if val is not None:
                return val
        # 特殊处理: timestamp / publish_time 从 published_at 计算
        if key in ('timestamp', 'publish_time') and self.published_at:
            try:
                from datetime import datetime as _dt
                return int(_dt.fromisoformat(self.published_at).timestamp())
            except Exception:
                pass
        # 其次从 _extra 获取
        extra = getattr(self, '_extra', {})
        if isinstance(extra, dict) and key in extra:
            return extra[key]
        return default

    def __getitem__(self, key: str) -> Any:
        """支持 item[key] 语法"""
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        """支持 key in item 语法"""
        return self.get(key) is not None

    def __setitem__(self, key: str, value: Any) -> None:
        """支持 item[key] = value 语法 — 标记推送状态等"""
        mapped = self._FIELD_MAP.get(key, key)
        if hasattr(self, mapped):
            object.__setattr__(self, mapped, value)
        else:
            # 存入 _extra
            if not hasattr(self, '_extra') or not isinstance(self._extra, dict):
                object.__setattr__(self, '_extra', {})
            self._extra[key] = value

    # ── 静态工厂方法 (Adapters) ──────────────────────────────

    @staticmethod
    def from_rsshub_dict(data: dict, source_name: str = "rsshub") -> "UnifiedNewsItem":
        """
        从 RSSHub 采集器的字典格式转换为 UnifiedNewsItem
        适配脚本 scripts/rsshub_multi_source_cron.py 的输出
        """
        return UnifiedNewsItem(
            source=f"rsshub/{source_name}",
            title=data.get("title", ""),
            content=data.get("content", data.get("summary", "")),
            url=data.get("link", ""),
            published_at=data.get("publish_time", ""),
            tags=["rsshub"],
            watchlist_codes=data.get("stocks", []),
        )

    @staticmethod
    def from_news_push_item(item: Any) -> "UnifiedNewsItem":
        """
        从 NewsPush Collector 的 NewsItem 对象转换为 UnifiedNewsItem
        适配模块 app.news_push.collector.NewsItem
        """
        # 延迟导入避免循环依赖
        try:
            title = item.title
            content = item.content or ""
            source = item.source
            url = item.url or ""
            published = item.published_at
            tags = item.tags if hasattr(item, "tags") else []
            stocks = item.related_stocks if hasattr(item, "related_stocks") else []
        except AttributeError as e:
            logger.error(f"无法转换非 NewsItem 对象: {e}")
            return UnifiedNewsItem(source="unknown", title="Error", content="")

        return UnifiedNewsItem(
            source=f"news_push/{source}",
            title=title,
            content=content,
            url=url,
            published_at=published,
            tags=tags,
            watchlist_codes=stocks,
        )
