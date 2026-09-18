"""统一 LLM 调用入口测试 (2026-05-17)

覆盖:
  - yaml routing (feature → primary/fallback)
  - schema 校验 (失败 → fallback / None)
  - 缓存兜底
  - cross_check critique 路径 (含拒决策)
  - 兼容旧 multi_model_client.chat
"""
import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.llm.model_call import (
    call_llm, CallResult,
    _try_single_model, _resolve_feature_route, _resolve_model,
    _save_cache, _load_cache, prompt_hash,
)


# ─── Fixtures ───
@pytest.fixture
def mock_cfg():
    """模拟 llm yaml 配置"""
    return {
        "models": {
            "qwen-flash": {
                "api_key_env": "TEST_QWEN_KEY",
                "base_url": "https://test.qwen/",
                "model_name": "qwen3.6-flash",
                "timeout": 8,
                "cost_in": 0.0003,
                "cost_out": 0.0006,
            },
            "qwen-plus": {
                "api_key_env": "TEST_QWEN_KEY",
                "base_url": "https://test.qwen/",
                "model_name": "qwen3.6-plus",
                "timeout": 30,
                "cost_in": 0.004,
                "cost_out": 0.012,
            },
            "deepseek-pro": {
                "api_key_env": "TEST_DS_KEY",
                "base_url": "https://test.ds/",
                "model_name": "deepseek-v4-pro",
                "timeout": 30,
                "cost_in": 0.002,
                "cost_out": 0.008,
            },
        },
        "features": {
            "simple_feature": "qwen-flash",
            "fallback_feature": {
                "primary": "qwen-plus", "fallback": "deepseek-pro",
            },
            "cache_feature": {
                "primary": "qwen-plus", "fallback": "deepseek-pro",
                "cache_on_fail": True,
            },
        },
        "defaults": {"cache_dir": "tmp/test_llm_cache", "divergence_threshold": 0.5},
    }


@pytest.fixture
def env_keys(monkeypatch):
    monkeypatch.setenv("TEST_QWEN_KEY", "test-qwen-key")
    monkeypatch.setenv("TEST_DS_KEY", "test-ds-key")


@pytest.fixture
def mock_yaml(mock_cfg, monkeypatch):
    """patch _get_llm_config 返回 mock_cfg"""
    import app.llm.model_call as mc
    monkeypatch.setattr(mc, "_get_llm_config", lambda: mock_cfg)


# ─── Route 解析 ───
def test_resolve_route_simple(mock_cfg):
    r = _resolve_feature_route("simple_feature", mock_cfg)
    assert r == {"primary": "qwen-flash", "fallback": None, "cache_on_fail": False}


def test_resolve_route_full(mock_cfg):
    r = _resolve_feature_route("fallback_feature", mock_cfg)
    assert r["primary"] == "qwen-plus"
    assert r["fallback"] == "deepseek-pro"


def test_resolve_route_unknown(mock_cfg):
    assert _resolve_feature_route("nonexistent", mock_cfg) is None


def test_resolve_model_found(mock_cfg):
    m = _resolve_model("qwen-flash", mock_cfg)
    assert m["model_name"] == "qwen3.6-flash"


def test_resolve_model_unknown(mock_cfg):
    assert _resolve_model("unknown-alias", mock_cfg) is None


# ─── 路由级模型参数 ───
def test_route_extra_body_is_passed_to_model(mock_yaml, env_keys, mock_cfg):
    mock_cfg["features"]["thinking_feature"] = {
        "primary": "qwen-flash",
        "extra_body": {"enable_thinking": True, "reasoning_effort": "high"},
    }
    captured = {}

    def mock_try_single_model(alias, cfg, system, prompt, **kwargs):
        captured.update(alias=alias, extra_body=kwargs.get("extra_body"))
        return CallResult(success=True, response="ok", model_used="qwen3.6-flash")

    with patch("app.llm.model_call._try_single_model", side_effect=mock_try_single_model):
        result = call_llm("thinking_feature", "ping")

    assert result.success is True
    assert captured["alias"] == "qwen-flash"
    assert captured["extra_body"] == {"enable_thinking": True, "reasoning_effort": "high"}


