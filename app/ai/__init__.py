"""
AI 服务模块 - 大模型 API 集成

支持多个 AI Provider，默认使用 Bailian (百炼)
"""
from .base import AIService, AIResponse
from .providers.bailian import BailianProvider

__all__ = ['AIService', 'AIResponse', 'BailianProvider']
