"""
Claude Code Agent 封装 — 通过 Bailian API 调用 deepseek v4 (或可配置模型)

设计:
- 与 Hermes Agent 通过 Bailian API 并行调用
- 不依赖 ACP 子进程 (更稳定、更易重试)
- 支持分类重试 (超时/解析错误/API限流)
"""
import json
import logging
import os
import threading
import time
import random
from typing import Dict, Any, Optional
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("ftseek_us.debate.claude_runner")

# 加载环境变量
_env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)


# =====================================================================
# 分类重试策略
# =====================================================================

class RetryPolicy:
    """分类重试策略"""
    
    MAX_RETRIES = 2
    BASE_DELAY = 5
    MAX_DELAY = 60
    BACKOFF_FACTOR = 2
    JITTER = 0.1
    
    STRATEGIES = {
        "timeout":            {"action": "retry",   "backoff": "exponential"},
        "json_parse_error":   {"action": "retry",   "backoff": "switch_model"},
        "model_not_found":    {"action": "retry",   "backoff": "switch_model"},
        "api_rate_limit":     {"action": "retry",   "backoff": "linear_long"},
        "api_error":          {"action": "retry",   "backoff": "exponential"},
        "hermes_unavailable": {"action": "fallback", "backoff": "none"},
        "file_permission":    {"action": "alert",   "backoff": "none"},
    }
    
    FALLBACK_MODELS = {
        "deepseek-v4-pro": "qwen-max",
        "deepseek-chat": "qwen-max",
        "qwen-max": "minimax-m2.5",
    }
    
    def get_delay(self, attempt: int, backoff_type: str = "exponential") -> float:
        if backoff_type == "exponential":
            delay = min(self.BASE_DELAY * (self.BACKOFF_FACTOR ** attempt), self.MAX_DELAY)
        elif backoff_type == "linear_long":
            delay = min(self.BASE_DELAY * 3 * (attempt + 1), self.MAX_DELAY * 2)
        else:
            delay = self.BASE_DELAY
        
        jitter = delay * self.JITTER * random.random()
        return delay + jitter
    
    def should_retry(self, failure_type: str) -> bool:
        strategy = self.STRATEGIES.get(failure_type, {})
        return strategy.get("action") == "retry"
    
    def get_backoff_type(self, failure_type: str) -> str:
        strategy = self.STRATEGIES.get(failure_type, {})
        return strategy.get("backoff", "exponential")
    
    def should_switch_model(self, failure_type: str) -> bool:
        strategy = self.STRATEGIES.get(failure_type, {})
        return strategy.get("backoff") == "switch_model"


# =====================================================================
# Claude Code Runner
# =====================================================================

