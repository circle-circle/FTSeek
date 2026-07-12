#!/usr/bin/env python3
"""
FTSeek-US 多模型 LLM 客户端 — 已废弃 (DEPRECATED)

⚠️ 2026-07-06: 该模块已废弃。新代码请使用 SSOT 入口:
  - app.llm.model_call.call_llm(feature, prompt, ...)
  - app.llm.unified_service.UnifiedLLMService.call_text(...)

保留此文件仅用于兼容旧调用，所有路由 / API Key / 降级 / 审计
均由 model_call 统一管理。
"""
import os
import json
import time
import logging
import threading
from typing import Dict, Any, Optional, List

logger = logging.getLogger("ftseek_us.llm.multi_model")


# ─────────────────────────────────────────────────────────────
#  模型注册表
# ─────────────────────────────────────────────────────────────

MODEL_REGISTRY = {
    "qwen": {
        "model": "qwen-max",
        "purpose": "comprehensive",
        "description": "通义千问: 综合策略分析、宏观事件传导",
        "cost_per_1k_input": 0.008,    # 元/千 token (approximate)
        "cost_per_1k_output": 0.02,
        "max_tokens": 8000,
        "temperature_range": (0.0, 1.0),
    },
    "deepseek": {
        "model": "deepseek-v4-pro",
        "purpose": "reasoning",
        "description": "DeepSeek: 逻辑推理、假设生成",
        "cost_per_1k_input": 0.004,
        "cost_per_1k_output": 0.016,
        "max_tokens": 8000,
        "temperature_range": (0.0, 1.0),
    },
    "minimax": {
        "model": "MiniMax-M2.5",
        "purpose": "sentiment",
        "description": "MiniMax: 新闻情绪、市场热度",
        "cost_per_1k_input": 0.005,
        "cost_per_1k_output": 0.01,
        "max_tokens": 4000,
        "temperature_range": (0.0, 1.0),
    },
    "glm": {
        "model": "glm-4.7",
        "purpose": "technical",
        "description": "智谱 GLM: 技术指标分析（备用）",
        "cost_per_1k_input": 0.005,
        "cost_per_1k_output": 0.01,
        "max_tokens": 8000,
        "temperature_range": (0.0, 1.0),
    },
}

# 默认用途映射
DEFAULT_MODEL_FOR = {
    "attribution": "qwen",         # 归因用 Qwen
    "proposal": "deepseek",        # 假设生成用 DeepSeek
    "sentiment": "minimax",        # 情绪分析用 MiniMax
    "fallback": "qwen",            # 降级用 Qwen
}


