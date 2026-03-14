"""
Bailian (百炼) AI Provider

使用阿里云百炼平台的大模型 API
"""
import os
import json
import logging
from typing import Dict, Any
from ..base import BaseProvider, AIResponse

logger = logging.getLogger("ftseek.ai.bailian")


class BailianProvider(BaseProvider):
    """百炼 AI Provider"""
    
    name = "bailian"
    
    def __init__(self, config: dict = None):
        self.api_key = os.getenv('BAILIAN_API_KEY', '')
        self.endpoint = os.getenv('BAILIAN_ENDPOINT', 'dashscope.aliyuncs.com')
        self.model = os.getenv('BAILIAN_MODEL', 'qwen-plus')
        self.timeout = int(os.getenv('BAILIAN_TIMEOUT', '30'))
        
        if not self.api_key:
            logger.warning("BAILIAN_API_KEY 未配置，AI 功能将不可用")
    
    def chat(self, prompt: str, **kwargs) -> AIResponse:
        """发送聊天请求"""
        try:
            import requests
            
            url = f"https://{self.endpoint}/api/v1/services/aigc/text-generation/generation"
            
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'model': self.model,
                'input': {
                    'messages': [
                        {'role': 'user', 'content': prompt}
                    ]
                },
                'parameters': {
                    'temperature': kwargs.get('temperature', 0.1),
                    'max_tokens': kwargs.get('max_tokens', 500)
                }
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            result = response.json()
            
            # 解析响应
            if 'output' in result and 'text' in result['output']:
                text = result['output']['text']
                tokens = result.get('usage', {}).get('output_tokens', 0)
                
                return AIResponse.success_response(
                    data={'text': text, 'raw': result},
                    tokens=tokens,
                    cost=self._calculate_cost(tokens),
                    provider=self.name
                )
            else:
                return AIResponse.error_response(f"响应格式异常：{result}", self.name)
        
        except Exception as e:
            logger.error(f"百炼 API 调用失败：{e}")
            return AIResponse.error_response(str(e), self.name)
    
    def analyze_sentiment(self, text: str) -> AIResponse:
        """情绪分析"""
        prompt = f"""
分析以下文本的情绪倾向，只返回一个 -1 到 1 之间的数字：
- 1 表示非常正面
- 0 表示中性
- -1 表示非常负面

文本：{text}

分数："""
        
        response = self.chat(prompt, max_tokens=10)
        
        if response.success:
            try:
                # 解析分数
                score_str = response.data['text'].strip()
                score = float(score_str)
                # 确保在 [-1, 1] 范围
                score = max(-1.0, min(1.0, score))
                response.data['sentiment_score'] = score
            except:
                response.data['sentiment_score'] = 0.0
        
        return response
    
    def predict_market(self, code: str, context: Dict) -> AIResponse:
        """市场预测"""
        prompt = self._build_market_prompt(code, context)
        response = self.chat(prompt, max_tokens=300)
        
        if response.success:
            # 解析预测结果
            response.data['prediction'] = self._parse_prediction(response.data['text'])
        
        return response
    
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
- MACD: {indicators.get('macd_signal', 'N/A')}
- 均线：{indicators.get('ma_trend', 'N/A')}
- 布林带：{indicators.get('bb_position', 'N/A')}

资金流:
- MFI: {flow.get('mfi', 'N/A')}
- 趋势：{flow.get('trend', 'N/A')}

情绪:
- 评分：{sentiment.get('score', 'N/A')}

请返回：
1. 方向：上涨/下跌/震荡
2. 置信度：0-100 的整数
3. 理由：50 字以内

格式示例：
方向：上涨
置信度：75
理由：技术面突破 + 资金流入"""
    
    def _parse_prediction(self, text: str) -> Dict:
        """解析预测结果"""
        lines = text.strip().split('\n')
        result = {'direction': '震荡', 'confidence': 50, 'reason': ''}
        
        for line in lines:
            if '方向' in line:
                if '上涨' in line:
                    result['direction'] = '上涨'
                elif '下跌' in line:
                    result['direction'] = '下跌'
            elif '置信度' in line:
                try:
                    conf = int(''.join(filter(str.isdigit, line)))
                    result['confidence'] = min(100, max(0, conf))
                except:
                    pass
            elif '理由' in line:
                result['reason'] = line.split('理由')[-1].strip(':')
        
        return result
    
    def _calculate_cost(self, tokens: int) -> float:
        """计算成本 (参考定价)"""
        # Qwen-plus: ¥0.01/1K tokens
        return tokens * 0.01 / 1000
