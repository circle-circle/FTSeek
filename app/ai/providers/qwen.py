#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen AI Provider

使用阿里云 DashScope API (通义千问)
支持 Qwen-Max, Qwen-Plus, Qwen-Turbo 等模型
"""

import os
import json
import logging
from typing import Dict, Any
from pathlib import Path
from ..base import BaseProvider, AIResponse

logger = logging.getLogger("ftseek.ai.qwen")
logger.propagate = False

# 加载 .env 文件
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent.parent.parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass


class QwenProvider(BaseProvider):
    """通义千问 AI Provider"""

    name = "qwen"

    def __init__(self, config: dict = None):
        """
        初始化 Qwen Provider

        Args:
            config: 配置字典
        """
        self.api_key = os.getenv('DASHSCOPE_API_KEY') or os.getenv('BAILIAN_API_KEY', '')
        self.model = os.getenv('DASHSCOPE_MODEL') or os.getenv('BAILIAN_MODEL', 'qwen-plus')
        self.timeout = int(os.getenv('DASHSCOPE_TIMEOUT') or os.getenv('BAILIAN_TIMEOUT', '60'))

        # DashScope / Bailian 端点
        self.endpoint = os.getenv('BAILIAN_ENDPOINT', "https://dashscope.aliyuncs.com/api/v1")

        if not self.api_key:
            logger.warning("DASHSCOPE_API_KEY 未配置，AI 功能将不可用")
        else:
            logger.info(f"Qwen Provider 已初始化：model={self.model}")

    def chat(self, prompt: str, **kwargs) -> AIResponse:
        """发送聊天请求"""
        try:
            import requests

            url = f"{self.endpoint}/services/aigc/text-generation/generation"

            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }

            # Qwen 请求格式
            payload = {
                'model': self.model,
                'input': {
                    'messages': [
                        {'role': 'user', 'content': prompt}
                    ]
                },
                'parameters': {
                    'temperature': kwargs.get('temperature', 0.1),
                    'max_tokens': kwargs.get('max_tokens', 500),
                    'result_format': 'message'
                }
            }

            response = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            result = response.json()

            # Qwen 响应格式
            if 'output' in result and 'choices' in result['output']:
                choices = result['output']['choices']
                if choices and 'message' in choices[0]:
                    text = choices[0]['message']['content']

                    # 计算 token
                    usage = result.get('usage', {})
                    tokens = usage.get('output_tokens', 0)

                    return AIResponse.success_response(
                        data={'text': text, 'raw': result},
                        tokens=tokens,
                        cost=self._calculate_cost(tokens),
                        provider=self.name
                    )

            return AIResponse.error_response(f"响应格式异常：{result}", self.name)

        except Exception as e:
            logger.error(f"Qwen API 调用失败：{e}")
            return AIResponse.error_response(str(e), self.name)

    def analyze_sentiment(self, text: str) -> AIResponse:
        """情绪分析"""
        prompt = f"""
分析以下文本的情绪倾向，只返回一个 -1 到 1 之间的数字：
- 1 表示非常正面
- 0 表示中性
- -1 表示非常负面

文本：{text[:500]}

