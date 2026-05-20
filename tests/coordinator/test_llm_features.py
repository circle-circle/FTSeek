"""
LLM 增强 features 单测 (L0-L12, 2026-05-14)

聚焦于:
- 共享基础设施: feature_flags / json_schema_guard / audit_db
- 各 feature 模块 import 可用 + schema 校验逻辑
- 默认 disabled 行为
"""
import os
import tempfile
import pytest


# ─── L0 共享基础设施 ──────────────────────────────────────────
def test_feature_flags_default_disabled():
    """高成本/高风险 LLM features 默认 disabled"""
    from app.llm.features.feature_flags import is_enabled
    # 这些必须默认 disabled (高成本/风险/影响下单决策)
    assert is_enabled("emergency_review") is False
    assert is_enabled("earnings_analyst") is False
    # thesis_inferer PB-1 (2026-05-15) 已启用 (Phase B.6)
    # news_classifier P1-B (2026-05-15) 已启用 (修复 0 LLM 调用)


def test_feature_flags_unknown_returns_false():
    """未知 feature → false"""
    from app.llm.features.feature_flags import is_enabled
    assert is_enabled("nonexistent_feature_xyz") is False


def test_json_schema_guard_parse_simple():
    from app.llm.features.json_schema_guard import parse_json_strict
    obj = parse_json_strict('{"a": 1, "b": "x"}')
    assert obj == {"a": 1, "b": "x"}


def test_json_schema_guard_parse_fenced():
    from app.llm.features.json_schema_guard import parse_json_strict
    text = "Sure, here is:\n```json\n{\"a\": 1}\n```"
    obj = parse_json_strict(text)
    assert obj == {"a": 1}


def test_json_schema_guard_parse_with_prefix_text():
    from app.llm.features.json_schema_guard import parse_json_strict
    obj = parse_json_strict('Analysis result: {"sentiment": -0.5, "horizon": 60}')
    assert obj is not None
    assert obj["sentiment"] == -0.5


def test_json_schema_guard_validate_required():
    from app.llm.features.json_schema_guard import validate_schema
    ok, _ = validate_schema(
        {"event_type": "earnings", "confidence": 0.7},
        required={"event_type": str, "confidence": float},
    )
    assert ok
    # missing required
    ok2, err2 = validate_schema(
        {"event_type": "earnings"},
        required={"event_type": str, "confidence": float},
    )
    assert not ok2
    assert "confidence" in err2


def test_json_schema_guard_validate_enum():
    from app.llm.features.json_schema_guard import validate_schema
    ok, _ = validate_schema(
        {"decision": "HOLD"},
        required={"decision": str},
        enums={"decision": ["HOLD", "REDUCE_50", "FLAT_NOW"]},
    )
    assert ok
    ok2, err2 = validate_schema(
        {"decision": "PANIC"},
        required={"decision": str},
        enums={"decision": ["HOLD", "REDUCE_50", "FLAT_NOW"]},
    )
    assert not ok2
    assert "enum_violation" in err2


def test_json_schema_guard_clamp_numeric():
    from app.llm.features.json_schema_guard import clamp_numeric
    assert clamp_numeric(1.5, 0.0, 1.0) == 1.0
    assert clamp_numeric(-2.0, -1.0, 1.0) == -1.0
    assert clamp_numeric(0.5, 0.0, 1.0) == 0.5
    # 异常输入
    assert clamp_numeric("invalid", 0.0, 1.0) == 0.0


def test_audit_db_schema():
    """audit_db 写入 + 读取"""
    import sqlite3
    from app.llm.features.audit_db import record_call, get_feature_call_count
    record_call(
        feature="test_feature_xyz",
        model="qwen",
        prompt="test prompt",
        response='{"ok": true}',
        success=True,
        cost_usd=0.001,
        latency_ms=100,
    )
    n = get_feature_call_count("test_feature_xyz", hours=1)
    assert n >= 1


# ─── L1 News classifier ─────────────────────────────────────
def test_l1_news_classifier_disabled_returns_none():
    from app.news_v2.processor.llm_classifier import classify_news
    # feature 默认 disabled → return None 不调 LLM
    result = classify_news("test-id", "Test title", "Body", "test_source")
    assert result is None