class MultiModelClient:
    """
    多模型 LLM 客户端
    
    用法:
        client = MultiModelClient()
        
        # 使用默认模型
        result = client.chat("分析这笔交易", system="你是分析师")
        
        # 指定模型
        result = client.chat("生成假设", system="...", model="deepseek")
        
        # 获取成本统计
        cost = client.get_cost_summary()
    """

    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
    ):
        self.api_key = api_key or os.environ.get("BAILIAN_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get(
                "BAILIAN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            )
        )
        
        self._cost_tracker: List[Dict] = []
        self._total_cost = 0.0
        self._call_count = 0
        self._lock = threading.Lock()
        
        # 预算控制 — 优先从 YAML 配置读取，fallback 到环境变量
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            llm_budget = cfg.get("llm_budget", {})
            self.daily_budget = float(llm_budget.get("daily_max_usd", 5.0))
            self.per_module = llm_budget.get("per_module", {})
            self.cost_alerts = llm_budget.get("cost_alerts", {"warn_pct": 0.7, "halt_pct": 1.0})
        except Exception:
            self.daily_budget = float(os.environ.get("LLM_DAILY_BUDGET_USD", "5.0"))
            self.per_module = {}
            self.cost_alerts = {"warn_pct": 0.7, "halt_pct": 1.0}
        self._today_cost = 0.0
        self._today = ""
        
        if not self.api_key:
            logger.warning("[MultiModelClient] BAILIAN_API_KEY 未配置，LLM 调用将失败")
    
    def chat(
        self,
        user_prompt: str,
        system: str = "",
        model: str = None,
        temperature: float = 0.1,
        max_tokens: int = 1000,
        timeout: int = 60,
    ) -> str:
        """
        调用 LLM
        
        Args:
            user_prompt: 用户消息
            system: 系统 prompt
            model: 模型名 ("qwen"/"deepseek"/"minimax"/"glm")，None 用默认
            temperature: 温度
            max_tokens: 最大输出 token
            timeout: 超时秒数
            
        Returns:
            LLM 响应文本，失败返回空字符串
        """
        # 确定模型
        if model not in MODEL_REGISTRY:
            model = DEFAULT_MODEL_FOR.get(model, "qwen")
        
        model_info = MODEL_REGISTRY.get(model, MODEL_REGISTRY["qwen"])
        model_name = model_info["model"]
        
        # 预算检查
        if not self._check_budget():
            logger.warning(f"[MultiModelClient] 日预算已用完 ({self.daily_budget} USD)，拒绝调用")
            return ""
        
        # 构建请求
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_prompt})
        
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        
        start_time = time.time()
        
        try:
            import urllib.request
            import urllib.error
            
            req = urllib.request.Request(
                self.base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
            
            # 提取响应文本
            if "choices" in response_data and response_data["choices"]:
                text = response_data["choices"][0].get("message", {}).get("content", "")
            else:
                text = ""
                logger.warning(f"[MultiModelClient] {model_name} 响应格式异常: {response_data}")
            
            # 统计成本
            usage = response_data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            cost = self._calculate_cost(model, input_tokens, output_tokens)
            
            elapsed = time.time() - start_time
            
            self._record_call(
                model=model_name,
                purpose=model_info["purpose"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                elapsed=elapsed,
                success=bool(text),
            )
            
            logger.debug(
                f"[MultiModelClient] {model_name}: {input_tokens}→{output_tokens} tokens, "
                f"cost=${cost:.4f}, time={elapsed:.1f}s"
            )
            
            return text
            
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[MultiModelClient] {model_name} 调用失败 ({elapsed:.1f}s): {e}")
            
            self._record_call(
                model=model_name,
                purpose=model_info["purpose"],
                input_tokens=0,
                output_tokens=0,
                cost=0.0,
                elapsed=elapsed,
                success=False,
            )
            
            return ""
    
    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """计算单次调用成本 (USD)"""
        info = MODEL_REGISTRY.get(model, MODEL_REGISTRY["qwen"])
        input_cost = (input_tokens / 1000) * info["cost_per_1k_input"]
        output_cost = (output_tokens / 1000) * info["cost_per_1k_output"]
        return input_cost + output_cost
    
    def _check_budget(self) -> bool:
        """检查日预算"""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        
        if today != self._today:
            self._today = today
            self._today_cost = 0.0
        
        return self._today_cost < self.daily_budget
    
    def _record_call(
        self,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        elapsed: float,
        success: bool,
    ):
        """记录调用"""
        with self._lock:
            self._call_count += 1
            self._total_cost += cost
            self._today_cost += cost
            
            self._cost_tracker.append({
                "model": model,
                "purpose": purpose,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
                "elapsed": elapsed,
                "success": success,
                "timestamp": time.time(),
            })
            
            # 保留最近 1000 条记录
            if len(self._cost_tracker) > 1000:
                self._cost_tracker = self._cost_tracker[-500:]
    
    def get_cost_summary(self, hours: int = 24) -> Dict:
        """获取成本统计"""
        cutoff = time.time() - (hours * 3600)
        
        recent = [c for c in self._cost_tracker if c["timestamp"] >= cutoff]
        
        by_model = {}
        for c in recent:
            m = c["model"]
            if m not in by_model:
                by_model[m] = {"calls": 0, "cost": 0.0, "tokens": 0, "failures": 0}
            by_model[m]["calls"] += 1
            by_model[m]["cost"] += c["cost"]
            by_model[m]["tokens"] += c["input_tokens"] + c["output_tokens"]
            if not c["success"]:
                by_model[m]["failures"] += 1
        
        return {
            "total_calls": len(recent),
            "total_cost": sum(c["cost"] for c in recent),
            "today_cost": self._today_cost,
            "daily_budget": self.daily_budget,
            "budget_remaining": max(0, self.daily_budget - self._today_cost),
            "by_model": by_model,
        }
    
    def is_budget_exhausted(self) -> bool:
        """日预算是否已用完"""
        return self._today_cost >= self.daily_budget


# ─────────────────────────────────────────────────────────────
#  全局单例
# ─────────────────────────────────────────────────────────────

_multi_model_client: Optional[MultiModelClient] = None

def get_multi_model_client(
    api_key: str = None,
    base_url: str = None,
) -> MultiModelClient:
    global _multi_model_client
    if _multi_model_client is None:
        logger.warning(
            "[DEPRECATED] get_multi_model_client() is deprecated; "
            "use app.llm.model_call.call_llm() or UnifiedLLMService instead."
        )
        _multi_model_client = MultiModelClient(api_key=api_key, base_url=base_url)
    return _multi_model_client
