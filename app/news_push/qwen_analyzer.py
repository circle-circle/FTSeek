#!/usr/bin/env python3
"""
Qwen 大模型分析器

功能:
- 新闻智能摘要
- 情绪深度分析
- 相关性匹配
- 影响评估

使用阿里云百炼 API (qwen-max / qwen-plus / qwen-turbo)
"""
import os
import json
import logging
import hashlib
import time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path

# 尝试导入 requests
try:
    import requests
except ImportError:
    requests = None

from app.news_push.config import QWEN_CONFIG, CACHE_DIR

logger = logging.getLogger("ftseek.news_push.qwen_analyzer")
logger.propagate = False  # 防止日志重复


@dataclass
class AnalysisResult:
    """分析结果"""
    news_id: str
    summary: str = ""              # 智能摘要
    sentiment: str = "neutral"     # 情绪：positive/negative/neutral
    sentiment_score: float = 0.0   # 情绪分数 -1~1
    impact: str = "low"            # 影响程度：high/medium/low
    impact_reason: str = ""        # 影响原因
    key_points: List[str] = None   # 关键点列表
    related_stocks: List[str] = None  # 相关股票
    action_suggestion: str = ""    # 建议操作
    raw_response: str = ""         # 原始响应
    
    def __post_init__(self):
        if self.key_points is None:
            self.key_points = []
        if self.related_stocks is None:
            self.related_stocks = []
    
    def to_dict(self) -> Dict:
        return asdict(self)


