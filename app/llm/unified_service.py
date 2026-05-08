"""
统一 LLM 服务 (Unified LLM Service)

替代分散的:
  - BaseLLMService.call_llm() (会关闭全局单例 → P0 bug)
  - PreMarketAnalyst._call_llm() (无统一重试/降级)
  - next_day_preview 独立调用

特性:
1. 多模型支持 (每个请求指定模型)
2. 统一重试 + 指数退避
3. 模型降级策略
4. JSON 输出容错解析
5. 不关闭全局单例 client
"""

import asyncio
import json
import logging
import os
import re
import requests
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.llm.unified_service")


class UnifiedLLMService:
    """统一 LLM 服务 — 支持多模型、重试、降级、JSON 解析"""

    # 模型降级链 (主模型 → 备用1 → 备用2)
    FALLBACK_CHAIN = {
        "qwen3.6-plus": ["qwen-max", "qwen-turbo"],
        "qwen3.6-flash": ["qwen-turbo"],
        "deepseek-v4-pro": ["qwen-max"],
        "deepseek-v4-flash": ["qwen-turbo"],
        "MiniMax-M2.5": ["qwen-max"],
    }

    def __init__(self, config: Dict = None, default_model: str = "qwen3.6-plus",
                 default_timeout: int = 120, max_retries: int = 2,
                 enable_thinking: bool = False):
        self.config = config or {}
        self.default_model = default_model
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self.enable_thinking = enable_thinking

        # API 配置 (从环境变量读取)
        self.api_key = os.getenv("BAILIAN_API_KEY", "")
        self.base_url = os.getenv(
            "BAILIAN_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        if not self.api_key:
            logger.warning("[UnifiedLLMService] BAILIAN_API_KEY 未配置")

    async def call(self, messages: List[Dict], model: str = None,
                    timeout: int = None, temperature: float = 0.3,
                    max_tokens: int = 4096) -> Optional[str]:
        """
        调用 LLM (带重试+降级)

        Returns: 原始文本，失败返回 None
        """
        model = model or self.default_model
        timeout = timeout or self.default_timeout
        max_tokens = 8192 if "deepseek" in model.lower() else max_tokens

        for attempt in range(self.max_retries + 1):
            # 指数退避
            if attempt > 0:
                delay = 2 ** attempt
                logger.warning(f"[UnifiedLLM] 重试等待 {delay}s (attempt={attempt})")
                await asyncio.sleep(delay)

            current_model = model
            # 降级: 非首次尝试 → 切换备用模型
            if attempt > 0 and model in self.FALLBACK_CHAIN:
                fallback_idx = min(attempt - 1, len(self.FALLBACK_CHAIN[model]) - 1)
                current_model = self.FALLBACK_CHAIN[model][fallback_idx]
                logger.warning(f"[UnifiedLLM] 降级到 {current_model}")

            try:
                result = await self._http_call(
                    model=current_model,
                    messages=messages,
                    timeout=timeout,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if result:
                    return result

            except Exception as e:
                logger.error(
                    f"[UnifiedLLM] 调用失败 model={current_model} "
                    f"attempt={attempt+1}: {type(e).__name__}: {e}"
                )

        return None

    async def call_and_parse(self, messages: List[Dict], model: str = None,
                              timeout: int = None) -> Dict:
        """调用 LLM 并解析 JSON 结果"""
        raw = await self.call(messages, model, timeout)
        return self.extract_json(raw)

    async def _http_call(self, model: str, messages: List[Dict],
                          timeout: int, temperature: float,
                          max_tokens: int) -> Optional[str]:
        """直接 HTTP 调用 DashScope API (不依赖 AsyncOpenAI 单例)"""
        if not self.api_key:
            return None

        extra_body = {}
        if self.enable_thinking:
            extra_body["enable_thinking"] = True

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if extra_body:
            payload["extra_body"] = extra_body

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=timeout,
            )
        )

        if resp.status_code == 429:
            logger.warning(f"[UnifiedLLM] API 限流 (429), model={model}")
            return None

        if resp.status_code == 404:
            logger.warning(f"[UnifiedLLM] 模型不存在 (404): {model}")
            return None

        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices")
        if not choices:
            logger.warning(f"[UnifiedLLM] API 返回空 choices")
            return None

        return choices[0].get("message", {}).get("content", "")

    @staticmethod
    def extract_json(raw: str) -> Dict:
        """从 LLM 文本中提取 JSON (容错处理)"""
        if not raw:
            return {}

        text = raw.strip()

        # 去除 markdown 代码块
        text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n?\s*```\s*$', '', text, flags=re.MULTILINE)
        text = text.strip()

        # 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 提取第一个 {...}
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except Exception:
            pass

        # 双层嵌套
        match = re.search(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass

        logger.warning(f"[UnifiedLLM] JSON 解析失败: {text[:200]}")
        return {}

    @classmethod
    def run_sync(cls, coro, timeout: int = 150) -> Any:
        """在同步上下文中运行异步调用"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, coro)
                    return future.result(timeout=timeout)
            else:
                return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
            finally:
                loop.close()


# 全局单例
_unified_llm_service: Optional[UnifiedLLMService] = None


def get_unified_llm(config: Dict = None, **kwargs) -> UnifiedLLMService:
    global _unified_llm_service
    if _unified_llm_service is None or kwargs.get('force_new'):
        _unified_llm_service = UnifiedLLMService(config, **{
            k: v for k, v in kwargs.items()
            if k in ('default_model', 'default_timeout', 'max_retries', 'enable_thinking')
        })
    return _unified_llm_service
