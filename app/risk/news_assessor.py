"""
新闻风险评估器 (News Risk Assessor)

P1 核心模块：将 LLM 否决权从“一刀切”升级为“分级决策”。
支持时效衰减、信源加权、事件分类。
"""
import logging
import time
from enum import Enum
from typing import Optional, List, Dict, Any

logger = logging.getLogger("ftseek.risk.news_assessor")

class NewsRiskLevel(Enum):
    OBSERVE = 0    # Level 1: 允许交易，降权 20%
    RESTRICT = 1   # Level 2: 仅允许高置信度信号，仓位减半
    HARD_VETO = 2  # Level 3: 强制拦截

class NewsRiskAssessor:
    def __init__(self, 
                 ttl_minutes: float = 60.0, 
                 hard_veto_threshold: float = -0.85):
        self.ttl_seconds = ttl_minutes * 60
        self.hard_veto_threshold = hard_veto_threshold

    def assess(self, code: str, action: 'SignalAction', 
               news_items: Optional[List[Dict[str, Any]]]) -> NewsRiskLevel:
        """
        评估新闻风险等级。
        """
        if not news_items:
            return NewsRiskLevel.OBSERVE
            
        # 1. 时效衰减 + 信源加权
        weighted_score = self._calculate_weighted_sentiment(news_items)
        
        # 2. 逆向价格验证 (News-Price Divergence)
        # TODO: 需要接入实时 Tick 数据，此处占位
        # if weighted_score < self.hard_veto_threshold and price_momentum_5min > 0.01:
        #     return NewsRiskLevel.RESTRICT # 降级为限制
        
        # 3. 分级判定
        if weighted_score < self.hard_veto_threshold:
            logger.warning(f"[LLM 硬否决] {code} 触及黑天鹅阈值 ({weighted_score:.2f})")
            return NewsRiskLevel.HARD_VETO
        elif weighted_score < -0.70:
            return NewsRiskLevel.RESTRICT
        else:
            return NewsRiskLevel.OBSERVE

    def _calculate_weighted_sentiment(self, news_items: List[Dict[str, Any]]) -> float:
        """
        计算加权情绪分：考虑时间衰减和来源权重。
        """
        if not news_items: return 0.0
        
        total_weight = 0.0
        weighted_sum = 0.0
        now = time.time()
        
        for item in news_items:
            timestamp = item.get('timestamp', now) # 假设 item 中有时间戳
            age = now - timestamp
            if age > self.ttl_seconds:
                continue # 过期新闻
                
            # 时间衰减权重：越近权重越高
            time_weight = max(0.1, 1.0 - (age / self.ttl_seconds))
            
            # 信源权重 (假设 item 中有 source)
            source = item.get('source', '').lower()
            source_weight = 1.0
            if 'bloomberg' in source or 'reuters' in source:
                source_weight = 1.2
            elif 'social' in source:
                source_weight = 0.5
            
            final_weight = time_weight * source_weight
            score = item.get('sentiment', 0.0)
            
            weighted_sum += score * final_weight
            total_weight += final_weight
            
        if total_weight == 0: return 0.0
        return weighted_sum / total_weight
