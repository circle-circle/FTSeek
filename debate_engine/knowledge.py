"""
辩论知识库

持久化历史辩论记录、提炼教训、追踪回填结果。
支持按议题类型/组件/市场检索相关历史。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Lesson:
    """从辩论中提炼的教训"""
    lesson_id: str
    source_debate_id: str
    category: str                           # factor_decay / exit_mismatch / overfitting / etc.
    statement: str
    evidence: str = ""
    confidence: float = 0.5                 # 初始置信度
    validated: bool = False                 # 是否已被实际数据验证
    validation_result: str = ""             # confirmed / refuted / inconclusive
    applicable_markets: List[str] = field(default_factory=list)
    applicable_factors: List[str] = field(default_factory=list)
    applicable_exits: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Lesson:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DebateRecord:
    """单次辩论的完整记录"""
    debate_id: str
    topic_id: str
    topic_type: str
    market: str
    timestamp: str
    models_used: List[str]
    total_rounds: int
    stop_reason: str                        # converged / divergence / max_rounds
    consensus_score: float
    consensus_items: List[Dict] = field(default_factory=list)
    disagreement_items: List[Dict] = field(default_factory=list)
    action_items: List[Dict] = field(default_factory=list)
    rounds_summary: List[Dict] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DebateRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Outcome:
    """结论注入后的实际效果追踪"""
    outcome_id: str
    debate_id: str
    injection_id: str
    metric_name: str                        # win_rate / pnl / sharpe / max_dd
    value_before: float
    value_after: float
    observation_days: int
    observation_trades: int
    verdict: str                            # confirmed / refuted / inconclusive
    note: str = ""
    recorded_at: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Outcome:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


LESSON_CATEGORIES = {
    "factor_decay": "因子失效 — 参数有效性随市场变化衰减",
    "exit_mismatch": "止损不匹配 — ATR 倍数与策略时间框架不匹配",
    "filter_false_positive": "过滤假阳性 — 过滤条件误杀有效信号",
    "filter_false_negative": "过滤假阴性 — 过滤条件未能拦截噪声",
    "sample_size": "样本量不足 — 统计结论不可靠",
    "overfitting": "过拟合 — 参数在历史数据上有效但泛化差",
    "regime_sensitivity": "市场状态敏感 — 策略在特定 regime 下有效",
    "cross_market_gap": "跨市场差异 — 因子在不同市场表现不同",
    "execution_reality": "执行偏差 — 滑点/延迟导致实际偏离回测",
    "concentration_risk": "集中度风险 — 利润集中在少数交易",
}


class DebateKnowledgeBase:
    """
    辩论知识库 — 持久化 + 检索 + 学习
    
    目录结构:
    debate_engine/knowledge/
        debates/    — 完整辩论记录 (JSON)
        lessons/    — 提炼的教训 (lessons_index.json)
        outcomes/   — 回填结果 (outcomes_index.json)
        patterns/   — 跨辩论模式 (patterns_index.json)
    """
    
    def __init__(self, knowledge_dir: Optional[str] = None):
        if knowledge_dir is None:
            knowledge_dir = str(Path(__file__).resolve().parent / "knowledge")
        self.base_dir = Path(knowledge_dir)
        self.debates_dir = self.base_dir / "debates"
        self.lessons_dir = self.base_dir / "lessons"
        self.outcomes_dir = self.base_dir / "outcomes"
        self.patterns_dir = self.base_dir / "patterns"
        
        # 确保目录存在
        for d in [self.debates_dir, self.lessons_dir, self.outcomes_dir, self.patterns_dir]:
            d.mkdir(parents=True, exist_ok=True)
        
        # 内存缓存
        self._lessons: List[Lesson] = []
        self._debates: List[DebateRecord] = []
        self._outcomes: List[Outcome] = []
        self._load_indexes()
    
    def _load_indexes(self):
        """加载索引文件"""
        self._lessons = self._load_index(self.lessons_dir / "lessons_index.json", Lesson)
        self._debates = self._load_index(self.outcomes_dir / "debates_index.json", DebateRecord)
        self._outcomes = self._load_index(self.outcomes_dir / "outcomes_index.json", Outcome)
    
    def _load_index(self, path: Path, cls) -> List:
        """加载 JSON 索引文件"""
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                return [cls.from_dict(d) for d in data]
            except Exception as e:
                logger.warning(f"Failed to load {path}: {e}")
        return []
    
    def _save_index(self, path: Path, items: List):
        """保存 JSON 索引文件"""
        with open(path, "w") as f:
            json.dump([i.to_dict() for i in items], f, indent=2, ensure_ascii=False)
    
    # ── 记录 ──
    
    def save_debate(self, record: DebateRecord):
        """保存完整辩论记录"""
        # 保存完整 JSON
        debate_path = self.debates_dir / f"{record.debate_id}.json"
        with open(debate_path, "w") as f:
            json.dump(record.to_dict(), f, indent=2, ensure_ascii=False)
        
        # 更新索引
        self._debates.append(record)
        self._save_index(self.outcomes_dir / "debates_index.json", self._debates)
        logger.info(f"Saved debate: {record.debate_id}")
    
    def add_lesson(self, lesson: Lesson):
        """添加教训"""
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()
        lesson.updated_at = lesson.created_at
        self._lessons.append(lesson)
        self._save_index(self.lessons_dir / "lessons_index.json", self._lessons)
        logger.info(f"Added lesson: {lesson.lesson_id} [{lesson.category}]")
    
    def add_outcome(self, outcome: Outcome):
        """添加回填结果"""
        if not outcome.recorded_at:
            outcome.recorded_at = datetime.now().isoformat()
        self._outcomes.append(outcome)
        self._save_index(self.outcomes_dir / "outcomes_index.json", self._outcomes)
        
        # 更新相关 lesson 的置信度
        self._update_lesson_from_outcome(outcome)
        logger.info(f"Added outcome: {outcome.outcome_id} -> {outcome.verdict}")
    
    def _update_lesson_from_outcome(self, outcome: Outcome):
        """根据回填结果更新 lesson 置信度"""
        for lesson in self._lessons:
            if lesson.source_debate_id == outcome.debate_id:
                if outcome.verdict == "confirmed":
                    lesson.confidence = min(1.0, lesson.confidence + 0.1)
                    lesson.validated = True
                    lesson.validation_result = "confirmed"
                elif outcome.verdict == "refuted":
                    lesson.confidence = max(0.0, lesson.confidence - 0.3)
                    lesson.validated = True
                    lesson.validation_result = "refuted"
                lesson.updated_at = datetime.now().isoformat()
                break
        self._save_index(self.lessons_dir / "lessons_index.json", self._lessons)
    
    # ── 检索 ──
    
    def find_related_lessons(self, topic_type: str = "", market: str = "",
                              factors: Optional[List[str]] = None,
                              top_k: int = 5) -> List[Lesson]:
        """检索相关历史教训"""
        scored = []
        for lesson in self._lessons:
            score = 0
            # 匹配市场
            if market and market in lesson.applicable_markets:
                score += 2
            if "BOTH" in lesson.applicable_markets:
                score += 1
            # 匹配因子
            if factors:
                overlap = set(factors) & set(lesson.applicable_factors)
                score += len(overlap) * 3
            # 已验证的教训权重更高
            if lesson.validated:
                score += 2
            # 高置信度权重更高
            score += lesson.confidence
            # 低置信度 (<0.5) 降权
            if lesson.confidence < 0.5:
                score -= 2
            
            scored.append((score, lesson))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:top_k]]
    
    def find_related_debates(self, topic_type: str = "", market: str = "",
                              top_k: int = 3) -> List[DebateRecord]:
        """检索相关历史辩论"""
        matched = []
        for debate in self._debates:
            if topic_type and debate.topic_type != topic_type:
                continue
            if market and debate.market != market and debate.market != "BOTH":
                continue
            matched.append(debate)
        return matched[:top_k]
    
    def get_anti_patterns(self) -> List[str]:
        """获取已知反模式 (从被证伪的教训中提炼)"""
        patterns = []
        for lesson in self._lessons:
            if lesson.validated and lesson.validation_result == "refuted":
                patterns.append(f"[REFUTED] {lesson.statement} (confidence={lesson.confidence:.1f})")
        return patterns
    
    def build_knowledge_context(self, topic_type: str = "", market: str = "",
                                 factors: Optional[List[str]] = None) -> Dict[str, Any]:
        """为新辩论构建知识上下文"""
        lessons = self.find_related_lessons(topic_type, market, factors)
        debates = self.find_related_debates(topic_type, market)
        anti_patterns = self.get_anti_patterns()
        
        return {
            "related_lessons": [l.to_dict() for l in lessons],
            "related_debates": [d.to_dict() for d in debates],
            "anti_patterns": anti_patterns,
            "total_lessons": len(self._lessons),
            "validated_lessons": sum(1 for l in self._lessons if l.validated),
        }
    
    @property
    def stats(self) -> Dict[str, int]:
        return {
            "total_debates": len(self._debates),
            "total_lessons": len(self._lessons),
            "validated_lessons": sum(1 for l in self._lessons if l.validated),
            "total_outcomes": len(self._outcomes),
            "confirmed": sum(1 for o in self._outcomes if o.verdict == "confirmed"),
            "refuted": sum(1 for o in self._outcomes if o.verdict == "refuted"),
        }