# ─── 简单调用 (mock HTTP) ───
def test_simple_call_success(mock_yaml, env_keys):
    with patch("app.llm.model_call._http_call") as m:
        m.return_value = {
            "text": "Hello world",
            "tokens_in": 10, "tokens_out": 20,
            "raw": {},
        }
        res = call_llm("simple_feature", "ping")
        assert res.success is True
        assert res.response == "Hello world"
        # model_used 语义演进: 记录真实 model_name (非 alias), 便于成本/归因追踪
        assert res.model_used == "qwen3.6-flash"
        assert res.tokens_in == 10
        assert res.fallback_used is False


def test_call_returns_failure_when_unconfigured(mock_yaml, env_keys):
    res = call_llm("nonexistent_feature", "x")
    assert res.success is False
    assert "feature_not_routed" in res.error


def test_missing_api_key(mock_yaml, monkeypatch, mock_cfg):
    # 不设 env, 直接验证底层
    monkeypatch.delenv("TEST_QWEN_KEY", raising=False)
    res = _try_single_model("qwen-flash", mock_cfg, "sys", "user",
                              temperature=0.1, max_tokens=100)
    assert res.success is False
    assert "missing_env" in res.error


# ─── Fallback 链 ───
def test_fallback_used_when_primary_fails(mock_yaml, env_keys):
    call_log = []

    def mock_http(base_url, api_key, model_name, *a, **kw):
        call_log.append(model_name)
        if model_name == "qwen3.6-plus":
            raise Exception("primary timeout")
        return {"text": "ok from deepseek", "tokens_in": 5, "tokens_out": 10, "raw": {}}

    with patch("app.llm.model_call._http_call", side_effect=mock_http), \
         patch("app.llm.model_call.time.sleep"):  # 跳过 retry sleep
        res = call_llm("fallback_feature", "test")
    assert res.success is True
    assert res.response == "ok from deepseek"
    assert res.model_used == "deepseek-v4-pro"  # model_used 记录真实 model_name
    assert res.fallback_used is True
    # primary 调 2 次 (retry), fallback 调 1 次成功
    assert call_log.count("qwen3.6-plus") == 2
    assert call_log.count("deepseek-v4-pro") == 1


def test_all_models_fail_no_cache(mock_yaml, env_keys):
    with patch("app.llm.model_call._http_call", side_effect=Exception("network")):
        with patch("app.llm.model_call.time.sleep"):
            res = call_llm("fallback_feature", "test")
    assert res.success is False
    assert res.error == "all_models_failed"
    assert res.fallback_used is True


# ─── Schema 校验 ───
def test_schema_passes_on_valid_json(mock_yaml, env_keys):
    with patch("app.llm.model_call._http_call") as m:
        m.return_value = {
            "text": '{"date": "2026-05-17", "value": 100}',
            "tokens_in": 5, "tokens_out": 10, "raw": {},
        }
        res = call_llm(
            "simple_feature", "x",
            schema_required={"date": str, "value": int},
        )
    assert res.success is True
    assert res.schema_passed is True
    assert res.response == {"date": "2026-05-17", "value": 100}


def test_schema_fail_triggers_fallback(mock_yaml, env_keys):
    """primary 返回非法 JSON, fallback 返回合法 JSON → 应走 fallback"""
    def mock_http(base_url, api_key, model_name, *a, **kw):
        if model_name == "qwen3.6-plus":
            return {"text": "I don't know", "tokens_in": 5, "tokens_out": 3, "raw": {}}
        return {"text": '{"valid": true}', "tokens_in": 5, "tokens_out": 5, "raw": {}}

    with patch("app.llm.model_call._http_call", side_effect=mock_http), \
         patch("app.llm.model_call.time.sleep"):
        res = call_llm(
            "fallback_feature", "x",
            schema_required={"valid": bool},
        )
    assert res.success is True
    assert res.fallback_used is True
    assert res.response == {"valid": True}


