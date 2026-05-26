"""催化事件数据模型 — 新闻催化辩论引擎核心数据结构

设计原则:
- 最小化依赖 (仅 dataclass + typing)
- 与现有 CatalystEvent/CatalystVerdict 对齐
- 复用 call_llm feature 路由
"""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime


@dataclass
class CatalystEvent:
    """催化事件 — 从新闻流提取的交易相关事件
    
    来源: event_classifier.py 从新闻管线提取
    用途: 传入 catalyst_debate.py 进行多模型辩论
    """
    event_id: str                          # 唯一ID (hash of headline+source)
    event_type: str                        # REGULATORY|EARNINGS|FDA|MACRO|BREAKING|UPGRADE|DOWNGRADE
    severity: str                          # CRITICAL|HIGH|MEDIUM
    tickers: List[str]                     # ["US.FUTU", "US.BABA"]
    headline: str                          # 原始标题 (中文或英文)
    summary: str                           # 中英统一摘要 (LLM生成)
    source: str                            # newsnow|finnhub|rsshub|sec_edgar|hkex|csrc
    source_language: str = "en"            # zh|en
    published_at: float = 0.0              # Unix timestamp
    raw_items: List[dict] = field(default_factory=list)  # 原始新闻条目
    
    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "tickers": self.tickers,
            "headline": self.headline,
            "summary": self.summary,
            "source": self.source,
            "source_language": self.source_language,
            "published_at": self.published_at,
        }


@dataclass
class CatalystVerdict:
    """催化辩论结论 — 多模型辩论后的交易建议
    
    来源: catalyst_debate.py 辩论输出
    用途: 注入 _build_debate_context → 盘前计划
    """
    event_id: str                          # 关联 CatalystEvent
    symbol: str                            # 主标的 "US.FUTU"
    direction: str                         # LONG|SHORT|AVOID
    strength: int                          # 0-100 (辩论共识强度)
    confidence: float                      # 0.0-1.0
    consensus_level: float                 # 辩论共识度 (0.0-1.0)
    reasoning: str                         # 辩论结论摘要 (中英统一)
    sl_pct: float = -0.05                  # 建议止损% (负数)
    tp_pct: float = 0.10                   # 建议止盈% (正数)
    time_horizon: str = "swing"            # intraday|swing|position
    risk_warnings: List[str] = field(default_factory=list)
    model_votes: List[dict] = field(default_factory=list)  # 各模型原始投票
    debated_at: float = 0.0               # Unix timestamp
    
    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "strength": self.strength,
            "confidence": self.confidence,
            "consensus_level": self.consensus_level,
            "reasoning": self.reasoning,
            "sl_pct": self.sl_pct,
            "tp_pct": self.tp_pct,
            "time_horizon": self.time_horizon,
            "risk_warnings": self.risk_warnings,
            "debated_at": self.debated_at,
        }
