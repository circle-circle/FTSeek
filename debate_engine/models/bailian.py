"""
百炼平台统一客户端

所有模型 (qwen/deepseek/glm/minimax) 共用一个 API Key，
通过 OpenAI 兼容接口调用，只需切换 model 参数。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _load_config() -> Dict[str, Any]:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


class BailianClient:
    """
    百炼平台统一客户端
    
    所有模型共用 BAILIAN_API_KEY，通过 OpenAI 兼容格式调用。
    """
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or _load_config()
        platform = self.config["platform"]
        env_key = platform.get("api_key_env", "BAILIAN_API_KEY")
        self.api_key = os.environ.get(env_key, "")
        self.base_url = platform.get("base_url", 
            "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        
        self.models = {}
        for m in self.config.get("models", []):
            if m.get("enabled", False) and m.get("mode") != "subprocess":
                self.models[m["name"]] = m
    
    def list_models(self) -> List[str]:
        """列出已启用的模型"""
        return list(self.models.keys())
    
    def select_models(self, count: int = 2, manual: Optional[List[str]] = None) -> List[str]:
        """
        选择辩论模型组合
        
        默认: cross_family (自动选不同 family)
        手动: 指定模型列表
        """
        if manual:
            return [m for m in manual if m in self.models]
        
        # cross_family: 每个 family 选一个
        families_seen = set()
        selected = []
        for name, cfg in self.models.items():
            fam = cfg.get("family", "")
            if fam not in families_seen:
                families_seen.add(fam)
                selected.append(name)
                if len(selected) >= count:
                    break
        return selected
    
    def call(self, model_name: str, prompt: str,
             system: str = "", max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """
        调用百炼模型 — 同步接口
        
        Returns:
            {"model": str, "family": str, "content": str, "parsed": dict|None, "usage": dict}
        """
        if not self.api_key:
            raise RuntimeError("BAILIAN_API_KEY not set")
        
        model_cfg = self.models.get(model_name, {})
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": messages,
                "temperature": model_cfg.get("temperature", 0.3),
                "max_tokens": max_tokens or model_cfg.get("max_tokens", 8000),
            },
            timeout=120,
        )
        resp.raise_for_status()
        
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        
        parsed = None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed = self._extract_json(content)
        
        return {
            "model": model_name,
            "family": model_cfg.get("family", "unknown"),
            "content": content,
            "parsed": parsed,
            "usage": data.get("usage", {}),
        }
    
    @staticmethod
    def _extract_json(text: str) -> Optional[Dict]:
        """从 markdown 代码块中提取 JSON"""
        match = re.search(r'''```(?:json)?\s*\n(.*?)\n```''', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return None