# ─── 缓存兜底 ───
def test_cache_fallback_when_all_fail(mock_yaml, env_keys, tmp_path, monkeypatch):
    # 重定向 cache 目录到临时
    monkeypatch.setattr("app.llm.model_call._cache_path",
                        lambda feature, cache_dir=None: tmp_path / f"{feature}.json")
    # 先写一份 cache
    _save_cache("cache_feature", "cached_response_value")
    # 确认 cache 存在
    cached = _load_cache("cache_feature", max_age_hours=24)
    assert cached == "cached_response_value"

    # 模拟全部 LLM 失败
    with patch("app.llm.model_call._http_call", side_effect=Exception("down")):
        with patch("app.llm.model_call.time.sleep"):
            res = call_llm("cache_feature", "test")
    assert res.success is True
    assert res.from_cache is True
    assert res.response == "cached_response_value"


def test_cache_expired_falls_through(mock_yaml, env_keys, tmp_path, monkeypatch):
    monkeypatch.setattr("app.llm.model_call._cache_path",
                        lambda feature, cache_dir=None: tmp_path / f"{feature}.json")
    # 写 1 个 25h 前的 cache (过期)
    cache_path = tmp_path / "cache_feature.json"
    import time
    payload = {"response": "old", "saved_at": time.time() - 25 * 3600}
    cache_path.write_text(json.dumps(payload))

    with patch("app.llm.model_call._http_call", side_effect=Exception("down")):
        with patch("app.llm.model_call.time.sleep"):
            res = call_llm("cache_feature", "test")
    assert res.success is False  # 过期 cache 不用


# ─── Cross-check critique ───
def test_cross_check_agreed_returns_primary(mock_yaml, env_keys):
    """critic 同意 → 返回 primary 结果 + cross_check 元数据"""
    def mock_http(base_url, api_key, model_name, system, user, **kw):
        if model_name == "qwen3.6-plus":
            return {"text": "primary plan", "tokens_in": 10, "tokens_out": 20, "raw": {}}
        if model_name == "deepseek-v4-pro":
            return {"text": '{"agree": true, "severity": 0.1, "reason": "ok"}',
                    "tokens_in": 8, "tokens_out": 15, "raw": {}}

    with patch("app.llm.model_call._http_call", side_effect=mock_http):
        res = call_llm("fallback_feature", "decide", cross_check=True)
    assert res.success is True
    assert "primary plan" == res.response
    assert res.cross_check is not None
    assert res.cross_check["agreed"] is True


def test_cross_check_disagree_severe_rejects(mock_yaml, env_keys):
    """critic 严重不同意 → 拒决策"""
    def mock_http(base_url, api_key, model_name, system, user, **kw):
        if model_name == "qwen3.6-plus":
            return {"text": "primary plan", "tokens_in": 10, "tokens_out": 20, "raw": {}}
        return {"text": '{"agree": false, "severity": 0.8, "reason": "data thin", "corrections": ["X"]}',
                "tokens_in": 8, "tokens_out": 25, "raw": {}}

    with patch("app.llm.model_call._http_call", side_effect=mock_http):
        res = call_llm("fallback_feature", "decide", cross_check=True)
    assert res.success is False
    assert res.error == "cross_check_rejected"
    assert res.cross_check["severity"] == 0.8


def test_cross_check_critic_fail_degrades_to_primary(mock_yaml, env_keys):
    """critic 失败 → 仅返回 primary, 标记 degraded"""
    def mock_http(base_url, api_key, model_name, system, user, **kw):
        if model_name == "qwen3.6-plus":
            return {"text": "primary plan", "tokens_in": 10, "tokens_out": 20, "raw": {}}
        raise Exception("critic down")

    with patch("app.llm.model_call._http_call", side_effect=mock_http), \
         patch("app.llm.model_call.time.sleep"):
        res = call_llm("fallback_feature", "decide", cross_check=True)
    assert res.success is True
    assert res.response == "primary plan"
    assert res.cross_check.get("degraded") is True