class QwenAnalyzer:
    """
    Qwen 大模型分析器
    """
    
    # 分析提示词模板
    ANALYSIS_PROMPT = """你是一名专业的金融分析师。请分析以下新闻，并以 JSON 格式输出分析结果。

新闻标题：{title}
新闻内容：{content}
新闻来源：{source}

请分析:
1. 用 30 字以内总结核心内容
2. 判断情绪倾向 (正面/负面/中性) 并打分 (-1 到 1)
3. 评估对股市的影响程度 (高/中/低) 并说明原因
4. 提取 3-5 个关键点
5. 识别相关股票 (如有)
6. 给出投资建议 (关注/观望/回避)

输出格式 (严格 JSON):
{{
    "summary": "30 字以内的摘要",
    "sentiment": "positive/negative/neutral",
    "sentiment_score": 0.5,
    "impact": "high/medium/low",
    "impact_reason": "影响原因",
    "key_points": ["关键点 1", "关键点 2", "关键点 3"],
    "related_stocks": ["股票代码或名称"],
    "action_suggestion": "关注/观望/回避"
}}
"""
    
    def __init__(self, config: Dict = None):
        self.config = config or QWEN_CONFIG
        self.enabled = self.config.get('enabled', False)
        self.api_key = self.config.get('api_key', '')
        self.endpoint = self.config.get('endpoint', '')
        self.model = self.config.get('model', 'qwen-max')
        self.max_tokens = self.config.get('max_tokens', 500)
        self.temperature = self.config.get('temperature', 0.3)
        
        # 缓存
        self._cache: Dict[str, AnalysisResult] = {}
        self._cache_file = CACHE_DIR / "qwen_analysis_cache.json"
        self._load_cache()
        
        if self.enabled and self.api_key:
            logger.info(f"✅ Qwen 分析器已启用 | 模型：{self.model}")
        else:
            logger.info("⚠️  Qwen 分析器未启用 (缺少 API Key 或配置)")
    
    def _load_cache(self):
        """加载缓存"""
        if self._cache_file.exists():
            try:
                with open(self._cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for news_id, result in data.items():
                        self._cache[news_id] = AnalysisResult(**result)
                logger.debug(f"加载 Qwen 分析缓存：{len(self._cache)} 条")
            except Exception as e:
                logger.warning(f"加载缓存失败：{e}")
    
    def _save_cache(self):
        """保存缓存"""
        try:
            data = {k: v.to_dict() for k, v in self._cache.items()}
            with open(self._cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存缓存失败：{e}")
    
    def _get_cache_key(self, title: str, content: str) -> str:
        """生成缓存键"""
        text = f"{title}{content[:200]}"
        return hashlib.md5(text.encode('utf-8')).hexdigest()[:16]
    
    def _call_qwen_api(self, prompt: str) -> Optional[str]:
        """
        调用百炼 Qwen API (OpenAI 兼容格式)
        
        Returns:
            API 响应文本
        """
        if not requests:
            logger.error("缺少 requests 库")
            return None
        
        # 百炼 API 使用 OpenAI 兼容格式
        # endpoint 格式: https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
        url = self.endpoint
        if '/compatible-mode' not in url:
            # 自动转换为兼容模式 URL
            if url.endswith('/api/v1'):
                url = url.replace('/api/v1', '/compatible-mode/v1')
            elif not url.endswith('/chat/completions'):
                url = url.rstrip('/') + '/compatible-mode/v1/chat/completions'
        
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
        
        # OpenAI 兼容格式
        payload = {
            'model': self.model,
            'messages': [
                {
                    'role': 'system',
                    'content': '你是一名专业的金融分析师，擅长分析新闻对股市的影响。请输出严格的 JSON 格式。'
                },
                {
                    'role': 'user',
                    'content': prompt
                }
            ],
            'max_tokens': self.max_tokens,
            'temperature': self.temperature
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            
            if resp.status_code == 200:
                result = resp.json()
                # OpenAI 兼容格式响应
                content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                return content
            else:
                logger.error(f"Qwen API 请求失败：{resp.status_code} - {resp.text[:200]}")
                return None
                
        except Exception as e:
            logger.error(f"Qwen API 调用异常：{e}")
            return None
    
    def _parse_response(self, response: str, news_id: str) -> AnalysisResult:
        """解析 API 响应"""
        result = AnalysisResult(news_id=news_id, raw_response=response)
        
        # 尝试提取 JSON
        try:
            # 查找 JSON 块
            start = response.find('{')
            end = response.rfind('}') + 1
            
            if start >= 0 and end > start:
                json_str = response[start:end]
                data = json.loads(json_str)
                
                result.summary = data.get('summary', '')
                result.sentiment = data.get('sentiment', 'neutral')
                result.sentiment_score = float(data.get('sentiment_score', 0))
                result.impact = data.get('impact', 'low')
                result.impact_reason = data.get('impact_reason', '')
                result.key_points = data.get('key_points', [])
                result.related_stocks = data.get('related_stocks', [])
                result.action_suggestion = data.get('action_suggestion', '')
                
        except Exception as e:
            logger.warning(f"解析 Qwen 响应失败：{e}")
            # 降级处理：使用简单规则
            result.summary = response[:100] if response else ""
        
        return result
    
    def analyze(self, news_id: str, title: str, content: str, 
               source: str = "") -> AnalysisResult:
        """
        分析单条新闻
        
        Args:
            news_id: 新闻 ID
            title: 标题
            content: 内容
            source: 来源
            
        Returns:
            分析结果
        """
        # 检查缓存
        cache_key = self._get_cache_key(title, content)
        if cache_key in self._cache:
            logger.debug(f"Qwen 缓存命中：{news_id}")
            return self._cache[cache_key]
        
        # 如果未启用 API，返回基础分析
        if not self.enabled or not self.api_key:
            result = self._simple_analysis(news_id, title, content)
            self._cache[cache_key] = result
            self._save_cache()
            return result
        
        # 调用 Qwen API
        prompt = self.ANALYSIS_PROMPT.format(
            title=title,
            content=content[:1000],  # 限制长度
            source=source
        )
        
        logger.debug(f"调用 Qwen API 分析：{news_id}")
        response = self._call_qwen_api(prompt)
        
        if response:
            result = self._parse_response(response, news_id)
        else:
            # API 失败，降级处理
            result = self._simple_analysis(news_id, title, content)
        
        # 缓存结果
        self._cache[cache_key] = result
        self._save_cache()
        
        return result
    
    def _simple_analysis(self, news_id: str, title: str, content: str) -> AnalysisResult:
        """
        简单规则分析 (API 不可用时的降级方案)
        """
        text = f"{title} {content}".lower()
        
        # 情绪关键词
        positive = ['涨', '升', '利好', '盈利', '增长', '突破', '新高', '超预期']
        negative = ['跌', '降', '利空', '亏损', '下滑', '暴跌', '新低', '调查', '诉讼']
        
        pos_count = sum(1 for kw in positive if kw in text)
        neg_count = sum(1 for kw in negative if kw in text)
        
        # 情绪判断
        if pos_count > neg_count * 1.5:
            sentiment = 'positive'
            sentiment_score = min(1.0, pos_count / 10)
        elif neg_count > pos_count * 1.5:
            sentiment = 'negative'
            sentiment_score = -min(1.0, neg_count / 10)
        else:
            sentiment = 'neutral'
            sentiment_score = (pos_count - neg_count) / 20
        
        # 影响程度判断
        impact_keywords = ['财报', '业绩', '收购', '重组', 'IPO', '监管', '处罚', '退市']
        impact_count = sum(1 for kw in impact_keywords if kw in text)
        
        if impact_count >= 2:
            impact = 'high'
        elif impact_count == 1:
            impact = 'medium'
        else:
            impact = 'low'
        
        # 生成简单摘要
        summary = title[:50] if len(title) > 50 else title
        
        # 提取关键点
        key_points = [title]
        
        # 建议
        if sentiment == 'positive' and impact in ['high', 'medium']:
            action = '关注'
        elif sentiment == 'negative' and impact in ['high', 'medium']:
            action = '回避'
        else:
            action = '观望'
        
        return AnalysisResult(
            news_id=news_id,
            summary=summary,
            sentiment=sentiment,
            sentiment_score=sentiment_score,
            impact=impact,
            impact_reason=f"检测到{impact_count}个关键影响因素",
            key_points=key_points,
            action_suggestion=action
        )
    
    def batch_analyze(self, news_list: List[Dict], 
                     batch_size: int = 5) -> List[AnalysisResult]:
        """
        批量分析新闻
        
        Args:
            news_list: 新闻列表 [{'id': '', 'title': '', 'content': '', 'source': ''}]
            batch_size: 批次大小
            
        Returns:
            分析结果列表
        """
        results = []
        
        for i, news in enumerate(news_list):
            result = self.analyze(
                news_id=news.get('id', ''),
                title=news.get('title', ''),
                content=news.get('content', ''),
                source=news.get('source', '')
            )
            results.append(result)
            
            # 避免 API 限流
            if self.enabled and self.api_key and (i + 1) % batch_size == 0:
                time.sleep(1)
        
        logger.info(f"批量分析完成：{len(results)} 条")
        return results
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'cache_size': len(self._cache),
            'enabled': self.enabled,
            'model': self.model
        }


# 全局单例
_analyzer: Optional[QwenAnalyzer] = None


def get_qwen_analyzer(config: Dict = None) -> QwenAnalyzer:
    """获取分析器单例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = QwenAnalyzer(config)
    return _analyzer


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 测试
    analyzer = get_qwen_analyzer({
        'enabled': False,  # 测试时禁用 API
        'api_key': os.getenv('QWEN_API_KEY', '')
    })
    
    test_news = {
        'id': 'test_001',
        'title': '腾讯控股发布 Q4 财报，净利润同比增长 30% 超预期',
        'content': '腾讯控股今日发布 2025 年第四季度财报，显示净利润达到 XXX 亿元，同比增长 30%，超出市场预期。游戏业务和广告业务均表现强劲...',
        'source': 'sina'
    }
    
    result = analyzer.analyze(
        news_id=test_news['id'],
        title=test_news['title'],
        content=test_news['content'],
        source=test_news['source']
    )
    
    print("\n📊 Qwen 分析结果:")
    print(f"   摘要：{result.summary}")
    print(f"   情绪：{result.sentiment} ({result.sentiment_score:+.2f})")
    print(f"   影响：{result.impact}")
    print(f"   建议：{result.action_suggestion}")
    print(f"   关键点：{result.key_points}")
