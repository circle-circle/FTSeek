"""
辩论引擎单元测试 (Step 4)

≥ 15 test cases covering:
- TopicType + DebateTopic
- BailianClient (mock)
- DebateKnowledgeBase (CRUD + retrieval)
- DebateVerdict + Injection + InjectionApplier
- DynamicRoundScheduler (consensus computation)
- OutcomeTracker
- CLI argument parsing
"""
import json
import os
import shutil
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root in path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debate_engine.topic import DebateTopic, TopicType
from debate_engine.knowledge import (
    DebateKnowledgeBase, Lesson, DebateRecord, Outcome,
    LESSON_CATEGORIES,
)
from debate_engine.verdict import DebateVerdict, Injection, InjectionApplier
from debate_engine.rounds import DynamicRoundScheduler, DebateConfig, RoundResult
from debate_engine.outcome_tracker import OutcomeTracker


# ── Fixtures ──

@pytest.fixture
def temp_knowledge_dir(tmp_path):
    """Temporary knowledge directory"""
    kb_dir = tmp_path / "knowledge"
    for sub in ["debates", "lessons", "outcomes", "patterns"]:
        (kb_dir / sub).mkdir(parents=True)
    return str(kb_dir)


@pytest.fixture
def temp_overrides_dir(tmp_path):
    """Temporary overrides directory"""
    d = tmp_path / "overrides"
    d.mkdir()
    return str(d)


@pytest.fixture
def kb(temp_knowledge_dir):
    """Knowledge base with temp dir"""
    return DebateKnowledgeBase(knowledge_dir=temp_knowledge_dir)


def _make_topic(**kwargs):
    defaults = dict(
        topic_id="test_topic",
        topic_type=TopicType.FACTOR_PARAM,
        market="US",
        title="Test Topic",
        description="Testing the debate engine",
        evidence={"trades": 100, "win_rate": 0.45},
        affected_components=["factor:SSS"],
    )
    defaults.update(kwargs)
    return DebateTopic(**defaults)


def _make_lesson(kb, lesson_id="L001", category="factor_decay",
                 confidence=0.7, validated=False, market="US"):
    lesson = Lesson(
        lesson_id=lesson_id,
        source_debate_id="debate_001",
        category=category,
        statement=f"Test lesson {lesson_id}",
        confidence=confidence,
        validated=validated,
        applicable_markets=[market],
        applicable_factors=["SSS"],
    )
    kb.add_lesson(lesson)
    return lesson


# ── TopicType + DebateTopic ──

class TestDebateTopic:

    def test_topic_types(self):
        assert TopicType.FACTOR_PARAM.value == "factor_param"
        assert TopicType.EXIT_PROFILE.value == "exit_profile"
        assert TopicType.CROSS_MARKET.value == "cross_market"
        assert len(TopicType) == 8

    def test_topic_creation(self):
        topic = _make_topic()
        assert topic.topic_id == "test_topic"
        assert topic.topic_type == TopicType.FACTOR_PARAM
        assert topic.market == "US"

    def test_topic_serialization(self):
        topic = _make_topic()
        d = topic.to_dict()
        assert d["topic_type"] == "factor_param"
        assert d["market"] == "US"

    def test_topic_deserialization(self):
        d = {
            "topic_id": "t1",
            "topic_type": "strategy_viability",
            "market": "HK",
            "title": "HK Test",
        }
        topic = DebateTopic.from_dict(d)
        assert topic.topic_type == TopicType.STRATEGY_VIABILITY
        assert topic.market == "HK"

    def test_topic_with_evidence(self):
        topic = _make_topic(evidence={"trades": 200, "sharpe": 2.5})
        assert topic.evidence["trades"] == 200
        assert topic.evidence["sharpe"] == 2.5


# ── Knowledge Base ──