分数："""

        response = self.chat(prompt, max_tokens=10)

        if response.success:
            try:
                score_str = response.data['text'].strip()
                # 提取数字
                import re
                match = re.search(r'[-+]?\d*\.?\d+', score_str)
                if match:
                    score = float(match.group())
                    score = max(-1.0, min(1.0, score))
                else:
                    score = 0.0
                response.data['sentiment_score'] = score
            except (ValueError, KeyError, TypeError):
                response.data['sentiment_score'] = 0.0

        return response

    def analyze_news_entities(self, title: str, content: str) -> AIResponse:
        """
        新闻实体识别

        Args:
            title: 新闻标题
            content: 新闻内容

        Returns:
            AIResponse: 实体识别结果
        """
        from .prompts_news import NEWS_ENTITY_RECOGNITION

        prompt = NEWS_ENTITY_RECOGNITION.format(
            title=title,
            content=content[:1000] if content else ''
        )

        response = self.chat(prompt, max_tokens=1000)

        if response.success:
            try:
                text = response.data['text'].strip()
                # 提取 JSON
                if '```json' in text:
                    text = text.split('```json')[1].split('```')[0].strip()
                elif '```' in text:
                    text = text.split('```')[1].split('```')[0].strip()

                result = json.loads(text)
                response.data['entities'] = result.get('entities', [])
            except Exception as e:
                logger.warning(f"解析实体识别结果失败：{e}")
                response.data['entities'] = []

        return response

    def analyze_impact_chain(self, title: str, content: str) -> AIResponse:
        """
        影响链分析

        Args:
            title: 新闻标题
            content: 新闻内容

        Returns:
            AIResponse: 影响链分析结果
        """
        from .prompts_news import IMPACT_CHAIN_ANALYSIS

        prompt = IMPACT_CHAIN_ANALYSIS.format(
            title=title,
            content=content[:1000] if content else ''
        )

        response = self.chat(prompt, max_tokens=1000)

        if response.success:
            try:
                text = response.data['text'].strip()
                if '```json' in text:
                    text = text.split('```json')[1].split('```')[0].strip()
                elif '```' in text:
                    text = text.split('```')[1].split('```')[0].strip()

                result = json.loads(text)
                response.data.update(result)
            except Exception as e:
                logger.warning(f"解析影响链结果失败：{e}")

        return response

    def classify_event(self, title: str, content: str) -> AIResponse:
        """
        事件类型分类

        Args:
            title: 新闻标题
            content: 新闻内容

        Returns:
            AIResponse: 事件分类结果
        """
        from .prompts_news import EVENT_CLASSIFICATION

        prompt = EVENT_CLASSIFICATION.format(
            title=title,
            content=content[:1000] if content else ''
        )

        response = self.chat(prompt, max_tokens=500)

        if response.success:
            try:
                text = response.data['text'].strip()
                if '```json' in text:
                    text = text.split('```json')[1].split('```')[0].strip()
                elif '```' in text:
                    text = text.split('```')[1].split('```')[0].strip()

                result = json.loads(text)
                response.data['event_type'] = result.get('event_type', 'other')
                response.data['event_confidence'] = result.get('confidence', 0.0)
            except Exception as e:
                logger.warning(f"解析事件分类结果失败：{e}")

        return response

    def predict_market(self, code: str, context: Dict) -> AIResponse:
        """市场预测"""
        prompt = self._build_market_prompt(code, context)
        response = self.chat(prompt, max_tokens=300)

        if response.success:
            response.data['prediction'] = self._parse_prediction(response.data['text'])

        return response

    def _calculate_cost(self, tokens: int) -> float:
        """
        计算 API 调用成本

        Qwen 定价 (approximate):
        - qwen-max: 0.08 元/1K tokens
        - qwen-plus: 0.02 元/1K tokens
        - qwen-turbo: 0.008 元/1K tokens
        """
        price_per_1k = {
            'qwen-max': 0.08,
            'qwen-plus': 0.02,
            'qwen-turbo': 0.008,
        }
        price = price_per_1k.get(self.model, 0.02)
        return (tokens / 1000) * price

    def _build_market_prompt(self, code: str, context: Dict) -> str:
        """构建市场预测提示词"""
        indicators = context.get('indicators', {})
        flow = context.get('capital_flow', {})
        sentiment = context.get('sentiment', {})

        return f"""
基于以下信息预测股票短期走势 (1-5 个交易日)：

股票代码：{code}

技术指标:
- RSI: {indicators.get('rsi', 'N/A')}
- MACD: {indicators.get('macd', 'N/A')}
- 布林带：{indicators.get('bollinger', 'N/A')}

资金流向:
- 净流入：{flow.get('net_inflow', 'N/A')}
- 大单比：{flow.get('large_order_ratio', 'N/A')}

市场情绪:
- 情绪评分：{sentiment.get('score', 'N/A')}

请给出预测和理由："""

    def _parse_prediction(self, text: str) -> Dict:
        """解析预测结果"""
        # 简化解析逻辑
        return {
            'direction': 'unknown',
            'confidence': 0.5,
            'reasoning': text
        }


def get_qwen_provider() -> QwenProvider:
    """获取 Qwen Provider 单例"""
    return QwenProvider()
