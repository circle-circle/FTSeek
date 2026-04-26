"""
FTSeek UnifiedNewsItemV2 — 统一新闻数据模型

v2 升级点 (对比 v1 UnifiedNewsItem):
1. 增加 urgency 字段 (事件分级)
2. 增加 geo_scan 字段 (地缘预筛结果)
3. 增加 related_stocks 列表 (结构化关联)
4. 增加 trace_id (全链路归因)
5. 增加处理状态标记 (llm_analyzed, pushed, signal_generated)
"""

import hashlib
import time
from dataclasses import dataclass, field, asdict
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
    RSSHUB_FED = "rsshub_fed"
    RSSHUB_FUTU = "rsshub_futu"
    FUTU_NIUNIU = "futu_niuniu"     # Futu 7x24 快讯
    SEC_EDGAR = "sec_edgar"         # SEC 公告
    CN_10JQKA = "cn_10jqka"         # 同花顺快讯
    CN_EASTMONEY = "cn_eastmoney"   # 东方财富
    TICKER_ANOMALY = "ticker_anomaly"  # 逐笔异动 (间接信号)


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
    market_tags: List[str] = field(default_factory=list)     # ["HK", "US", "MACRO"]
    sectors: List[str] = field(default_factory=list)         # ["能源", "科技"]
    urgency: str = NewsUrgency.NORMAL    # 紧急程度
    related_stocks: List[str] = field(default_factory=list)  # ["HK.00700", "US.NVDA"]
    
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
        # 移除多余字段
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in d.items() if k in allowed}
        # 设置默认值
        clean.setdefault("urgency", NewsUrgency.NORMAL)
        clean.setdefault("geo_scan", {})
        clean.setdefault("related_stocks", [])
        clean.setdefault("trace_id", "")
        clean.setdefault("raw_url", "")
        clean.setdefault("llm_result", {})
        return cls(**clean)
    
    def to_dict(self) -> dict:
        """转为字典 (用于存储)"""
        return asdict(self)
    
    @property
    def content_hash(self) -> str:
        """内容哈希 (用于去重)"""
        text = f"{self.title}|{self.content[:200]}|{self.source}"
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
    
    @property
    def simhash_value(self) -> int:
        """SimHash 值 (用于模糊去重)"""
        return compute_simhash(self.title + self.content[:500])
    
    @property
    def age_seconds(self) -> float:
        """新闻年龄 (秒)"""
        return time.time() - self.collected_at
    
    @property
    def age_minutes(self) -> float:
        """新闻年龄 (分钟)"""
        return self.age_seconds / 60
    
    def is_expired(self, max_age_minutes: int = 120) -> bool:
        """是否过期"""
        return self.age_minutes > max_age_minutes
    
    def is_geo_event(self) -> bool:
        """是否是地缘政治事件"""
        return self.geo_scan.get("is_geo_event", False)
    
    def is_immediate(self) -> bool:
        """是否需要立即处理"""
        return self.urgency in (NewsUrgency.IMMEDIATE, NewsUrgency.HIGH)
    
    def __repr__(self):
        return f"NewsV2({self.source_name}: {self.title[:40]}... | {self.urgency} | score={self.raw_score:.2f})"


# ============================================================
# SimHash 实现 (轻量版, 无外部依赖)
# ============================================================

def _simple_hash(word: str) -> int:
    """简单的字符串哈希 (FNV-1a)"""
    h = 14695981039346656037
    for c in word.encode("utf-8"):
        h ^= c
        h *= 1099511628211
    return h & 0xFFFFFFFFFFFFFFFF


def _jieba_tokens(text: str) -> List[str]:
    """
    简易分词 (不依赖 jieba)
    按中文字符和英文单词切分
    """
    import re
    # 英文单词
    en_words = re.findall(r"[a-zA-Z]+", text)
    # 中文字符 (2-4 字组合)
    zh_chars = re.findall(r"[\u4e00-\u9fff]+", text)
    zh_ngrams = []
    for zh in zh_chars:
        for n in range(1, min(4, len(zh) + 1)):
            for i in range(len(zh) - n + 1):
                zh_ngrams.append(zh[i:i+n])
    return en_words + zh_ngrams


def compute_simhash(text: str, bits: int = 63) -> int:
    """
    计算 SimHash 值
    
    Args:
        text: 输入文本
        bits: 哈希位数 (默认 64)
    
    Returns:
        SimHash 整数值
    """
    if not text or len(text.strip()) < 2:
        return 0
    
    tokens = _jieba_tokens(text.lower())
    if not tokens:
        return 0
    
    # 加权向量
    v = [0] * bits
    for token in tokens:
        h = _simple_hash(token)
        weight = 1
        # 长 token 权重更高
        if len(token) > 4:
            weight = min(len(token) // 2, 5)
        for i in range(bits):
            if (h >> i) & 1:
                v[i] += weight
            else:
                v[i] -= weight
    
    # 生成 SimHash
    simhash = 0
    for i in range(bits):
        if v[i] > 0:
            simhash |= (1 << i)
    
    return simhash


def hamming_distance(h1: int, h2: int) -> int:
    """计算两个 SimHash 的汉明距离"""
    x = h1 ^ h2
    d = 0
    while x:
        d += 1
        x &= x - 1
    return d


def simhash_similarity(h1: int, h2: int, bits: int = 63) -> float:
    """计算两个 SimHash 的相似度 (0-1)"""
    if h1 == 0 or h2 == 0:
        return 0.0
    dist = hamming_distance(h1, h2)
    return 1.0 - dist / bits
