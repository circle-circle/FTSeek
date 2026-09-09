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


def test_audit_db_schema(tmp_path, monkeypatch):
    """audit_db 写入 + 读取 (2026-08-27 改造: 隔离到 tmp 库 + 生产上下文 —
    原直写生产 llm_audit.db, 是测试流量污染源之一; is_test 标记后读者
    过滤生产行, 需模拟生产上下文才能测通写入-读取回路)"""
    import app.llm.features.audit_db as audit_db
    db = tmp_path / "llm_audit.db"
    monkeypatch.setattr(audit_db, "_get_db_path", lambda: str(db))
    monkeypatch.setattr(audit_db, "_is_test_context", lambda: False)
    audit_db.record_call(
        feature="test_feature_xyz",
        model="qwen",
        prompt="test prompt",
        response='{"ok": true}',
        success=True,
        cost_usd=0.001,
        latency_ms=100,
    )
    n = audit_db.get_feature_call_count("test_feature_xyz", hours=1)
    assert n >= 1


# ─── L1 News classifier ─────────────────────────────────────
def test_l1_news_classifier_disabled_returns_none(monkeypatch):
    # 2026-07-26: yaml 已常开 news_classifier, 原测试假设默认关 → 每次真调 LLM 花钱。
    # 改为强制 disabled, 密封化验证关闭路径。
    import app.news_v2.processor.llm_classifier as lc
    monkeypatch.setattr(lc, "is_enabled", lambda *a, **k: False)
    result = lc.classify_news("test-id", "Test title", "Body", "test_source")
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


# ─── (L2 emergency review removed — module deleted 2026-06-15) ───

# ─── L3 thesis inferer ─────────────────────────────────────
def test_l3_thesis_disabled_does_not_run(monkeypatch):
    import app.llm.thesis_inferer as ti
    # 2026-09-09: yaml 已常开 thesis_inferer, 原测试假设默认关 → 每次真调
    # qwen3.7-max 花钱 (trace_id=test 落生产审计库)。镜像上方 L1 news_classifier
    # 先例: 强制 disabled, 密封化验证关闭路径。
    monkeypatch.setattr(ti, "is_enabled", lambda *a, **k: False)
    ti.infer_thesis_async("NVDA", {"trace_id": "test"}, {})


# L6 regime_classifier / L8 daily_auto_tuner: 2026-07-26 减法R2 已删
# (flag 恒 false 的死功能, 模块零生产调用方), 对应测试同步移除。


# ─── 整体可控性测试 ──────────────────────────────────────────
def test_high_risk_llm_features_default_disabled():
    """高成本/高风险 features 必须默认 disabled (即使其他 enabled)"""
    import yaml
    with open("config/unified_config.yaml") as f:
        cfg = yaml.safe_load(f)
    features = cfg.get("llm_features", {})
    # 这些可能影响下单决策的 features 不允许默认 enabled
    HIGH_RISK = ["emergency_review", "daily_auto_tuner"]
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
        "app.news_v2.processor.llm_classifier",
        "app.news_v2.processor.event_signal_generator",
        "app.llm.thesis_inferer",
    ]
    failed = []
    for m in modules:
        try:
            importlib.import_module(m)
        except Exception as e:
            failed.append(f"{m}: {e}")
    assert not failed, f"导入失败: {failed}"
