"""UnifiedLLMService — 美股全局唯一 LLM 调用入口

设计目标:
  - 所有模块通过 UnifiedLLMService.call() 或 call_async() 调用
  - 底层走 model_call.call_llm() (YAML 路由 / 重试 / 降级 / 审计 / schema / cache)
  - 支持 json_mode, extra_body, system, temperature, max_tokens, timeout
  - 零依赖: 仅 stdlib + model_call

使用方式:
  from app.llm.unified_service import UnifiedLLMService

  # 同步调用
  res = UnifiedLLMService.call("model_agent", prompt, system=sys, json_mode=True)
  if res: print(res.response)

  # 异步调用 (asyncio.to_thread 包装)
  res = await UnifiedLLMService.call_async("debate_engine", prompt, system=sys)

  # 简单文本返回 (兼容旧代码风格)
  text = UnifiedLLMService.call_text("news_classifier", prompt)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.llm.model_call import call_llm, CallResult

logger = logging.getLogger(__name__)


class UnifiedLLMService:
    """统一 LLM 调用服务 — 所有模块唯一入口"""

    @staticmethod
    def call(
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
        """同步调用 — 直接走 model_call.call_llm()

        Args:
            feature: YAML llm.features 中的 key (e.g. "model_agent")
            prompt: user prompt
            system: system prompt
            json_mode: True → response_format=json_object
            extra_body: 模型特定参数 (e.g. {"enable_thinking": False})
            schema_required: 必填字段 schema, 非 None 时自动 parse+validate
            cross_check: True → 双调 primary+fallback, critique 仲裁

        Returns:
            CallResult (success/response/model_used/cost/latency/fallback_used/schema_passed)
        """
        return call_llm(
            feature, prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            schema_required=schema_required,
            schema_optional=schema_optional,
            schema_enums=schema_enums,
            timeout=timeout,
            cross_check=cross_check,
            json_mode=json_mode,
            extra_body=extra_body,
        )

    @staticmethod
    async def call_async(
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
        """异步调用 — asyncio.to_thread 包装同步调用, 不阻塞 event loop"""
        return await asyncio.to_thread(
            UnifiedLLMService.call,
            feature, prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            schema_required=schema_required,
            schema_optional=schema_optional,
            schema_enums=schema_enums,
            timeout=timeout,
            cross_check=cross_check,
            json_mode=json_mode,
            extra_body=extra_body,
        )

    @staticmethod
    def call_text(
        feature: str,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int = 2000,
        json_mode: bool = False,
        extra_body: Optional[Dict] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """简单文本返回 — 兼容旧代码风格

        Returns:
            LLM 响应文本, 失败返回 None
        """
        res = UnifiedLLMService.call(
            feature, prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            extra_body=extra_body,
            timeout=timeout,
        )
        if res and res.success:
            return res.response
        logger.warning(f"[UnifiedLLMService·{feature}] 调用失败: {res.error if res else 'unknown'}")
        return None

    @staticmethod
    async def call_text_async(
        feature: str,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int = 2000,
        json_mode: bool = False,
        extra_body: Optional[Dict] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """异步简单文本返回"""
        res = await UnifiedLLMService.call_async(
            feature, prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            extra_body=extra_body,
            timeout=timeout,
        )
        if res and res.success:
            return res.response
        return None
