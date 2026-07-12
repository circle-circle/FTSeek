"""
LLM Helper (L0 2026-05-14) — 各 feature 共用的 LLM 调用包装

提供:
- 统一调用入口 call_llm(feature, prompt, ...) 自动 audit + budget check
- 自动 fallback (如 Qwen-Max 失败 → DeepSeek)
- 自动超时
- 默认 disabled feature 直接 return None
"""
from __future__ import annotations
import logging
import time
from typing import Any, Dict, Optional, Tuple

from .feature_flags import is_enabled, get_per_feature_budget
from .audit_db import record_call, get_feature_cost_today

logger = logging.getLogger(__name__)


def call_llm(
    feature: str,
    prompt: str,
    system: str = "",
    model: str = "qwen",
    temperature: float = 0.1,
    max_tokens: int = 1000,
    timeout: int = 30,
    require_enabled: bool = True,
) -> Optional[str]:
    """
    统一 LLM 调用 (含 audit + budget 检查).

    Args:
        feature: feature key (用于 flag 检查 + audit)
        require_enabled: True (默认) → feature 未启用直接 return None

    Returns: response 文本 or None (各种失败情况)
    """
    if require_enabled and not is_enabled(feature):
        return None

    # per-feature 预算
    feature_budget = get_per_feature_budget(feature)
    if feature_budget is not None:
        spent = get_feature_cost_today(feature)
        if spent >= feature_budget:
            logger.warning(f"[LLM·{feature}] 单 feature 预算耗尽 ${spent:.2f} >= ${feature_budget:.2f}, 跳过")
            return None

    # 调用 — 统一走 model_call (YAML 路由 / SSOT)
    start = time.time()
    response: Optional[str] = None
    error_msg: Optional[str] = None
    cost = 0.0
    tokens_in = 0
    tokens_out = 0
    actual_model = model
    try:
        from app.llm.model_call import call_llm as _model_call, CallResult
        res: Optional[CallResult] = _model_call(
            feature,
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if res:
            actual_model = res.model_used or model
            cost = res.cost_usd or 0.0
            tokens_in = res.tokens_in or 0
            tokens_out = res.tokens_out or 0
            if res.success:
                response = res.response if isinstance(res.response, str) else None
            else:
                error_msg = res.error or "model_call failed"
                logger.warning(f"[LLM·{feature}] 调用失败: {error_msg}")
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"[LLM·{feature}] 调用异常: {e}")

    latency_ms = (time.time() - start) * 1000
    record_call(
        feature=feature,
        model=actual_model,
        prompt=prompt,
        response=response,
        success=bool(response and not error_msg),
        cost_usd=cost,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        error_msg=error_msg,
    )
    return response
