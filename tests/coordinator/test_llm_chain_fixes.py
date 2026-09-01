"""LLM 链路修复回归测试 (2026-05-19, 今晚开盘前)

修复:
- L1: news_classifier 迁 model_call (走 yaml.llm.features.news_classifier 路由)
- L2-B: schema/API 失败时记 audit + 保留 raw response (诊断基础)
- L3: scalping 触发 thesis_inferer (释放 110 笔/天 LLM 数据)
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def _read_src(rel_path: str) -> str:
    return (Path(__file__).resolve().parent.parent.parent / rel_path).read_text()


# ─────────────────────────────────────────────────────
# L1: news_classifier 迁 model_call
# ─────────────────────────────────────────────────────

def test_l1_news_classifier_uses_model_call():
    """L1: news_classifier 必须 import model_call (新), 弃用 llm_helper.call_llm (老)."""
    src = _read_src("app/news_v2/processor/llm_classifier.py")

    # 必须 import model_call
    assert "from app.llm.model_call import call_llm" in src, (
        "L1: news_classifier 必须从 model_call import call_llm"
    )

    # 必须用 schema_required (不是老 parse_and_validate 二步)
    assert "schema_required=" in src, "L1: 必须用 model_call 的 schema_required 参数"

    # 不应直接用 from app.llm.features.llm_helper import call_llm (但允许 _model_call_llm 别名引用)
    direct_old_import = "from app.llm.features.llm_helper import call_llm"
    matching_lines = [l for l in src.split('\n') if direct_old_import in l and not l.strip().startswith('#')]
    assert len(matching_lines) == 0, (
        f"L1: 不应再直接 import llm_helper.call_llm, 实际: {matching_lines}"
    )


# ─────────────────────────────────────────────────────
# L2-B: schema/API 失败也记 audit
# ─────────────────────────────────────────────────────

def test_l2b_schema_failure_records_audit():
    """L2-B: model_call 在 schema 失败时必须调 _record_audit, 保留 raw response 供诊断."""
    src = _read_src("app/llm/model_call.py")

    # schema 失败 continue 之前必须 _record_audit
    # 找 schema_failed_on_ 上下文 (精确到 res.error 赋值 — 第12轮 downshift_retry_schema_failed_on_
    # 审计标记含同子串, 模糊 find 会误命中它, 见 DEB-20260829 第12轮)
    idx = src.find('res.error = f"schema_failed_on_')
    assert idx > 0, "L2-B: schema 失败标记必须存在"
    # 同一函数内 schema_failed_on_ 后必须有 _record_audit (检查后续 200 字符)
    nearby = src[idx:idx + 500]
    assert "_record_audit(feature, res, prompt)" in nearby, (
        "L2-B: schema 失败后必须调用 _record_audit 保留 raw"
    )

    # 必须有 schema_failed 日志含 raw_head 用于即时诊断
    assert "raw_head=" in src, "L2-B: schema 失败必须打 raw_head 日志"


def test_l2b_api_failure_also_records_audit():
    """L2-B: API 失败 (timeout/auth) 也要记 audit, 否则失败原因不可观测."""
    src = _read_src("app/llm/model_call.py")
    # API 失败 (`if not res.success`) 后应有 _record_audit
    api_fail_idx = src.find("if not res.success:")
    assert api_fail_idx > 0
    nearby = src[api_fail_idx:api_fail_idx + 300]
    assert "_record_audit(feature, res, prompt)" in nearby, (
        "L2-B: API 失败也必须 _record_audit"
    )


# ─────────────────────────────────────────────────────
# L3: scalping 接 thesis_inferer — 已随 scalping 引擎移除 (2026-06-19)
# ─────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────
# L2-B 运行时验证: schema 失败实际写 audit
# ─────────────────────────────────────────────────────

def test_l2b_runtime_schema_failure_writes_audit(monkeypatch):
    """L2-B 运行时: schema 失败时 _record_audit 必须被调用, 且 response 含 raw text."""
    from app.llm import model_call as mc

    fake_cfg = {
        "models": {
            "fake-model": {"provider": "test", "api_key_env": "TEST_KEY",
                           "base_url": "http://localhost", "model_name": "fake-v1",
                           "timeout": 5, "cost_in": 0.001, "cost_out": 0.001},
        },
        "features": {"test_schema_fail": {"primary": "fake-model"}},
    }
    monkeypatch.setattr(mc, "_get_llm_config", lambda: fake_cfg)

    def mock_try(alias, cfg, system, prompt, **kw):
        return mc.CallResult(
            success=True,
            response='{"wrong_field": "value"}',  # 缺 required 字段
            model_used=alias, latency_ms=120,
            tokens_in=10, tokens_out=20, cost_usd=0.0001,
        )
    monkeypatch.setattr(mc, "_try_single_model", mock_try)

    # 拦截 _record_audit 调用计数 + 参数
    audit_calls = []
    original_audit = mc._record_audit

    def spy_audit(feature, res, prompt_sample):
        audit_calls.append({
            "feature": feature,
            "success": res.success,
            "schema_passed": res.schema_passed,
            "error": res.error,
            "response_text": res.response if isinstance(res.response, str) else None,
        })
    monkeypatch.setattr(mc, "_record_audit", spy_audit)

    result = mc.call_llm(
        feature="test_schema_fail",
        prompt="test prompt",
        schema_required={"required_field": str},
    )

    assert not result.success, "schema 失败 + 无 fallback → success=False"

    # L2-B 核心验证: schema 失败时 _record_audit 必须被调用
    schema_fail_calls = [c for c in audit_calls if "schema_failed_on_" in (c["error"] or "")]
    assert len(schema_fail_calls) >= 1, (
        f"L2-B: schema 失败必须调 _record_audit, 实际所有 audit calls: {audit_calls}"
    )

    # 必须保留 raw response (诊断基础)
    assert schema_fail_calls[0]["response_text"] == '{"wrong_field": "value"}', (
        f"L2-B: schema 失败时 audit 必须含 raw response, 实际: {schema_fail_calls[0]}"
    )
