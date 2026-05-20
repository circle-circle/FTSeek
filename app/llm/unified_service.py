"""
统一 LLM 服务 (Unified LLM Service)

替代分散的:
  - BaseLLMService.call_llm() (会关闭全局单例 → P0 bug)
  - PreMarketAnalyst._call_llm() (无统一重试/降级)
  - next_day_preview 独立调用

特性:
1. 多模型支持 (每个请求指定模型)
2. 统一重试 + 指数退避
3. 模型降级策略 (YAML 配置驱动)
4. JSON 输出容错解析
5. 不关闭全局单例 client
6. 成本预算控制 (daily budget guard)
7. 响应缓存 (减少重复调用)
8. 特性路由 (task_type → model 映射)
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import requests
import time
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek.llm.unified_service")

# 指标文件路径
METRICS_DIR = Path(__file__).resolve().parent.parent.parent / "data"
METRICS_FILE = METRICS_DIR / "llm_metrics.jsonl"
# 预算追踪文件
BUDGET_FILE = METRICS_DIR / "llm_daily_budget.json"


class UnifiedLLMService:
    """统一 LLM 服务 — 支持多模型、重试、降级、JSON 解析、成本统计"""

    # 模型定价 (每百万 Token 价格, 人民币, DashScope 2026-05)
    # 来源: https://help.aliyun.com/zh/model-studio/developer-reference/tongyi-thousand-questions-metering-and-billing
    MODEL_PRICING = {
        # (input_price_per_M, output_price_per_M)
        "qwen3.6-plus":    (2.0,  6.0),
        "qwen3.6-flash":   (0.3,  0.6),
        "qwen-max":        (8.0,  24.0),
        "qwen-turbo":      (0.3,  0.6),
        "deepseek-v4-pro": (4.0,  12.0),
        "deepseek-v4-flash": (1.0, 3.0),
        "MiniMax-M2.5":    (5.0,  15.0),
    }

    # 硬编码降级链 (YAML 配置优先级更高，会覆盖)
    _DEFAULT_FALLBACK_CHAIN = {
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

        # ── YAML 配置驱动 (2026-05-21) ──
        llm_cfg = self._load_llm_config()

        # 特性路由: task_type → model
        self.feature_routes: Dict[str, str] = llm_cfg.get("features", {})
        # 去掉非路由键 (fallback_chain, budget, cache 等)
        self.feature_routes = {
            k: v for k, v in self.feature_routes.items()
            if isinstance(v, str) and k != "fallback_chain"
        }

        # 降级链: YAML > 硬编码默认
        yaml_chain = llm_cfg.get("features", {}).get("fallback_chain", {})
        self.FALLBACK_CHAIN = {**self._DEFAULT_FALLBACK_CHAIN, **yaml_chain}

        # 预算控制
        budget_cfg = llm_cfg.get("budget", {})
        self.budget_enabled = budget_cfg.get("enabled", True)
        self.budget_daily_max = float(budget_cfg.get("daily_max_cny", 10.0))
        self.budget_warning_threshold = float(budget_cfg.get("warning_threshold", 0.8))

        # 响应缓存
        cache_cfg = llm_cfg.get("cache", {})
        self.cache_enabled = cache_cfg.get("enabled", True)
        self.cache_dir = Path(cache_cfg.get("cache_dir", "/tmp/ftseek_llm_cache"))
        self.cache_ttl = int(cache_cfg.get("cache_ttl_seconds", 300))
        self.cache_on_fail = cache_cfg.get("cache_on_fail", False)

        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _load_llm_config(self) -> Dict:
        """从 unified_config.yaml 加载 llm 配置块"""
        try:
            cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"
            if cfg_path.exists():
                import yaml
                with open(cfg_path, "r", encoding="utf-8") as f:
                    full_cfg = yaml.safe_load(f) or {}
                return full_cfg.get("llm", {})
        except Exception as e:
            logger.debug(f"[UnifiedLLM] YAML 配置加载失败，使用默认值: {e}")
        return {}

    async def call(self, messages: List[Dict], model: str = None,
                    timeout: int = None, temperature: float = 0.3,
                    max_tokens: int = 4096, task_type: str = "unknown") -> Optional[str]:
        """
        调用 LLM (带重试+降级+路由+预算+缓存)

        Args:
            task_type: 业务场景标识 (如 "veto", "premarket", "consensus")

        Returns: 原始文本，失败返回 None
        """
        # ── 特性路由: task_type 优先于显式 model ──
        if model is None and task_type in self.feature_routes:
            model = self.feature_routes[task_type]
            logger.debug(f"[UnifiedLLM] 路由 {task_type} → {model}")

        model = model or self.default_model
        timeout = timeout or self.default_timeout
        max_tokens = 8192 if "deepseek" in model.lower() else max_tokens

        # ── 预算检查 ──
        if self.budget_enabled and self._budget_exceeded():
            logger.error(
                f"[UnifiedLLM] 日预算超限: "
                f"{self._get_daily_cost():.4f} >= {self.budget_daily_max} CNY, 拒绝调用"
            )
            return None

        # ── 缓存检查 (仅对幂等查询) ──
        cache_key = self._cache_key(messages, model, temperature)
        if self.cache_enabled and cache_key:
            cached = self._cache_get(cache_key)
            if cached is not None:
                logger.debug(f"[UnifiedLLM] 缓存命中: {task_type}")
                self._record_metrics(
                    model=model, task_type=task_type, status="cache_hit",
                    latency_ms=0, usage={}, attempt=1,
                )
                return cached

        start_time = time.time()

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
                result, usage = await self._http_call(
                    model=current_model,
                    messages=messages,
                    timeout=timeout,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                
                latency_ms = int((time.time() - start_time) * 1000)
                
                # 提取 prompt 内容用于审计 (2026-05-21 增强)
                prompt_text = ""
                if messages:
                    # 提取 system + user 消息
                    for msg in messages:
                        role = msg.get("role", "")
                        content = msg.get("content", "")
                        if role in ("system", "user"):
                            prompt_text += f"[{role}]: {content}\n"
                
                self._record_metrics(
                    model=current_model,
                    task_type=task_type,
                    status="success" if result else "empty",
                    latency_ms=latency_ms,
                    usage=usage,
                    attempt=attempt + 1,
                    prompt=prompt_text,
                    response=result
                )

                if result:
                    # ── 缓存写入 ──
                    if self.cache_enabled and cache_key:
                        self._cache_put(cache_key, result)
                    # ── 预算告警 ──
                    if self.budget_enabled:
                        daily = self._get_daily_cost()
                        if daily >= self.budget_daily_max * self.budget_warning_threshold:
                            logger.warning(
                                f"[UnifiedLLM] 日预算 {self.budget_warning_threshold:.0%} 告警: "
                                f"{daily:.4f}/{self.budget_daily_max} CNY"
                            )
                    return result

            except Exception as e:
                logger.error(
                    f"[UnifiedLLM] 调用失败 model={current_model} "
                    f"attempt={attempt+1}: {type(e).__name__}: {e}"
                )
                self._record_metrics(
                    model=current_model,
                    task_type=task_type,
                    status="error",
                    latency_ms=int((time.time() - start_time) * 1000),
                    usage={},
                    attempt=attempt + 1,
                    error=str(e)
                )

        return None

    async def call_with_tools(self, messages: List[Dict], tools: List[Dict],
                               model: str = None, timeout: int = None,
                               tool_choice: str = "auto",
                               temperature: float = 0.3, max_tokens: int = 4096,
                               task_type: str = "unknown") -> Optional[Dict]:
        """
        调用 LLM with native function calling (带重试+降级)

        Returns: {"content": str, "tool_calls": [...]} 或 None
        """
        model = model or self.default_model
        timeout = timeout or self.default_timeout
        start_time = time.time()

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                delay = 2 ** attempt
                logger.warning(f"[UnifiedLLM] 重试等待 {delay}s (attempt={attempt})")
                await asyncio.sleep(delay)

            current_model = model
            if attempt > 0 and model in self.FALLBACK_CHAIN:
                fallback_idx = min(attempt - 1, len(self.FALLBACK_CHAIN[model]) - 1)
                current_model = self.FALLBACK_CHAIN[model][fallback_idx]
                logger.warning(f"[UnifiedLLM] 降级到 {current_model}")

            try:
                result, usage = await self._http_call_with_tools(
                    model=current_model, messages=messages,
                    tools=tools, tool_choice=tool_choice,
                    timeout=timeout, temperature=temperature,
                    max_tokens=max_tokens,
                )
                latency_ms = int((time.time() - start_time) * 1000)
                self._record_metrics(
                    model=current_model, task_type=task_type,
                    status="success" if result else "empty",
                    latency_ms=latency_ms, usage=usage, attempt=attempt + 1,
                )
                if result:
                    return result
            except Exception as e:
                logger.error(
                    f"[UnifiedLLM] call_with_tools 失败 model={current_model} "
                    f"attempt={attempt+1}: {type(e).__name__}: {e}"
                )
                self._record_metrics(
                    model=current_model, task_type=task_type,
                    status=f"error:{type(e).__name__}",
                    latency_ms=int((time.time() - start_time) * 1000),
                    usage={}, attempt=attempt + 1,
                )

        return None

    async def _http_call_with_tools(self, model: str, messages: List[Dict],
                                      tools: List[Dict], tool_choice: str,
                                      timeout: int, temperature: float,
                                      max_tokens: int) -> Tuple[Optional[Dict], Dict]:
        """HTTP 调用 with native function calling"""
        if not self.api_key:
            return None, {}

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload, timeout=timeout,
            )
        )

        if resp.status_code in (429, 404):
            logger.warning(f"[UnifiedLLM] API {resp.status_code}, model={model}")
            return None, {}
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        choices = data.get("choices")
        if not choices:
            return None, usage

        msg = choices[0].get("message", {})
        result = {}
        if msg.get("content"):
            result["content"] = msg["content"]
        if msg.get("tool_calls"):
            result["tool_calls"] = [
                {
                    "id": tc["id"],
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in msg["tool_calls"]
            ]

        return result if result else None, usage

    async def call_and_parse(self, messages: List[Dict], model: str = None,
                              timeout: int = None, task_type: str = "unknown",
                              schema_required: Dict = None) -> Dict:
        """调用 LLM 并解析 JSON (可选 schema 校验 2026-05-21)"""
        raw = await self.call(messages, model, timeout, task_type=task_type)
        if not raw:
            return {}
        
        # 如果提供了 schema，进行校验
        if schema_required:
            try:
                from app.llm.json_schema_guard import parse_and_validate
                result = parse_and_validate(raw, schema_required)
                if result:
                    return result
                else:
                    logger.warning(f"[UnifiedLLM] Schema 校验失败，返回空 dict")
                    return {}
            except Exception as e:
                logger.warning(f"[UnifiedLLM] Schema 校验异常：{e}")
                return self.extract_json(raw)
        
        return self.extract_json(raw)

    async def _http_call(self, model: str, messages: List[Dict],
                          timeout: int, temperature: float,
                          max_tokens: int) -> Tuple[Optional[str], Dict]:
        """
        直接 HTTP 调用 DashScope API
        
        Returns:
            Tuple of (content_text, usage_info)
        """
        if not self.api_key:
            return None, {}

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
            return None, {}

        if resp.status_code == 404:
            logger.warning(f"[UnifiedLLM] 模型不存在 (404): {model}")
            return None, {}

        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})

        choices = data.get("choices")
        if not choices:
            logger.warning(f"[UnifiedLLM] API 返回空 choices")
            return None, usage

        return choices[0].get("message", {}).get("content", ""), usage

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

    def _estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """估算单次调用成本 (人民币)"""
        pricing = self.MODEL_PRICING.get(model)
        if not pricing:
            return 0.0
        input_price, output_price = pricing
        return (prompt_tokens / 1_000_000 * input_price) + (completion_tokens / 1_000_000 * output_price)

    def _record_metrics(self, model: str, task_type: str, status: str, 
                        latency_ms: int, usage: Dict, attempt: int, error: str = None,
                        prompt: str = None, response: str = None):
        """
        记录 LLM 调用指标到 data/llm_metrics.jsonl
        
        2026-05-21 增强：添加 prompt 和 response 内容用于审计
        """
        try:
            METRICS_DIR.mkdir(parents=True, exist_ok=True)
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)
            cost = self._estimate_cost(model, prompt_tokens, completion_tokens)
            record = {
                "ts": int(time.time()),
                "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": model,
                "task_type": task_type,
                "status": status,
                "latency_ms": latency_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_cny": round(cost, 6),
                "attempt": attempt,
                "error": error[:50] if error else None,
                # 2026-05-21 增强：审计内容 (截断避免过大)
                "prompt_preview": prompt[:500] if prompt else None,
                "response_preview": response[:500] if response else None,
            }
            
            # Append to JSONL (atomic enough for append mode)
            with open(METRICS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[UnifiedLLM] 指标记录失败: {e}")

    # ── 缓存方法 ──────────────────────────────────────────────

    def _cache_key(self, messages: List[Dict], model: str, temperature: float) -> Optional[str]:
        """生成缓存键 (基于 prompt 内容 + model + temperature)"""
        if not messages:
            return None
        try:
            # 只取 user message 内容做 hash (system prompt 不变则不影响)
            user_content = ""
            for msg in messages:
                if msg.get("role") == "user":
                    user_content += msg.get("content", "")
            if not user_content:
                return None
            raw = f"{model}:{temperature}:{user_content[:2000]}"
            return hashlib.md5(raw.encode("utf-8")).hexdigest()
        except Exception:
            return None

    def _cache_get(self, key: str) -> Optional[str]:
        """读取缓存 (检查 TTL)"""
        try:
            cache_file = self.cache_dir / f"{key}.json"
            if not cache_file.exists():
                return None
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - data.get("ts", 0)
            if age > self.cache_ttl:
                cache_file.unlink(missing_ok=True)
                return None
            return data.get("response")
        except Exception:
            return None

    def _cache_put(self, key: str, response: str):
        """写入缓存"""
        try:
            cache_file = self.cache_dir / f"{key}.json"
            cache_file.write_text(
                json.dumps({"ts": time.time(), "response": response}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"[UnifiedLLM] 缓存写入失败: {e}")

    # ── 预算方法 ──────────────────────────────────────────────

    def _get_daily_cost(self) -> float:
        """计算今日累计成本 (从 metrics JSONL 读取)"""
        try:
            today = time.strftime("%Y-%m-%d")
            if not METRICS_FILE.exists():
                return 0.0
            total = 0.0
            with open(METRICS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if r.get("datetime", "").startswith(today):
                        total += r.get("cost_cny", 0)
            return total
        except Exception:
            return 0.0

    def _budget_exceeded(self) -> bool:
        """检查日预算是否超限"""
        return self._get_daily_cost() >= self.budget_daily_max

    @staticmethod
    def get_stats(days: int = 7) -> Dict:
        """
        获取 LLM 调用统计 (从 metrics 文件)
        """
        if not METRICS_FILE.exists():
            return {"error": "No metrics file found"}
        
        records = []
        cutoff = int(time.time()) - (days * 86400)
        
        try:
            with open(METRICS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip(): continue
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        records.append(r)
        except Exception:
            return {"error": "Failed to read metrics"}
            
        if not records:
            return {"total_calls": 0}

        total_calls = len(records)
        success_calls = len([r for r in records if r['status'] == 'success'])
        total_latency = sum(r.get('latency_ms', 0) for r in records)
        total_tokens = sum(r.get('total_tokens', 0) for r in records)
        total_cost = sum(r.get('cost_cny', 0) for r in records)
        
        # Model distribution
        model_stats = {}
        for r in records:
            m = r.get('model', 'unknown')
            if m not in model_stats:
                model_stats[m] = {"calls": 0, "tokens": 0, "cost": 0.0, "errors": 0}
            model_stats[m]["calls"] += 1
            model_stats[m]["tokens"] += r.get('total_tokens', 0)
            model_stats[m]["cost"] += r.get('cost_cny', 0)
            if r.get('status') != 'success':
                model_stats[m]["errors"] += 1
        # Round costs
        for m in model_stats:
            model_stats[m]["cost"] = round(model_stats[m]["cost"], 4)
            
        # Task distribution
        task_counts = {}
        for r in records:
            t = r.get('task_type', 'unknown')
            task_counts[t] = task_counts.get(t, 0) + 1

        return {
            "period_days": days,
            "total_calls": total_calls,
            "success_rate": f"{success_calls/total_calls:.1%}",
            "avg_latency_ms": int(total_latency / total_calls),
            "total_tokens": total_tokens,
            "total_cost_cny": round(total_cost, 4),
            "models": model_stats,
            "tasks": task_counts,
        }

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
