#!/usr/bin/env python3
"""新闻时效性衰减与过期过滤模块 (Phase 1 Implementation)

核心职责:
1. 根据事件类型 (category) 计算 TTL (Time To Live)
2. 过滤掉已过期的新闻
3. 为每条新闻计算 "时效权重" (freshness_weight) 供下游使用

策略:
- 黑天鹅/突发 (breaking): 30 分钟 TTL
- 财报/监管/诉讼: 4 小时 TTL
- 宏观/行业: 2 小时 TTL
- 其他/普通快讯: 1 小时 TTL
"""

import logging
from dataclasses import dataclass
from typing import List, Any
from datetime import datetime

logger = logging.getLogger("ftseek.pipeline.news_ttl")

# TTL 映射 (分钟)
TTL_MAP = {
    "earnings": 240,     # 财报
    "regulation": 240,   # 监管/诉讼
    "macro": 120,        # 宏观
    "geopolitical": 120, # 地缘
    "analyst": 120,      # 分析师评级
    "breaking": 30,      # 突发/黑天鹅
    "default": 60        # 默认
}

@dataclass
class NewsTTLFilter:
    """新闻 TTL 过滤器"""
    
    def filter_and_decay(self, news: List[Any]) -> List[Any]:
        """执行过期过滤与权重计算"""
        if not news:
            return []
            
        kept = []
        dropped = 0
        
        for item in news:
            # 1. 确定类别
            category = getattr(item, "category", "").lower() or "default"
            ttl = TTL_MAP.get(category, TTL_MAP["default"])
            
            # 2. 检查时效
            age = getattr(item, "age_minutes", 0)
            
            if age > ttl:
                dropped += 1
                logger.debug(f"[TTL] 丢弃过期新闻 (age={age}m, ttl={ttl}m): {getattr(item, 'title', '')[:30]}")
                continue
                
            # 3. 计算时效权重 (线性衰减)
            # age=0 -> weight=1.0, age=ttl -> weight=0.5
            weight = 1.0 - (age / ttl) * 0.5
            setattr(item, "freshness_weight", max(0.5, min(1.0, weight)))
            
            kept.append(item)
            
        logger.info(f"[TTL] 过滤完成: 输入 {len(news)} -> 保留 {len(kept)} (丢弃 {dropped} 条过期)")
        return kept
