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
        """Call LLM — 统一走 UnifiedLLMService (重试/降级/审计集中管理)

        Returns the raw text response, or "{}" on all failures.
        """
        from app.llm.unified_service import get_unified_llm

        _model = model or self.default_model
        _timeout = timeout or self.default_timeout

        svc = get_unified_llm(
            default_model=_model,
            default_timeout=_timeout,
            max_retries=self.max_retries,
            enable_thinking=self.enable_thinking,
        )
        result = await svc.call(
            messages, model=_model, timeout=_timeout,
            task_type="base_llm_service",
        )
        return result or "{}"

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