def test_l1_news_classifier_lang_detection():
    from app.news_v2.processor.llm_classifier import _detect_lang
    assert _detect_lang("Apple beats Q4 earnings", "Strong revenue growth") == "en"
    assert _detect_lang("苹果公司发布Q4财报超预期", "营收强劲增长") == "zh"
    assert _detect_lang("ホンダの第4四半期決算が予想を上回る", "売上高は強力に成長") == "ja"


# ─── L9 News signal gen ────────────────────────────────────
def test_l9_news_signal_disabled():
    from app.news_v2.processor.event_signal_generator import generate_signals_from_classification
    from app.news_v2.processor.llm_classifier import NewsClassification
    cls = NewsClassification(
        news_id="x", event_type="earnings",
        primary_tickers=["NVDA"], impacted_tickers=[],
        sentiment=0.8, time_horizon_minutes=60,
        credibility=0.9, action_hint="BUY", rationale=""
    )
    # disabled → 空 list
    sigs = generate_signals_from_classification(cls)
    assert sigs == []


# ─── L2 emergency review ───────────────────────────────────
def test_l2_emergency_review_disabled():
    from app.risk.llm_emergency_review import review
    result = review("NVDA", "price_drop", {"unrealized_pnl_pct": -0.05})
    assert result is None


def test_l2_emergency_should_trigger_price_drop():
    from app.risk.llm_emergency_review import should_trigger
    trigger = should_trigger("NVDA", {"pnl_5m": -0.04})
    assert trigger == "price_drop"


def test_l2_emergency_should_trigger_vix_spike():
    from app.risk.llm_emergency_review import should_trigger
    trigger = should_trigger("NVDA", {"vix_change_5m": 0.25})
    assert trigger == "vix_spike"


def test_l2_emergency_no_trigger():
    from app.risk.llm_emergency_review import should_trigger
    trigger = should_trigger("NVDA", {"pnl_5m": -0.01, "vix_change_5m": 0.05})
    assert trigger is None


# ─── L3 thesis inferer ─────────────────────────────────────
def test_l3_thesis_disabled_does_not_run():
    from app.llm.thesis_inferer import infer_thesis_async
    # disabled → 不会调 LLM (无报错即 OK)
    infer_thesis_async("NVDA", {"trace_id": "test"}, {})


# ─── L4 earnings analyst ────────────────────────────────────
def test_l4_earnings_disabled():
    from app.llm.earnings_analyst import analyze_earnings
    result = analyze_earnings("NVDA", {}, {"direction": "LONG"})
    assert result is None


# ─── L5 disagreement arbitrator ──────────────────────────────
def test_l5_compute_disagreement_consensus():
    from app.llm.disagreement_arbitrator import compute_disagreement
    views = [
        {"direction": "LONG", "confidence": 0.7},
        {"direction": "LONG", "confidence": 0.75},
    ]
    d = compute_disagreement(views)
    assert d < 0.3  # 一致 → 低分歧


def test_l5_compute_disagreement_split():
    from app.llm.disagreement_arbitrator import compute_disagreement
    views = [
        {"direction": "LONG", "confidence": 0.8},
        {"direction": "SHORT", "confidence": 0.7},
    ]
    d = compute_disagreement(views)
    assert d > 0.3  # 分歧大


def test_l5_arbitrate_disabled():
    from app.llm.disagreement_arbitrator import arbitrate
    views = [
        {"direction": "LONG", "confidence": 0.8},
        {"direction": "SHORT", "confidence": 0.7},
    ]
    result = arbitrate("NVDA", views)
    assert result is None  # disabled


# ─── L6 regime classifier ────────────────────────────────────
def test_l6_regime_disabled():
    from app.llm.regime_classifier import classify
    result = classify({"spy_change_pct": 0.01, "vix": 18})
    assert result is None