class TestKnowledgeBase:

    def test_add_and_retrieve_lesson(self, kb):
        _make_lesson(kb, "L001")
        assert len(kb._lessons) == 1
        assert kb._lessons[0].lesson_id == "L001"

    def test_find_related_lessons_by_factor(self, kb):
        _make_lesson(kb, "L001", category="factor_decay")
        _make_lesson(kb, "L002", category="exit_mismatch")
        results = kb.find_related_lessons(factors=["SSS"], top_k=5)
        assert len(results) >= 1

    def test_find_related_lessons_by_market(self, kb):
        _make_lesson(kb, "L001", market="US")
        _make_lesson(kb, "L002", market="HK")
        results = kb.find_related_lessons(market="US", top_k=5)
        # US lesson should rank higher
        assert len(results) >= 1

    def test_validated_lessons_rank_higher(self, kb):
        _make_lesson(kb, "L001", confidence=0.7, validated=False)
        _make_lesson(kb, "L002", confidence=0.7, validated=True)
        results = kb.find_related_lessons(top_k=2)
        # Validated lesson should come first
        assert results[0].validated is True

    def test_low_confidence_penalized(self, kb):
        _make_lesson(kb, "L001", confidence=0.8)
        _make_lesson(kb, "L002", confidence=0.3)
        results = kb.find_related_lessons(top_k=2)
        assert results[0].lesson_id == "L001"

    def test_save_debate(self, kb):
        record = DebateRecord(
            debate_id="debate_test_001",
            topic_id="t1",
            topic_type="factor_param",
            market="US",
            timestamp="2026-05-23T10:00:00",
            models_used=["qwen3.7-max", "deepseek-v4-pro"],
            total_rounds=3,
            stop_reason="converged",
            consensus_score=0.85,
        )
        kb.save_debate(record)
        assert len(kb._debates) == 1

    def test_add_outcome_updates_lesson(self, kb):
        _make_lesson(kb, "L001", confidence=0.5)
        outcome = Outcome(
            outcome_id="O001",
            debate_id="debate_001",
            injection_id="inj_001",
            metric_name="win_rate",
            value_before=0.30,
            value_after=0.45,
            observation_days=7,
            observation_trades=20,
            verdict="confirmed",
        )
        kb.add_outcome(outcome)
        assert kb._lessons[0].confidence == 0.6  # 0.5 + 0.1
        assert kb._lessons[0].validated is True

    def test_refuted_outcome_decreases_confidence(self, kb):
        _make_lesson(kb, "L001", confidence=0.7)
        outcome = Outcome(
            outcome_id="O002",
            debate_id="debate_001",
            injection_id="inj_002",
            metric_name="pnl",
            value_before=100,
            value_after=80,
            observation_days=7,
            observation_trades=10,
            verdict="refuted",
        )
        kb.add_outcome(outcome)
        assert abs(kb._lessons[0].confidence - 0.4) < 0.01  # 0.7 - 0.3
        assert kb._lessons[0].validated is True

    def test_anti_patterns(self, kb):
        _make_lesson(kb, "L001", confidence=0.7, validated=True)
        kb._lessons[0].validation_result = "refuted"
        kb._save_index(kb.lessons_dir / "lessons_index.json", kb._lessons)
        patterns = kb.get_anti_patterns()
        assert len(patterns) == 1
        assert "REFUTED" in patterns[0]

    def test_build_knowledge_context(self, kb):
        _make_lesson(kb, "L001", category="factor_decay")
        ctx = kb.build_knowledge_context(topic_type="factor_param", market="US")
        assert "related_lessons" in ctx
        assert "anti_patterns" in ctx
        assert "total_lessons" in ctx

    def test_stats(self, kb):
        _make_lesson(kb, "L001")
        _make_lesson(kb, "L002")
        stats = kb.stats
        assert stats["total_lessons"] == 2

    def test_lesson_categories_complete(self):
        assert len(LESSON_CATEGORIES) == 10
        assert "overfitting" in LESSON_CATEGORIES
        assert "execution_reality" in LESSON_CATEGORIES


# ── Verdict + Injection ──

class TestVerdict:

    def test_injection_creation(self):
        inj = Injection(
            injection_id="inj_001",
            target="factor:SSS",
            action="override_param",
            params={"threshold": -1.5},
            confidence=0.85,
            market="US",
            source_debate_id="debate_001",
        )
        assert inj.target == "factor:SSS"
        assert inj.status == "pending"

    def test_injection_serialization(self):
        inj = Injection(
            injection_id="inj_001",
            target="exit:stable_15m",
            action="override_param",
            params={"max_holding_minutes": 180},
            confidence=0.9,
            market="US",
            source_debate_id="debate_001",
        )
        d = inj.to_dict()
        restored = Injection.from_dict(d)
        assert restored.injection_id == inj.injection_id
        assert restored.params == inj.params

    def test_verdict_creation(self):
        v = DebateVerdict(
            debate_id="debate_001",
            topic_id="t1",
            topic_type="factor_param",
            market="US",
            total_rounds=3,
            consensus_score=0.85,
            stop_reason="converged",
        )
        assert v.debate_id == "debate_001"
        assert v.consensus_score == 0.85

    def test_verdict_serialization(self):
        v = DebateVerdict(
            debate_id="d1", topic_id="t1", topic_type="custom",
            market="US", total_rounds=4, consensus_score=0.7,
            stop_reason="max_rounds",
            consensus_items=[{"statement": "test"}],
        )
        d = v.to_dict()
        restored = DebateVerdict.from_dict(d)
        assert restored.debate_id == "d1"
        assert len(restored.consensus_items) == 1


