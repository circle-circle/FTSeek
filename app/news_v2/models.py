"""
FTSeek-US UnifiedNewsItemV2 — 统一新闻数据模型

v2 升级点 (对比 v1 UnifiedNewsItem):
1. 增加 urgency 字段 (事件分级)
2. 增加 geo_scan 字段 (地缘预筛结果)
3. 增加 related_stocks 列表 (结构化关联)
4. 增加 trace_id (全链路归因)
5. 增加处理状态标记 (llm_analyzed, pushed, signal_generated)
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime


class NewsUrgency:
    """新闻紧急程度"""
    IMMEDIATE = "IMMEDIATE"   # 立即处理 (<5s): 战争/政策/财报发布
    HIGH = "HIGH"             # 高优先级 (<30s): 重要公告/大单异动
    NORMAL = "NORMAL"         # 正常: 行业分析/市场评论
    LOW = "LOW"               # 低优先级: 汇总报告/周报


class NewsSource:
    """新闻源标识"""
    RSSHUB_WSJ = "rsshub_wsj"
    RSSHUB_BLOOMBERG = "rsshub_bloomberg"
    RSSHUB_REUTERS = "rsshub_reuters"
    RSSHUB_FED = "rsshub_fed"
    SEC_EDGAR = "sec_edgar"         # SEC 公告
    FINNHUB_NEWS = "finnhub_news"   # Finnhub 突发新闻
    NEWSNOW_US = "newsnow_us"       # NewsNow US 聚合
    TICKER_ANOMALY = "ticker_anomaly"  # 逐笔异动 (间接信号)
    # CN 国产源 (2026-05-14): 国内大陆环境优先, 中文新闻
    CAILIANPRESS = "cailianpress"          # 财联社快讯
    WALLSTREETCN = "wallstreetcn"          # 华尔街见闻
    EASTMONEY_US = "eastmoney_us"          # 东方财富美股新闻
    XUEQIU_US = "xueqiu_us"                # 雪球美股 (备选)


@dataclass
class UnifiedNewsItemV2:
    """
    统一新闻条目 v2
    
    所有新闻源采集后统一转换为此格式，进入管线。
    """
    # === 核心字段 ===
    id: str                              # 全局唯一 ID: {source}_{hash8}
    title: str                           # 标题
    content: str                         # 摘要或全文 (最长 2000 字)
    source: str                          # 源标识 (见 NewsSource)
    source_name: str                     # 人类可读来源
    timestamp: float                     # 发布时间 (epoch)
    collected_at: float = field(default_factory=time.time)  # 采集时间
    
    # === 分类字段 ===
    market_tags: List[str] = field(default_factory=list)     # ["US", "MACRO"]
    sectors: List[str] = field(default_factory=list)         # ["Technology", "Energy"]
    urgency: str = NewsUrgency.NORMAL    # 紧急程度
    related_stocks: List[str] = field(default_factory=list)  # ["US.NVDA", "US.AAPL"]
    
    # === 质量字段 ===
    raw_score: float = 0.5               # 初始质量分 (0-1)
    geo_scan: Dict = field(default_factory=dict)  # 地缘预筛结果
    
    # === 处理字段 ===
    llm_analyzed: bool = False           # 是否已做 LLM 分析
    llm_result: Dict = field(default_factory=dict)  # LLM 分析结果
    pushed: bool = False                 # 是否已推送
    signal_generated: bool = False       # 是否已生成交易信号
    
    # === 溯源字段 ===
    trace_id: str = ""                   # 全链路追踪 ID
    raw_url: str = ""                    # 原文链接
    
    @classmethod
    def from_dict(cls, d: dict) -> "UnifiedNewsItemV2":
        """从字典构造 (兼容旧版 UnifiedNewsItem)"""
        known = {"id", "title", "content", "source", "source_name", "timestamp",
                 "collected_at", "market_tags", "sectors", "urgency", "related_stocks",
                 "raw_score", "geo_scan", "llm_analyzed", "llm_result", "pushed",
                 "signal_generated", "trace_id", "raw_url"}
        return cls(**{k: v for k, v in d.items() if k in known})
    
    def to_dict(self) -> dict:
        """转换为字典 (用于序列化)"""
        from dataclasses import asdict
        return asdict(self)
    
    @property
    def content_hash(self) -> str:
        """内容精确哈希 (用于 Gate 1 去重)"""
        return hashlib.md5(f"{self.title}{self.content[:200]}".encode()).hexdigest()
    
    @property
    def simhash_value(self) -> int:
        """SimHash 值 (63 位，用于 Gate 2 模糊去重)"""
        return compute_simhash(f"{self.title} {self.content[:500]}")
    
    def is_expired(self, max_age_minutes: int = 120) -> bool:
        """是否过期"""
        age_minutes = (time.time() - self.timestamp) / 60
        return age_minutes > max_age_minutes
    
    def is_geo_event(self) -> bool:
        """是否地缘政治事件"""
        return self.geo_scan.get("is_geo_event", False)
    
    def is_immediate(self) -> bool:
        """是否紧急新闻 (IMMEDIATE 级别)"""
        return self.urgency == "IMMEDIATE"
    
    def age_minutes(self) -> float:
        """新闻年龄 (分钟)"""
        return (time.time() - self.timestamp) / 60


def compute_simhash(text: str, bits: int = 63) -> int:
    """
    计算 SimHash (63 位)
    
    使用 63 位而非 64 位，因为 SQLite INTEGER 是有符号 64 位 (最大 2^63-1)
    """
    if not text:
        return 0
    
    # 简单 n-gram 分词
    tokens = []
    for n in range(3, 6):
        for i in range(len(text) - n + 1):
            tokens.append(text[i:i+n])
    
    if not tokens:
        return 0
    
    # 计算每个 token 的 hash
    v = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(bits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1
    
    # 生成 SimHash
    fingerprint = 0
    for i in range(bits):
        if v[i] > 0:
            fingerprint |= (1 << i)
    
    return fingerprint


def hamming_distance(hash1: int, hash2: int, bits: int = 63) -> int:
    """计算两个 SimHash 的汉明距离"""
    x = hash1 ^ hash2
    return bin(x & ((1 << bits) - 1)).count("1")


def similarity(hash1: int, hash2: int, bits: int = 63) -> float:
    """计算两个 SimHash 的相似度 (0-1)"""
    dist = hamming_distance(hash1, hash2, bits)
    return 1.0 - (dist / bits)
