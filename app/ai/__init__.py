"""
AI 服务模块 - 大模型 API 集成

支持多个 AI Provider，默认使用 Bailian (百炼)
"""
from .base import AIService, AIResponse
from .providers.bailian import BailianProvider
from .llm_market_analyst import (
    LLMMarketAnalyst,
    MarketAssessment,
    NewsContext,
    CrossMarketContext,
    TechnicalSnapshot,
    NewsImpact,
)
from .prompt_templates import PromptBuilder
from .llm_output_parser import LLMOutputParser

__all__ = [
    'AIService', 'AIResponse', 'BailianProvider',
    'LLMMarketAnalyst', 'MarketAssessment', 'NewsContext',
    'CrossMarketContext', 'TechnicalSnapshot', 'NewsImpact',
    'PromptBuilder', 'LLMOutputParser',
]