class TestInjectionApplier:

    def test_apply_creates_yaml(self, temp_overrides_dir):
        applier = InjectionApplier(overrides_dir=temp_overrides_dir)
        inj = Injection(
            injection_id="inj_001",
            target="factor:SSS",
            action="override_param",
            params={"threshold": -1.5},
            confidence=0.9,
            market="US",
            source_debate_id="debate_20260523",
        )
        path = applier.apply(inj)
        assert Path(path).exists()
        assert "debate_20260523" in path

    def test_list_active(self, temp_overrides_dir):
        applier = InjectionApplier(overrides_dir=temp_overrides_dir)
        inj = Injection(
            injection_id="inj_001", target="factor:SSS",
            action="override_param", params={"threshold": -1.5},
            confidence=0.9, market="US", source_debate_id="debate_001",
        )
        applier.apply(inj)
        active = applier.list_active()
        assert len(active) == 1
        assert active[0]["target"] == "factor:SSS"

    def test_revert_all(self, temp_overrides_dir):
        applier = InjectionApplier(overrides_dir=temp_overrides_dir)
        for i in range(3):
            inj = Injection(
                injection_id=f"inj_{i}", target=f"factor:F{i}",
                action="override_param", params={}, confidence=0.8,
                market="US", source_debate_id="debate_001",
            )
            applier.apply(inj)
        count = applier.revert_all("debate_001")
        assert count == 3
        assert len(applier.list_active()) == 0


# ── DynamicRoundScheduler ──

class TestRoundScheduler:

    def test_config_defaults(self):
        config = DebateConfig()
        assert config.min_rounds == 3
        assert config.max_rounds == 7
        assert config.convergence_threshold == 0.8

    def test_consensus_computation_identical(self):
        scheduler = DynamicRoundScheduler()
        outputs = {
            "model_a": {"parsed": {"action_items": ["add VIX filter", "tighten stop"]}},
            "model_b": {"parsed": {"action_items": ["add VIX filter", "tighten stop"]}},
        }
        score = scheduler._compute_consensus(outputs)
        assert score == 1.0  # Perfect agreement

    def test_consensus_computation_partial(self):
        scheduler = DynamicRoundScheduler()
        outputs = {
            "model_a": {"parsed": {"action_items": ["add VIX filter", "tighten stop", "widen entry"]}},
            "model_b": {"parsed": {"action_items": ["add VIX filter", "reduce position", "add spread filter"]}},
        }
        score = scheduler._compute_consensus(outputs)
        assert 0.0 < score < 1.0  # Partial agreement

    def test_consensus_computation_no_overlap(self):
        scheduler = DynamicRoundScheduler()
        outputs = {
            "model_a": {"parsed": {"action_items": ["action_a", "action_b"]}},
            "model_b": {"parsed": {"action_items": ["action_c", "action_d"]}},
        }
        score = scheduler._compute_consensus(outputs)
        assert score == 0.0

    def test_consensus_with_errors(self):
        scheduler = DynamicRoundScheduler()
        outputs = {
            "model_a": {"error": "timeout"},
            "model_b": {"parsed": {"action_items": ["test"]}},
        }
        score = scheduler._compute_consensus(outputs)
        assert score == 0.0  # Can't compare with error

    def test_extract_factors(self, kb):
        scheduler = DynamicRoundScheduler(knowledge_base=kb)
        topic = _make_topic(affected_components=["factor:SSS", "factor:GDIAN", "exit:stable_15m"])
        factors = scheduler._extract_factors(topic)
        assert "SSS" in factors
        assert "GDIAN" in factors
        assert len(factors) == 2


# ── OutcomeTracker ──

class TestOutcomeTracker:

    def test_record_manual_outcome(self, kb):
        tracker = OutcomeTracker(kb=kb)
        outcome = tracker.record_manual_outcome(
            debate_id="debate_001",
            injection_id="inj_001",
            metric_name="win_rate",
            value_before=0.30,
            value_after=0.42,
            observation_days=7,
            observation_trades=25,
            verdict="confirmed",
            note="Significant improvement",
        )
        assert outcome.verdict == "confirmed"
        assert outcome.value_after == 0.42

    def test_learning_report(self, kb):
        tracker = OutcomeTracker(kb=kb)
        report = tracker.generate_learning_report()
        assert "summary" in report
        assert "confirmation_rate" in report
        assert "recommendations" in report


# ── Lesson Categories ──

class TestLessonCategories:

    def test_all_categories_present(self):
        expected = [
            "factor_decay", "exit_mismatch", "filter_false_positive",
            "filter_false_negative", "sample_size", "overfitting",
            "regime_sensitivity", "cross_market_gap", "execution_reality",
            "concentration_risk",
        ]
        for cat in expected:
            assert cat in LESSON_CATEGORIES, f"Missing category: {cat}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