# ─── Prompt hash (版本管理) ───
def test_prompt_hash_stable():
    h1 = prompt_hash("sys", "user")
    h2 = prompt_hash("sys", "user")
    assert h1 == h2


def test_prompt_hash_differs_on_change():
    h1 = prompt_hash("sys-v1", "user")
    h2 = prompt_hash("sys-v2", "user")
    assert h1 != h2


# ─── truncat_downshift (DEB-20260829 第12轮: 默认停用 + 失败入审计) ───

_TRUNCATED = {"text": '{"surfaced": [{"symbol": "AA', "finish_reason": "length",
              "tokens_in": 4538, "tokens_out": 8000, "raw": {}}
_VALID = {"text": '{"valid": true}', "tokens_in": 5, "tokens_out": 5, "raw": {}}


def test_length_truncation_skips_retry_by_default(mock_yaml, env_keys):
    """9/1 UE 事故行为契约: length 截断 + schema 失败 → 默认不发同模型降档重试,
    直接换 fallback (旧默认 ×0.5 重试对内容真超限数学上必死, 历史成功 0 次)。"""
    calls = []

    def mock_http(base_url, api_key, model_name, system, user, **kw):
        calls.append((model_name, kw.get("max_tokens")))
        return _TRUNCATED if model_name == "qwen3.6-plus" else _VALID

    with patch("app.llm.model_call._http_call", side_effect=mock_http), \
         patch("app.llm.model_call.time.sleep"):
        res = call_llm("fallback_feature", "x", schema_required={"valid": bool})
    assert res.success is True
    assert res.fallback_used is True
    # primary 恰 1 次调用 (无降档重试), fallback 1 次
    assert calls.count(("qwen3.6-plus", 2000)) == 1
    assert calls.count(("deepseek-v4-pro", 2000)) == 1


def test_downshift_retry_failure_audited(mock_cfg, monkeypatch, env_keys):
    """显式回滚开启 retry_downshift_enabled: 重试失败必须入 audit 且带可辨识标记
    (原仅成功路径记录 — 9/1 双臂各 ~59s 白费完全不可观测)。"""
    import app.llm.model_call as mc
    cfg2 = dict(mock_cfg)
    cfg2["retry_downshift_enabled"] = True
    monkeypatch.setattr(mc, "_get_llm_config", lambda: cfg2)

    audits = []
    monkeypatch.setattr(mc, "_record_audit",
                        lambda feature, res, prompt: audits.append((feature, res.error)))

    calls = []

    def mock_http(base_url, api_key, model_name, system, user, **kw):
        calls.append((model_name, kw.get("max_tokens")))
        return _TRUNCATED if model_name == "qwen3.6-plus" else _VALID

    with patch("app.llm.model_call._http_call", side_effect=mock_http), \
         patch("app.llm.model_call.time.sleep"):
        res = call_llm("fallback_feature", "x", max_tokens=8000,
                       schema_required={"valid": bool})
    assert res.success is True
    assert res.fallback_used is True
    # primary 首调 8000 + 降档重试 4000 + fallback
    assert calls.count(("qwen3.6-plus", 8000)) == 1
    assert calls.count(("qwen3.6-plus", 4000)) == 1
    # 首调 schema 失败 + 降档重试失败 均入 audit
    errs = [a[1] for a in audits]
    assert any(e and e.startswith("schema_failed_on_") for e in errs)
    assert any(e and e.startswith("downshift_retry_schema_failed_on_") for e in errs)


# ─── CallResult ___bool__ ───
def test_callresult_truthy():
    assert bool(CallResult(success=True)) is True
    assert bool(CallResult(success=False)) is False
