"""
AI 服务基类和通用接口
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger("ftseek.ai")
logger.propagate = False  # 防止日志重复


@dataclass
class AIResponse:
    """AI API 响应"""
    success: bool
    data: Any
    error: Optional[str] = None
    tokens_used: int = 0
    cost: float = 0.0
    provider: str = ""
    
    @classmethod
    def success_response(cls, data: Any, tokens: int = 0, cost: float = 0.0, provider: str = ""):
        return cls(success=True, data=data, tokens_used=tokens, cost=cost, provider=provider)
    
    @classmethod
    def error_response(cls, error: str, provider: str = ""):
        return cls(success=False, data=None, error=error, provider=provider)


class BaseProvider(ABC):
    """AI Provider 基类"""
    
    name: str = "base"
    
    @abstractmethod
    def chat(self, prompt: str, **kwargs) -> AIResponse:
        """发送聊天请求"""
        pass
    
    @abstractmethod
    def analyze_sentiment(self, text: str) -> AIResponse:
        """情绪分析"""
        pass
    
    @abstractmethod
    def predict_market(self, code: str, context: Dict) -> AIResponse:
        """市场预测"""
        pass


class AIService:
    """AI 服务统一接口"""
    
    def __init__(self, provider: str = 'bailian'):
        self.provider = self._load_provider(provider)
        logger.info(f"AI 服务已初始化：{provider}")
    
    def _load_provider(self, name: str) -> BaseProvider:
        """加载 Provider"""
        if name == 'bailian':
            from .providers.bailian import BailianProvider
            return BailianProvider()
        elif name == 'qwen':
            try:
                from .providers.qwen import QwenProvider
                return QwenProvider()
            except ImportError:
                logger.warning(f"Provider '{name}' 未实现，回退到 bailian")
                from .providers.bailian import BailianProvider
                return BailianProvider()
        else:
            from .providers.bailian import BailianProvider
            return BailianProvider()
    
    def chat(self, prompt: str, **kwargs) -> AIResponse:
        return self.provider.chat(prompt, **kwargs)
    
    def analyze_sentiment(self, text: str) -> AIResponse:
        return self.provider.analyze_sentiment(text)
    
    def predict_market(self, code: str, context: Dict) -> AIResponse:
        return self.provider.predict_market(code, context)