def test_l6_regime_factor_adjustments():
    from app.llm.regime_classifier import get_factor_weight_adjustments
    adj = get_factor_weight_adjustments("trend_continuation")
    assert "trend" in adj
    assert -0.10 <= adj["trend"] <= 0.10


def test_l6_regime_unknown_returns_empty():
    from app.llm.regime_classifier import get_factor_weight_adjustments
    adj = get_factor_weight_adjustments("unknown_regime_xyz")
    assert adj == {}


# ─── L7 large position review ────────────────────────────────
def test_l7_large_position_disabled():
    from app.llm.large_position_review import review_large_position
    result = review_large_position("NVDA", {"position_pct": 0.10}, {})
    assert result is None  # disabled


def test_l7_below_threshold_returns_none():
    """即使 enabled, 仓位低于阈值也不调"""
    # 改用 monkeypatch 模拟 enabled
    import app.llm.features.feature_flags as ff
    orig = ff.is_enabled
    ff.is_enabled = lambda f: f == "large_position_review"
    try:
        from app.llm.large_position_review import review_large_position
        # position_pct=0.03 < 默认 threshold 0.05
        result = review_large_position("NVDA", {"position_pct": 0.03, "direction": "LONG"}, {})
        assert result is None
    finally:
        ff.is_enabled = orig


# ─── L8 daily auto tuner ─────────────────────────────────────
def test_l8_auto_tuner_disabled():
    from app.llm.daily_auto_tuner import propose_from_daily_review
    proposals = propose_from_daily_review({"summary": {}})
    assert proposals == []


def test_l8_pending_proposals_query():
    """get_pending_proposals 不报错"""
    from app.llm.daily_auto_tuner import get_pending_proposals
    proposals = get_pending_proposals()
    assert isinstance(proposals, list)


# ─── L11 sector correlation ──────────────────────────────────
def test_l11_correlation_disabled():
    from app.llm.sector_correlation_reasoner import reason_about
    result = reason_about("NVDA", -0.03, ["AMD", "INTC"])
    assert result is None


def test_l11_below_threshold_no_call():
    """anchor 变动小于阈值不触发"""
    import app.llm.features.feature_flags as ff
    orig = ff.is_enabled
    ff.is_enabled = lambda f: f == "sector_correlation"
    try:
        from app.llm.sector_correlation_reasoner import reason_about
        result = reason_about("NVDA", 0.005, ["AMD"])  # 0.5% < 2%
        assert result is None
    finally:
        ff.is_enabled = orig


# ─── 整体可控性测试 ──────────────────────────────────────────
def test_high_risk_llm_features_default_disabled():
    """高成本/高风险 features 必须默认 disabled (即使其他 enabled)"""
    import yaml
    with open("config/unified_config.yaml") as f:
        cfg = yaml.safe_load(f)
    features = cfg.get("llm_features", {})
    # 这些可能影响下单决策的 features 不允许默认 enabled
    HIGH_RISK = ["emergency_review", "earnings_analyst",
                 "large_position_review", "daily_auto_tuner"]
    for f in HIGH_RISK:
        assert features.get(f, {}).get("enabled", False) is False, \
            f"高风险 feature {f} 不应默认 enabled"


def test_all_l_modules_importable():
    """所有 L 模块可正常 import (无 syntax error)"""
    import importlib
    modules = [
        "app.llm.features.feature_flags",
        "app.llm.features.json_schema_guard",
        "app.llm.features.audit_db",
        "app.llm.features.llm_helper",
        "app.news_v2.processor.llm_classifier",
        "app.news_v2.processor.event_signal_generator",
        "app.news_v2.processor.news_attribution",
        "app.risk.llm_emergency_review",
        "app.llm.thesis_inferer",
        "app.llm.earnings_analyst",
        "app.llm.disagreement_arbitrator",
        "app.llm.regime_classifier",
        "app.llm.large_position_review",
        "app.llm.daily_auto_tuner",
        "app.llm.sector_correlation_reasoner",
    ]
    failed = []
    for m in modules:
        try:
            importlib.import_module(m)
        except Exception as e:
            failed.append(f"{m}: {e}")
    assert not failed, f"导入失败: {failed}"
