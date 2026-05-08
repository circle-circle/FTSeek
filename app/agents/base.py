"""
Base LLM Service — unified async LLM call infrastructure.

Consolidates patterns repeated across 7+ call sites:
  - call_llm(): LLM invocation with retry, timeout, client lifecycle
  - extract_json(): JSON extraction from LLM output
  - call_and_parse(): convenience combo
  - run_sync(): sync wrapper via ThreadPoolExecutor

Usage:
    svc = BaseLLMService(config=llm_config, default_model="qwen3.6-flash")
    result = await svc.call_and_parse(messages)
    # or sync:
    result = BaseLLMService.run_sync(svc.call_and_parse(messages))
"""

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Any, Callable, Coroutine

logger = logging.getLogger("ftseek.agents.base")


class BaseLLMService:
    """Unified async LLM service with retry, JSON extraction, sync wrapper."""

    def __init__(
        self,
        config: Optional[Dict] = None,
        default_model: str = "qwen3.6-flash",
        default_timeout: int = 90,
        enable_thinking: bool = False,
        max_retries: int = 1,
    ):
        self._config = config or {}
        self.default_model = default_model
        self.default_timeout = default_timeout
        self.enable_thinking = enable_thinking
        self.max_retries = max_retries

    # ─── LLM invocation ─────────────────────────────────────────────

    async def call_llm(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        extra_body: Optional[Dict] = None,
    ) -> str:
        """Call LLM with retry, timeout, and safe client cleanup.

        Returns the raw text response, or "{}" on all failures.
        """
        _model = model or self.default_model
        _timeout = timeout or self.default_timeout

        _extra_body = dict(extra_body or {})
        if self.enable_thinking:
            _extra_body.setdefault("enable_thinking", True)
        if not _extra_body:
            _extra_body = None

        for attempt in range(self.max_retries + 1):
            try:
                from app.core.unified_llm_factory import get_llm_client

                client = get_llm_client(self._config)
                if not client:
                    logger.error("[BaseLLMService] LLM client unavailable")
                    return "{}"

                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=_model,
                        messages=messages,
                        extra_body=_extra_body,
                    ),
                    timeout=_timeout,
                )
                result = response.choices[0].message.content or "{}"
                # P0 修复 (2026-05-06): 禁止关闭全局单例 client
                # get_llm_client() 返回全局单例，close() 会销毁 httpx 连接
                # 导致后续所有 LLM 调用失败。factory 管理生命周期。
                return result

            except asyncio.TimeoutError:
                logger.warning(
                    "[BaseLLMService] Timeout model=%s timeout=%ds attempt=%d",
                    _model, _timeout, attempt + 1,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)  # 指数退避
            except Exception as e:
                logger.error(
                    "[BaseLLMService] LLM call failed model=%s attempt=%d: %s",
                    _model, attempt + 1, e,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)  # 指数退避

        return "{}"

    @staticmethod
    def _safe_close(client) -> None:
        try:
            client.close()
        except Exception:
            pass

    # ─── JSON extraction ────────────────────────────────────────────

    @staticmethod
    def extract_json(raw: str) -> Dict:
        """Extract JSON dict from LLM response text.

        Handles: direct JSON, ```json fences, ``` fences, nested braces.
        """
        if not raw:
            return {}

        cleaned = raw.strip()

        # Strip markdown code fences
        cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r'\n?\s*```\s*$', '', cleaned, flags=re.MULTILINE)

        # Direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Regex extraction (single and double-nested objects)
        match = re.search(
            r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}',
            cleaned, re.DOTALL,
        )
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return {}

    # ─── Convenience combo ──────────────────────────────────────────

    async def call_and_parse(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        extra_body: Optional[Dict] = None,
    ) -> Dict:
        """Call LLM and extract JSON result in one step."""
        raw = await self.call_llm(messages, model, timeout, extra_body)
        return self.extract_json(raw)

    # ─── Sync wrapper ────────────────────────────────────────────────

    @staticmethod
    def run_sync(
        coro_factory: Callable[..., Coroutine],
        *args: Any,
        timeout: int = 180,
        **kwargs: Any,
    ) -> Any:
        """Run an async function synchronously.

        Handles both running and non-running event loops.
        Replaces 3 duplicated sync-wrapper patterns across the codebase.

        Usage:
            result = BaseLLMService.run_sync(svc.call_and_parse, messages, timeout=60)
        """
        async def _runner():
            return await coro_factory(*args, **kwargs)

        try:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, _runner())
                return future.result(timeout=timeout)
        except RuntimeError:
            return asyncio.run(_runner())
