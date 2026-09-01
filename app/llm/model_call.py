"""统一 LLM 调用入口 (2026-05-17, 综合方案)

设计目标 (用户 5 需求):
  1. 模型切换零代码改 — yaml `llm.models.<alias>.model_name` 单点修改
  2. 统一接口 — `call_llm(feature, prompt)` 替代散在的 multi_model_client.chat
  3. 提升收益 — feature → primary/fallback 灵活组合
  4. 规避幻觉 — JSON Schema 强校验 + Citation prompt 增强
  5. 多模型验证 — 关键场景串行 critique (1 个出方案 + 1 个评审), 默认 off

向后兼容:
  - 新代码用 call_llm(feature="premarket_planner") (走 yaml routing 直连 provider)
  - 旧 audit_db.record_call 复用
  - 2026-08-27: multi_model_client.py 已删, 其桥接 call_llm_compat(零调用方)同步移除

降级链 (2 层):
  primary (qwen) → fallback (deepseek) → cache (data/llm_cache/<feature>.json) → None
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class CallResult:
    """LLM 调用结果 — 单一返回类型, 自带元数据"""
    success: bool
    response: Optional[Union[str, Dict]] = None
    model_used: str = ""
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    from_cache: bool = False
    fallback_used: bool = False
    schema_passed: Optional[bool] = None       # None=未校验 / True/False=结果
    cross_check: Optional[Dict] = None         # critique 结果, 仅 cross_check 场景
    error: Optional[str] = None
    finish_reason: str = ""                    # 2026-07-25 (failure DEB B①): 截断可观测 (length=被 max_tokens 截)
    cached_tokens: int = 0                     # 2026-07-25 (缓存探针实锤): 服务端隐式前缀缓存命中量 (deepseek 已证自动生效)

    def __bool__(self) -> bool:
        return self.success


# ─── 内部: yaml 配置读取 ───
def _get_llm_config() -> Dict:
    """从 unified_config.yaml 读 llm 段, 失败返回空 dict (降级)"""
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted("llm", {}) or {}
    except Exception as e:
        logger.debug(f"[ModelCall] yaml 读取失败 (用空配置): {e}")
        return {}


def _resolve_feature_route(feature: str, cfg: Dict) -> Optional[Dict]:
    """读 yaml llm.features[feature] → {primary, fallback, cache_on_fail}

    支持 2 种格式:
      简单: 'feature: qwen-flash'  → {primary: qwen-flash, fallback: None}
      完整: 'feature: {primary: qwen, fallback: deepseek, cache_on_fail: true}'
    """
    features = cfg.get("features", {}) or {}
    raw = features.get(feature)
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"primary": raw, "fallback": None, "cache_on_fail": False}
    if isinstance(raw, dict):
        return {
            "primary": raw.get("primary"),
            "fallback": raw.get("fallback"),
            "cache_on_fail": bool(raw.get("cache_on_fail", False)),
            # 2026-07-25 (thinking DEB E3): 透传路由级 extra_body (per-feature thinking 开关)
            "extra_body": raw.get("extra_body"),
            # 2026-07-25 (显式缓存实锤): per-feature 前缀缓存开关 (仅 dashscope 生效)
            "prompt_cache": bool(raw.get("prompt_cache", False)),
        }
    return None


def _resolve_model(alias: str, cfg: Dict) -> Optional[Dict]:
    """读 yaml llm.models[alias] → 完整 model 配置"""
    if not alias:
        return None
    models = cfg.get("models", {}) or {}
    m = models.get(alias)
    if not m:
        logger.warning(f"[ModelCall] 未知模型 alias: {alias} (yaml.llm.models 未定义)")
        return None
    return m


# ─── 内部: HTTP 调用 (复用 urllib, 0 新依赖) ───
def _http_call(
    base_url: str,
    api_key: str,
    model_name: str,
    system: str,
    user: str,
    *,
    temperature: float,
    max_tokens: int,
    timeout: int,
    json_mode: bool = False,
    extra_body: Optional[Dict] = None,
    prompt_cache: bool = False,
) -> Dict:
    """单次 HTTP 调用. 返回 {text, tokens_in, tokens_out, raw_response} 或 raise"""
    import urllib.request
    import urllib.error

    messages = []
    if system:
        # 2026-07-25 (用户指正后实锤): DashScope qwen 系支持显式前缀缓存 —
        #   system 用 content 数组 + cache_control ephemeral 标记, 创建后 5min TTL 内
        #   同前缀命中 (实测: 创建 3719tok → 第2发 cached=3719, 99.4%)。
        #   仅 dashscope 启用 (其他 provider 数组格式兼容性未验)。开关: route.prompt_cache。
        if prompt_cache and "dashscope.aliyuncs.com" in base_url:
            messages.append({"role": "system", "content": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]})
        else:
            messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # json_mode: 强制 response_format=json_object (qwen/deepseek 兼容)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    # extra_body: 模型特定参数 (e.g. enable_thinking, top_p)
    if extra_body:
        payload.update(extra_body)

    # 2026-07-06 P0修复: DashScope 推理模型 (glm-5.2/deepseek-v4-pro/qwen3.7-max)
    # 默认 enable_thinking=True, 内部 reasoning 消耗 max_tokens → empty_response
    # 实测: glm-5.2 max_tokens=800 时 800 reasoning → 0 输出 = empty_response
    # 投票/评分场景不需要 chain-of-thought, 关闭 thinking 可:
    #   1. 消灭 empty_response 根因 (78/210 失败的根因)
    #   2. 速度 10x 提升 (39s→4s)
    #   3. token 消耗大幅降低 (1297 reasoning → 0)
    # 只对 dashscope provider 生效, deepseek 官方 API 无此参数会被忽略
    if "dashscope.aliyuncs.com" in base_url:
        payload.setdefault("enable_thinking", False)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    # 兼容 OpenAI 格式 endpoint (qwen dashscope / deepseek 都用)
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text = ""
    _finish = ""
    if "choices" in data and data["choices"]:
        text = data["choices"][0].get("message", {}).get("content", "") or ""
        # 2026-07-25 (failure DEB B①): finish_reason 透传 — 此前完全不读,
        #   max_tokens 截断 (length) 与"模型本就坏 JSON"不可区分
        _finish = str(data["choices"][0].get("finish_reason", "") or "")
        if not text.strip():
            raise ValueError(f"empty_response_from_{model_name}")
    usage = data.get("usage", {}) or {}
    # 2026-07-25 (缓存探针): cached_tokens 透传 — deepseek 隐式前缀缓存实测自动生效
    #   (同前缀第2发 cached=3072/3298, 时延-45%), 此前不采=命中率/真实成本不可见
    _cached = int((usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0)
    return {
        "text": text,
        "finish_reason": _finish,
        "cached_tokens": _cached,
        "tokens_in": int(usage.get("prompt_tokens", 0) or 0),
        "tokens_out": int(usage.get("completion_tokens", 0) or 0),
        "raw": data,
    }


def _try_single_model(
    alias: str,
    cfg: Dict,
    system: str,
    user: str,
    *,
    temperature: float,
    max_tokens: int,
    timeout_override: Optional[int] = None,
    json_mode: bool = False,
    extra_body: Optional[Dict] = None,
    prompt_cache: bool = False,
) -> CallResult:
    """单一模型调用 + retry 1 次. 不做 schema 校验 / 不做 fallback. 失败返回 success=False."""
    model_cfg = _resolve_model(alias, cfg)
    if not model_cfg:
        return CallResult(success=False, error=f"unknown_alias:{alias}")

    api_key_env = model_cfg.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return CallResult(success=False, model_used=alias, error=f"missing_env:{api_key_env}")

    base_url = model_cfg.get("base_url", "")
    model_name = model_cfg.get("model_name", "")
    timeout = int(timeout_override or model_cfg.get("timeout", 30))
    cost_in_per_1k = float(model_cfg.get("cost_in", 0.0))
    cost_out_per_1k = float(model_cfg.get("cost_out", 0.0))

    start = time.time()
    last_error = None
    for attempt in range(2):  # 1 次 retry (网络抖动)
        try:
            r = _http_call(
                base_url, api_key, model_name, system, user,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                json_mode=json_mode, extra_body=extra_body, prompt_cache=prompt_cache,
            )
            elapsed_ms = (time.time() - start) * 1000
            cost = (r["tokens_in"] / 1000.0 * cost_in_per_1k +
                    r["tokens_out"] / 1000.0 * cost_out_per_1k)
            return CallResult(
                success=True, response=r["text"], model_used=f"{model_name}",
                latency_ms=elapsed_ms, cost_usd=cost,
                tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                finish_reason=r.get("finish_reason", ""),
                cached_tokens=r.get("cached_tokens", 0),
            )
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                logger.debug(f"[ModelCall·{alias}({model_name})] 第 {attempt+1} 次失败, 重试: {e}")
                time.sleep(1)  # 短暂退避
            else:
                logger.warning(f"[ModelCall·{alias}({model_name})] 全部 2 次失败: {e}")

    elapsed_ms = (time.time() - start) * 1000
    return CallResult(
        success=False, model_used=f"{model_name}", latency_ms=elapsed_ms, error=last_error,
    )


# ─── 内部: 缓存 ───
def _cache_path(feature: str, cache_dir: Optional[str] = None) -> Path:
    if not cache_dir:
        cfg = _get_llm_config()
        cache_dir = cfg.get("defaults", {}).get("cache_dir", "data/llm_cache")
    p = Path(__file__).resolve().parent.parent.parent / cache_dir
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{feature}.json"


def _save_cache(feature: str, response: Union[str, Dict]):
    try:
        path = _cache_path(feature)
        payload = {
            "response": response,
            "saved_at": time.time(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"[ModelCall·cache] 保存失败 {feature}: {e}")


def _load_cache(feature: str, max_age_hours: int = 24) -> Optional[Union[str, Dict]]:
    try:
        path = _cache_path(feature)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        saved_at = float(payload.get("saved_at", 0))
        if (time.time() - saved_at) > max_age_hours * 3600:
            return None
        return payload.get("response")
    except Exception as e:
        logger.debug(f"[ModelCall·cache] 读取失败 {feature}: {e}")
        return None


# ─── 公开主入口 ───
def call_llm(
    feature: str,
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.1,
    max_tokens: int = 2000,
    schema_required: Optional[Dict[str, type]] = None,
    schema_optional: Optional[Dict[str, type]] = None,
    schema_enums: Optional[Dict[str, List[Any]]] = None,
    timeout: Optional[int] = None,
    cross_check: bool = False,
    json_mode: bool = False,
    extra_body: Optional[Dict] = None,
) -> CallResult:
    """统一 LLM 调用入口

    Args:
        feature: yaml llm.features 中的 key, 例 "premarket_planner"
        prompt: user prompt
        system: system prompt (含 citation 强制指令推荐)
        schema_*: JSON Schema, 非 None 时 response 自动 parse + validate;
                  schema 失败 → fallback (不返回非法 dict)
        cross_check: True 时双调 primary + fallback, divergence 仲裁 (默认 false)

    Returns:
        CallResult: 含 response / model_used / cost / latency / fallback_used / schema_passed
        失败时 success=False, 调用方根据 error 字段处理.

    Flow:
        1. 读 yaml llm.features[feature] → primary, fallback
        2. primary 调 → 成功 → schema 校验 → 缓存 → return
        3. primary fail → fallback 调 → 同样校验
        4. fallback fail → cache_on_fail? → 读 cache → return (with from_cache=True)
        5. 全失败 → CallResult(success=False) + audit log
        6. cross_check=True → 同时调 primary + fallback, _critique_results

    Examples:
        # 简单调用
        res = call_llm("thesis_inferer", "What's the thesis?")
        if res: print(res.response)

        # 带 schema 强校验
        res = call_llm(
            "premarket_planner", prompt,
            schema_required={"date": str, "targets": list},
            schema_enums={"market_regime": ["Risk-On", "Risk-Off", "Neutral"]},
        )
        if res: plan_dict = res.response  # 已 parse + validated
    """
    if not feature:
        return CallResult(success=False, error="empty_feature")

    cfg = _get_llm_config()
    route = _resolve_feature_route(feature, cfg)
    if route is None:
        logger.warning(f"[ModelCall] feature '{feature}' 未在 yaml.llm.features 配置")
        return CallResult(success=False, error=f"feature_not_routed:{feature}")

    # 2026-07-25 (thinking DEB E3 接线, 用户裁决): yaml 路由级 extra_body — per-feature
    #   thinking 灰度的基建开关 (如 route.extra_body.enable_thinking: true)。
    #   调用方显式传入优先 (dict 合并后者覆盖); yaml 无配置时行为不变。回滚: 删 yaml key。
    _route_eb = route.get("extra_body")
    if isinstance(_route_eb, dict) and _route_eb:
        extra_body = {**_route_eb, **(extra_body or {})}

    has_schema = (schema_required is not None)

    # ── cross_check 路径: 双调 primary + fallback, critique 仲裁 ──
    if cross_check and route.get("fallback"):
        return _cross_check_call(
            feature, route, prompt, system, cfg,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            schema_required=schema_required,
            schema_optional=schema_optional, schema_enums=schema_enums,
            json_mode=json_mode, extra_body=extra_body,
        )

    # ── 标准路径: primary → fallback → cache ──
    primary = route.get("primary")
    fallback = route.get("fallback")
    cache_on_fail = route.get("cache_on_fail", False)

    for idx, alias in enumerate([primary, fallback]):
        if not alias:
            continue
        res = _try_single_model(
            alias, cfg, system, prompt,
            temperature=temperature, max_tokens=max_tokens, timeout_override=timeout,
            json_mode=json_mode, extra_body=extra_body,
            prompt_cache=bool(route.get("prompt_cache", False)),
        )
        if not res.success:
            # L2-B 修复 (2026-05-19): API 失败也记 audit, 否则失败原因 (timeout/auth/rate_limit) 不可观测
            _record_audit(feature, res, prompt)
            continue

        # schema 校验 (若指定)
        parsed = res.response
        schema_passed = None
        if has_schema:
            try:
                from app.llm.features.json_schema_guard import parse_and_validate
                parsed = parse_and_validate(
                    res.response or "",
                    schema_required, schema_optional, schema_enums,
                    feature=feature,
                )
            except Exception as e:
                logger.warning(f"[ModelCall·{feature}·schema] 异常: {e}")
                parsed = None
            schema_passed = parsed is not None
            if not schema_passed:
                # 2026-07-25 (failure DEB B②, 双信号触发): schema 失败 且 finish_reason=length
                #   → 同模型降 max_tokens 自救一次 (agent_bridge L1059 自救模式上提标准链).
                #   仅双信号: 只 parse 失败=模型能力问题降无益; 只 length=输出可能完整可解。
                # [DEB-20260829 第12轮 2026-09-01, 用户裁"同意"] 默认翻 False 停用:
                #   历史触发面仅 2 次 (均 9/1 UE 双臂, llm_audit 实证) 成功 0 次; ×0.5 对
                #   "内容真超限"数学上必死 (输出 >C 则必 >C/2), 当晚双臂各耗 ~59s 全部白费;
                #   升档方案同弃 — 唯一触发场景 UE 满载 16000 tok ≈210-230s, 再升必越实体
                #   timeout=300。length 截断 → 直接走 fallback 换模型 (本分支自然落空)。
                #   回滚: yaml llm.retry_downshift_enabled=true (代码路径保留, 失败现已入 audit)。
                if (res.finish_reason == "length"
                        and bool(cfg.get("retry_downshift_enabled", False))
                        and (max_tokens or 0) > 1024):
                    _ds_tokens = max(1024, int((max_tokens or 2048) * 0.5))
                    logger.warning(
                        f"[ModelCall·{feature}·truncat_downshift] alias={alias} 截断+parse失败 "
                        f"→ 降 max_tokens {max_tokens}→{_ds_tokens} 自救重试"
                    )
                    _ds_res = _try_single_model(
                        alias, cfg, system,
                        prompt + "\n\n(输出被截断过一次: 请精简内容, 保证 JSON 完整闭合)",
                        temperature=temperature, max_tokens=_ds_tokens, timeout_override=timeout,
                        json_mode=json_mode, extra_body=extra_body,
                        prompt_cache=bool(route.get("prompt_cache", False)),
                    )
                    if _ds_res.success:
                        try:
                            _ds_parsed = parse_and_validate(
                                _ds_res.response or "", schema_required, schema_optional,
                                schema_enums, feature=feature)
                        except Exception:
                            _ds_parsed = None
                        if _ds_parsed is not None:
                            _ds_res.fallback_used = (idx > 0)
                            _ds_res.schema_passed = True
                            _ds_res.response = _ds_parsed
                            _record_audit(feature, _ds_res, prompt)
                            if cache_on_fail and _ds_res.success:
                                _save_cache(feature, _ds_res.response)
                            return _ds_res
                        # 重试 API 成功但 schema 仍败 → 标记后入 audit
                        _ds_res.error = f"downshift_retry_schema_failed_on_{alias}"
                    else:
                        # 重试 API 失败 → 保留底层错误 (timeout/network) 但打上重试标记
                        _ds_res.error = f"downshift_retry_failed_on_{alias}: {_ds_res.error}"
                    # [第12轮] 降档重试失败入 audit — 原仅成功路径记录, 失败的 token 花费
                    #   与耗时完全不可观测 (9/1 双臂各 ~59s 白费即靠时间差推断)。真实成本
                    #   应入账: 这些 token 确实花了。
                    _record_audit(feature, _ds_res, prompt)
                # schema 失败 → 视为本模型失败, 走 fallback
                # L2-B 修复 (2026-05-19): schema 失败时记 audit 保留 raw response.
                # 5/15 thesis_inferer 5 笔 100% 失败 (response_len=0), 但 raw 没保留 → 无法诊断.
                # 现在 res.response 仍是 LLM 原始 text (尚未替换为 parsed dict),
                # _record_audit 会写入前 1000 字 → audit DB 可看到 LLM 实际输出, 数据驱动改 schema.
                res.error = f"schema_failed_on_{alias}"
                res.schema_passed = False
                _record_audit(feature, res, prompt)
                logger.warning(
                    f"[ModelCall·{feature}·schema_failed] alias={alias} "
                    f"raw_len={len(res.response or '')} raw_head={(res.response or '')[:200]!r}"
                )
                continue

        res.fallback_used = (idx > 0)
        res.schema_passed = schema_passed
        if has_schema:
            res.response = parsed   # 返回 dict (已 parse)

        # 审计 + 缓存 (成功路径)
        _record_audit(feature, res, prompt)
        if cache_on_fail and res.success:
            _save_cache(feature, res.response)

        return res

    # 全失败 → 缓存兜底
    if cache_on_fail:
        cached = _load_cache(feature, max_age_hours=24)
        if cached is not None:
            logger.warning(f"[ModelCall·{feature}] 全部模型失败, 使用 24h 缓存")
            return CallResult(
                success=True, response=cached, model_used="cache",
                from_cache=True, fallback_used=True,
            )

    return CallResult(success=False, error="all_models_failed", fallback_used=True)


def _cross_check_call(
    feature: str, route: Dict, prompt: str, system: str, cfg: Dict,
    *, temperature: float, max_tokens: int, timeout: Optional[int],
    schema_required, schema_optional, schema_enums,
    json_mode: bool = False, extra_body: Optional[Dict] = None,
) -> CallResult:
    """关键场景: primary 出方案 + fallback critique 评审 (串行接力)

    比"并行双调对方向" 更可靠 — 第 2 个 LLM 能看见第 1 个的输出, 不是凭空生成.
    任何环节失败 → 退化为单模型路径.
    """
    primary = route["primary"]
    critic = route["fallback"]

    # Resolve real model names for reporting
    def _real_name(alias):
        try:
            mc = cfg.get("models", {}).get(alias, {})
            return mc.get("model_name", alias)
        except Exception:
            return alias
    primary_real = _real_name(primary)
    critic_real = _real_name(critic)

    # Step 1: primary 出方案
    res_primary = _try_single_model(
        primary, cfg, system, prompt,
        temperature=temperature, max_tokens=max_tokens, timeout_override=timeout,
    )
    if not res_primary.success:
        logger.warning(f"[ModelCall·{feature}·cross] primary {primary} 失败, 退化标准路径")
        # 退化到标准 fallback
        return call_llm(
            feature, prompt, system=system,
            temperature=temperature, max_tokens=max_tokens,
            schema_required=schema_required, schema_optional=schema_optional,
            schema_enums=schema_enums, timeout=timeout, cross_check=False,
        )

    # Step 2: critic 评审
    critique_user = (
        f"以下是第一位分析师的方案, 请独立评审:\n\n"
        f"---方案开始---\n{res_primary.response}\n---方案结束---\n\n"
        f"请检查: 1) 数据依据是否充分 2) 方向逻辑是否合理 3) 风险是否被低估 "
        f"4) 是否存在幻觉/无依据陈述\n"
        f"返回 JSON: {{\"agree\": true/false, \"severity\": 0-1, \"reason\": \"...\", "
        f"\"corrections\": [\"...\"]}}"
    )
    critic_system = "你是严谨的量化分析评审, 独立思考, 找出方案漏洞."
    res_critic = _try_single_model(
        critic, cfg, critic_system, critique_user,
        temperature=0.05,  # 评审需要确定性
        max_tokens=800, timeout_override=timeout,
    )

    # Critique 失败 → 仅返回 primary (degraded mode)
    if not res_critic.success:
        logger.info(f"[ModelCall·{feature}·cross] critic 失败, 仅 primary 结果")
        res_primary.cross_check = {"degraded": True, "reason": res_critic.error}
        return _finalize_with_schema(feature, res_primary, schema_required, schema_optional, schema_enums)

    # 解析 critique
    try:
        from app.llm.features.json_schema_guard import parse_json_strict
        critique_obj = parse_json_strict(res_critic.response or "") or {}
    except Exception:
        critique_obj = {}

    agree = bool(critique_obj.get("agree", True))
    severity = float(critique_obj.get("severity", 0.0) or 0)
    threshold = float(cfg.get("defaults", {}).get("divergence_threshold", 0.5))

    # 分歧严重 → 拒决策
    if not agree and severity >= threshold:
        logger.warning(
            f"[ModelCall·{feature}·cross] 🚨 critic 严重不同意 (severity={severity:.2f}>={threshold}), 拒方案"
        )
        return CallResult(
            success=False,
            error="cross_check_rejected",
            model_used=f"{primary_real}+{critic_real}",
            latency_ms=res_primary.latency_ms + res_critic.latency_ms,
            cost_usd=res_primary.cost_usd + res_critic.cost_usd,
            cross_check={
                "agreed": False,
                "severity": severity,
                "critic_reason": critique_obj.get("reason", ""),
                "corrections": critique_obj.get("corrections", []),
            },
        )

    # 通过 → 返回 primary (附 critique 元数据)
    res_primary.model_used = f"{primary_real}+critique_by_{critic_real}"
    res_primary.latency_ms += res_critic.latency_ms
    res_primary.cost_usd += res_critic.cost_usd
    res_primary.cross_check = {
        "agreed": agree,
        "severity": severity,
        "critic_reason": critique_obj.get("reason", ""),
    }
    return _finalize_with_schema(feature, res_primary, schema_required, schema_optional, schema_enums)


def _finalize_with_schema(feature, res, schema_required, schema_optional, schema_enums):
    """在已 success=True 的 CallResult 上做 schema 校验. schema 失败 → success=False."""
    if schema_required is None:
        _record_audit(feature, res, "")
        return res
    try:
        from app.llm.features.json_schema_guard import parse_and_validate
        parsed = parse_and_validate(
            res.response or "", schema_required, schema_optional, schema_enums,
            feature=feature,
        )
    except Exception as e:
        logger.warning(f"[ModelCall·{feature}·schema] 异常: {e}")
        parsed = None
    res.schema_passed = parsed is not None
    if parsed is None:
        res.success = False
        res.error = "schema_failed"
        return res
    res.response = parsed
    _record_audit(feature, res, "")
    return res


def _record_audit(feature: str, res: CallResult, prompt_sample: str):
    """复用现有 audit_db.record_call. 失败不阻塞."""
    try:
        from app.llm.features.audit_db import record_call
        record_call(
            feature=feature,
            model=res.model_used,
            prompt=prompt_sample[:500] if prompt_sample else "",
            response=(json.dumps(res.response)[:1000] if isinstance(res.response, dict)
                      else (res.response or "")[:1000]),
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            cost_usd=res.cost_usd,
            latency_ms=int(res.latency_ms),
            success=res.success,
            error_msg=" ".join(filter(None, [
                res.error or "",
                f"finish_reason={res.finish_reason}" if res.finish_reason and res.finish_reason != "stop" else "",
                f"cached_tokens={res.cached_tokens}" if res.cached_tokens else "",
            ])),
        )
    except Exception as e:
        logger.debug(f"[ModelCall·audit] {e}")


# ─── 工具: prompt 哈希 (供 Prompt 版本管理用) ───
def prompt_hash(system: str, user: str) -> str:
    """生成 prompt 内容 hash, 供 audit_db 追溯 '今天 plan 用的是哪版 prompt'"""
    combined = (system or "") + "\n---\n" + (user or "")
    return hashlib.md5(combined.encode("utf-8")).hexdigest()[:12]