class ClaudeCodeRunner:
    """Claude Code Agent — 通过 Bailian API 调用 deepseek 或其他模型"""
    
    def __init__(self, model: str = None, timeout: int = 90):
        self.default_model = model or os.getenv("CLAUDE_MODEL", "deepseek-v4-pro")
        self.timeout = timeout
        self.retry_policy = RetryPolicy()
        
        # API 配置 (复用 Bailian)
        self.base_url = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = os.getenv("BAILIAN_API_KEY", "")
        if not self.api_key:
            logger.warning("[ClaudeRunner] BAILIAN_API_KEY 为空，LLM 调用将失败")
        
        logger.info(f"[ClaudeRunner] 已初始化 | model={self.default_model} | timeout={timeout}s")
    
    def run_analysis(self, prompt: str, system_prompt: str = None,
                     round_num: int = 1, context: dict = None) -> Optional[Dict]:
        """
        运行 Claude Code 分析
        
        Args:
            prompt: 用户 prompt
            system_prompt: 系统 prompt
            round_num: 辩论轮次
            context: 额外上下文
        
        Returns:
            解析后的 JSON dict, 失败返回 None
        """
        current_model = self.default_model
        
        for attempt in range(self.retry_policy.MAX_RETRIES + 1):
            try:
                response = self._call_llm(prompt, system_prompt, model=current_model)
                
                if response is None:
                    raise TimeoutError("LLM 返回空响应")
                
                # 解析 JSON
                parsed = self._parse_response(response)
                
                if parsed is not None:
                    logger.info(
                        f"[ClaudeRunner] Round {round_num} 成功 | "
                        f"model={current_model} | attempt={attempt}"
                    )
                    return parsed
                
                # JSON 解析失败 → 分类重试
                if self.retry_policy.should_switch_model("json_parse_error"):
                    fallback = self.retry_policy.FALLBACK_MODELS.get(current_model)
                    if fallback and fallback != current_model:
                        logger.warning(
                            f"[ClaudeRunner] JSON 解析失败, 切换模型 "
                            f"{current_model} → {fallback}"
                        )
                        current_model = fallback
                        continue
                
                raise ValueError("JSON 解析失败")
                
            except TimeoutError as e:
                failure_type = "timeout"
                error_msg = str(e)
            except json.JSONDecodeError as e:
                failure_type = "json_parse_error"
                error_msg = str(e)
            except Exception as e:
                error_str = str(e)
                if error_str.startswith("model_not_found:"):
                    failure_type = "model_not_found"
                else:
                    failure_type = "api_error"
                error_msg = error_str
            
            # 分类重试决策
            if not self.retry_policy.should_retry(failure_type):
                logger.warning(f"[ClaudeRunner] {failure_type}, 不重试: {error_msg}")
                return None
            
            # 等待重试
            if attempt < self.retry_policy.MAX_RETRIES:
                delay = self.retry_policy.get_delay(attempt, self.retry_policy.get_backoff_type(failure_type))
                logger.warning(
                    f"[ClaudeRunner] {failure_type}, 重试 {attempt+1}/{self.retry_policy.MAX_RETRIES} "
                    f"(延迟 {delay:.0f}s): {error_msg}"
                )
                time.sleep(delay)
                
                # 切换模型 (如果需要)
                if self.retry_policy.should_switch_model(failure_type):
                    fallback = self.retry_policy.FALLBACK_MODELS.get(current_model)
                    if fallback:
                        current_model = fallback
                        logger.info(f"[ClaudeRunner] 切换模型 → {fallback}")
            else:
                logger.error(f"[ClaudeRunner] 全部 {self.retry_policy.MAX_RETRIES+1} 次尝试失败: {error_msg}")
                return None
        
        return None
    
    def _call_llm(self, prompt: str, system_prompt: str = None,
                  model: str = None, temperature: float = 0.3) -> Optional[str]:
        """调用 LLM API — 2026-05-21 迁移到 UnifiedLLMService

        旧: requests 直连 + 手动错误分类
        新: YAML llm.features.claude_code 路由 + UnifiedLLMService
        """
        from app.llm.unified_service import UnifiedLLMService
        return UnifiedLLMService.call_text(
            "claude_code", prompt,
            system=system_prompt or "",
            temperature=temperature,
            max_tokens=4096,
        )
    
    def _parse_response(self, response: str) -> Optional[Dict]:
        """解析 LLM 响应为 JSON"""
        try:
            text = response.strip()
            
            # 清理 markdown 代码块
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            
            return json.loads(text)
            
        except json.JSONDecodeError as e:
            logger.warning(f"[ClaudeRunner] JSON 解析失败: {e}")
            logger.debug(f"[ClaudeRunner] 原始响应: {response[:500]}")
            return None


# =====================================================================
# 模块级单例
# =====================================================================

_claude_runner: Optional[ClaudeCodeRunner] = None
_claude_runner_lock = threading.Lock()


def get_claude_runner(model: str = None, timeout: int = 180) -> ClaudeCodeRunner:
    global _claude_runner
    # P0 修复 (2026-05-03): 加锁防止并发初始化竞态
    with _claude_runner_lock:
        if _claude_runner is None:
            _claude_runner = ClaudeCodeRunner(model=model, timeout=timeout)
    return _claude_runner


def reset_claude_runner():
    """测试用: 重置单例"""
    global _claude_runner
    with _claude_runner_lock:
        _claude_runner = None
