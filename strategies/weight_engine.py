#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
权重分配引擎 - 三维度动态权重分配系统核心算法

功能:
1. 新闻权重计算 (0-40%)
2. 港股美股跟随权重 (0-35%)
3. 技术指标权重 (25-100%)
4. 权重归一化 (总和=100%)
5. 约束验证

约束:
- 权重总和必须=100%
- 单笔亏损≤2%
- 至少 2 个新闻源
- 日交易≤5 笔
- 决策链路可追溯
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# 导入相关模块
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from modules.news_monitor import NewsMonitor, NewsImportance, NewsSentiment
from modules.decision_log import (
    DecisionLog, DecisionRecord, WeightAllocation,
    NewsFactor, HkUsFactor, TechnicalFactor, create_decision_record
)

logger = logging.getLogger('weight_engine')


class MarketRegime(Enum):
    """市场状态"""
    BULL = "bull"          # 牛市
    BEAR = "bear"          # 熊市
    SIDEWAYS = "sideways"  # 震荡
    VOLATILE = "volatile"  # 高波动


@dataclass
class WeightConfig:
    """权重配置"""
    news_max_weight: float = 0.40       # 新闻最大权重 40%
    news_normal_weight: float = 0.25    # 一般新闻权重 25%
    hk_us_max_weight: float = 0.35      # 港股美股最大权重 35%
    technical_min_weight: float = 0.25  # 技术最小权重 25%
    correlation_threshold: float = 0.6  # 相关性阈值
    
    # 约束
    max_single_loss: float = 0.02       # 单笔最大亏损 2%
    min_news_sources: int = 2           # 最少新闻源
    max_daily_trades: int = 5           # 日交易上限
    
    def to_dict(self) -> dict:
        return {
            'news_max_weight': self.news_max_weight,
            'news_normal_weight': self.news_normal_weight,
            'hk_us_max_weight': self.hk_us_max_weight,
            'technical_min_weight': self.technical_min_weight,
            'correlation_threshold': self.correlation_threshold,
            'max_single_loss': self.max_single_loss,
            'min_news_sources': self.min_news_sources,
            'max_daily_trades': self.max_daily_trades
        }


@dataclass
class WeightResult:
    """权重计算结果"""
    stock: str
    timestamp: datetime
    news_weight: float
    hk_us_weight: float
    technical_weight: float
    total_weight: float
    news_factor: Optional[NewsFactor]
    hk_us_factor: Optional[HkUsFactor]
    technical_factor: TechnicalFactor
    decision_id: str
    constraints_valid: bool
    constraint_errors: List[str]
    
    def to_dict(self) -> dict:
        return {
            'stock': self.stock,
            'timestamp': self.timestamp.isoformat(),
            'weights': {
                'news': self.news_weight,
                'hk_us': self.hk_us_weight,
                'technical': self.technical_weight,
                'total': self.total_weight
            },
            'news_factor': self.news_factor.to_dict() if self.news_factor else None,
            'hk_us_factor': self.hk_us_factor.to_dict() if self.hk_us_factor else None,
            'technical_factor': self.technical_factor.to_dict(),
            'decision_id': self.decision_id,
            'constraints_valid': self.constraints_valid,
            'constraint_errors': self.constraint_errors
        }


class WeightEngine:
    """
    权重分配引擎
    
    核心算法:
    1. 新闻权重 = f(重大新闻数量，情感强度，置信度)
    2. 港股美股权重 = f(相关系数，价格跟随度)
    3. 技术权重 = 100% - 新闻权重 - 港股美股权重
    4. 归一化确保总和=100%
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化权重引擎
        
        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.news_monitor = NewsMonitor()
        self.decision_log = DecisionLog()
        
        # 加载港股美股映射
        self.hk_us_mapping = self._load_hk_us_mapping()
        
        # 缓存
        self.correlation_cache: Dict[str, Tuple[float, datetime]] = {}
        self.cache_ttl = timedelta(hours=1)
        
        logger.info("WeightEngine initialized")
    
    def _load_config(self, config_path: Optional[str]) -> WeightConfig:
        """加载配置"""
        config = WeightConfig()
        
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for key, value in data.items():
                        if hasattr(config, key):
                            setattr(config, key, value)
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
        
        return config
    
    def _load_hk_us_mapping(self) -> dict:
        """加载港股美股映射表"""
        mapping_path = "/opt/ftseek/FTSeek/config/hk_us_mapping.json"
        
        if os.path.exists(mapping_path):
            with open(mapping_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        
        return {'mapping': []}
    
    def get_hk_us_pair(self, hk_stock: str) -> Optional[dict]:
        """
        获取港股美股配对信息
        
        Args:
            hk_stock: 港股代码
            
        Returns:
            Optional[dict]: 配对信息
        """
        for pair in self.hk_us_mapping.get('mapping', []):
            if pair['hk_stock'] == hk_stock and pair.get('enabled', False):
                return pair
        return None
    
    def calculate_correlation(
        self, 
        hk_stock: str, 
        us_stock: str, 
        days: int = 60
    ) -> float:
        """
        计算港股美股相关性
        
        Args:
            hk_stock: 港股代码
            us_stock: 美股代码
            days: 计算天数
            
        Returns:
            float: 相关系数 (-1 ~ 1)
        """
        # 检查缓存
        cache_key = f"{hk_stock}:{us_stock}"
        if cache_key in self.correlation_cache:
            corr, cache_time = self.correlation_cache[cache_key]
            if datetime.now() - cache_time < self.cache_ttl:
                return corr
        
        # 获取历史价格 (实际需调用 API)
        # 这里使用模拟数据
        try:
            # 实际实现需要调用 Futu OpenAPI 获取历史价格
            # hk_prices = get_historical_prices(hk_stock, days)
            # us_prices = get_historical_prices(us_stock, days)
            
            # 从映射表获取预计算的相关性
            for pair in self.hk_us_mapping.get('mapping', []):
                if pair['hk_stock'] == hk_stock and pair['us_stock'] == us_stock:
                    correlation = pair.get('correlation', 0.0)
                    
                    # 更新缓存
                    self.correlation_cache[cache_key] = (correlation, datetime.now())
                    
                    return correlation
            
            # 默认返回 0
            return 0.0
            
        except Exception as e:
            logger.warning(f"Failed to calculate correlation: {e}")
            return 0.0
    
    def calculate_news_weight(
        self, 
        news_items: List[dict],
        sources: List[str]
    ) -> Tuple[float, NewsFactor]:
        """
        计算新闻权重
        
        Args:
            news_items: 新闻列表
            sources: 新闻源列表
            
        Returns:
            Tuple[float, NewsFactor]: (权重，新闻因子详情)
        """
        if not news_items:
            return 0.0, NewsFactor(
                news_count=0,
                major_news_count=0,
                sources=[],
                sentiment='neutral',
                sentiment_score=0.0,
                confidence=0.0,
                importance='minor',
                latency_ms=0
            )
        
        # 检查新闻源数量
        if len(sources) < self.config.min_news_sources:
            logger.debug(f"Only {len(sources)} news sources (min: {self.config.min_news_sources})")
        
        # 统计重大新闻
        major_count = 0
        total_sentiment_score = 0.0
        total_confidence = 0.0
        
        for news in news_items:
            if news.get('importance') == 'major':
                major_count += 1
            total_sentiment_score += news.get('sentiment_score', 0.0)
            total_confidence += news.get('confidence', 0.0)
        
        avg_sentiment = total_sentiment_score / len(news_items) if news_items else 0.0
        avg_confidence = total_confidence / len(news_items) if news_items else 0.0
        
        # 确定重要性
        if major_count >= 2:
            importance = 'major'
            weight = self.config.news_max_weight  # 40%
        elif major_count >= 1 or len(news_items) >= 3:
            importance = 'normal'
            weight = self.config.news_normal_weight  # 25%
        else:
            importance = 'minor'
            weight = 0.0
        
        # 确定情感
        if avg_sentiment > 0.2:
            sentiment = 'positive'
        elif avg_sentiment < -0.2:
            sentiment = 'negative'
        else:
            sentiment = 'neutral'
        
        news_factor = NewsFactor(
            news_count=len(news_items),
            major_news_count=major_count,
            sources=sources,
            sentiment=sentiment,
            sentiment_score=avg_sentiment,
            confidence=avg_confidence,
            importance=importance,
            latency_ms=news_items[0].get('latency_ms', 0) if news_items else 0
        )
        
        return weight, news_factor
    
    def calculate_hk_us_weight(
        self,
        hk_stock: str,
        hk_price_change: float,
        us_price_change: float
    ) -> Tuple[float, Optional[HkUsFactor]]:
        """
        计算港股美股跟随权重
        
        Args:
            hk_stock: 港股代码
            hk_price_change: 港股价格变化
            us_price_change: 美股价格变化
            
        Returns:
            Tuple[float, Optional[HkUsFactor]]: (权重，港股美股因子详情)
        """
        # 获取配对信息
        pair = self.get_hk_us_pair(hk_stock)
        
        if not pair:
            return 0.0, None
        
        us_stock = pair['us_stock']
        correlation = self.calculate_correlation(hk_stock, us_stock)
        
        # 检查相关性阈值
        if correlation < self.config.correlation_threshold:
            logger.info(f"Correlation {correlation:.2f} < threshold {self.config.correlation_threshold}")
            return 0.0, HkUsFactor(
                hk_stock=hk_stock,
                us_stock=us_stock,
                correlation=correlation,
                enabled=False,
                hk_price_change=hk_price_change,
                us_price_change=us_price_change,
                weight_contribution=0.0
            )
        
        # 计算价格跟随度
        # 如果美股上涨，港股也上涨，则正向跟随
        same_direction = (hk_price_change * us_price_change) > 0
        
        if same_direction:
            # 权重 = 相关系数 * 最大权重 (35%)
            weight = correlation * self.config.hk_us_max_weight
        else:
            # 反向跟随，降低权重
            weight = correlation * self.config.hk_us_max_weight * 0.5
        
        hk_us_factor = HkUsFactor(
            hk_stock=hk_stock,
            us_stock=us_stock,
            correlation=correlation,
            enabled=True,
            hk_price_change=hk_price_change,
            us_price_change=us_price_change,
            weight_contribution=weight
        )
        
        return weight, hk_us_factor
    
    def calculate_technical_weight(
        self,
        news_weight: float,
        hk_us_weight: float,
        indicators: Dict[str, float] = None
    ) -> Tuple[float, TechnicalFactor]:
        """
        计算技术指标权重
        
        Args:
            news_weight: 新闻权重
            hk_us_weight: 港股美股权重
            indicators: 技术指标值
            
        Returns:
            Tuple[float, TechnicalFactor]: (权重，技术因子详情)
        """
        # 技术权重 = 100% - 新闻权重 - 港股美股权重
        technical_weight = 1.0 - news_weight - hk_us_weight
        
        # 确保在 25-100% 范围内
        technical_weight = max(self.config.technical_min_weight, 
                               min(1.0, technical_weight))
        
        # 计算信号强度 (基于技术指标)
        if indicators:
            # 简单加权平均
            signal_strength = sum(indicators.values()) / len(indicators)
            confidence = 0.8  # 默认置信度
        else:
            signal_strength = 0.5
            confidence = 0.7
        
        technical_factor = TechnicalFactor(
            indicators=indicators or {},
            signal_strength=signal_strength,
            confidence=confidence
        )
        
        return technical_weight, technical_factor
    
    def validate_constraints(
        self,
        weight_result: WeightResult,
        daily_trade_count: int
    ) -> Tuple[bool, List[str]]:
        """
        验证硬约束
        
        Args:
            weight_result: 权重结果
            daily_trade_count: 日交易数量
            
        Returns:
            Tuple[bool, List[str]]: (是否通过，错误列表)
        """
        errors = []
        
        # 1. 权重总和=100%
        if abs(weight_result.total_weight - 1.0) > 0.001:
            errors.append(f"权重总和={weight_result.total_weight:.4f}≠100%")
        
        # 2. 至少 2 个新闻源 (如果有新闻)
        if weight_result.news_factor:
            if len(weight_result.news_factor.sources) < self.config.min_news_sources:
                errors.append(f"新闻源数量={len(weight_result.news_factor.sources)}<{self.config.min_news_sources}")
        
        # 3. 日交易≤5 笔
        if daily_trade_count > self.config.max_daily_trades:
            errors.append(f"日交易={daily_trade_count}>{self.config.max_daily_trades}")
        
        # 4. 决策链路可追溯 (有 decision_id)
        if not weight_result.decision_id:
            errors.append("决策链路不可追溯 (无 decision_id)")
        
        return len(errors) == 0, errors
    
    async def calculate_weights(
        self,
        stock: str,
        action: str,
        hk_price_change: float = 0.0,
        us_price_change: float = 0.0,
        technical_indicators: Dict[str, float] = None,
        position_size: float = 0.05,
        stop_loss: float = 0.0,
        take_profit: float = 0.0
    ) -> WeightResult:
        """
        计算权重分配
        
        Args:
            stock: 股票代码
            action: 买入/卖出
            hk_price_change: 港股价格变化
            us_price_change: 美股价格变化
            technical_indicators: 技术指标
            position_size: 仓位比例
            stop_loss: 止损价
            take_profit: 止盈价
            
        Returns:
            WeightResult: 权重结果
        """
        timestamp = datetime.now()
        
        # 1. 获取新闻并计算新闻权重
        news_aggregation = await self.news_monitor.get_aggregation(stock)
        news_items = [n.to_dict() for n in news_aggregation.news_items]
        sources = list(set(n.source for n in news_aggregation.news_items))
        
        news_weight, news_factor = self.calculate_news_weight(news_items, sources)
        
        # 2. 计算港股美股跟随权重
        hk_us_weight, hk_us_factor = self.calculate_hk_us_weight(
            stock, hk_price_change, us_price_change
        )
        
        # 3. 计算技术指标权重
        technical_weight, technical_factor = self.calculate_technical_weight(
            news_weight, hk_us_weight, technical_indicators
        )
        
        # 4. 计算总权重
        total_weight = news_weight + hk_us_weight + technical_weight
        
        # 5. 创建决策记录
        decision_record = create_decision_record(
            stock=stock,
            action=action,
            news_weight=news_weight,
            hk_us_weight=hk_us_weight,
            technical_weight=technical_weight,
            news_factor=news_factor if news_factor.news_count > 0 else None,
            hk_us_factor=hk_us_factor,
            technical_factor=technical_factor,
            position_size=position_size,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        
        # 6. 记录决策
        decision_id = self.decision_log.log_decision(decision_record)
        
        # 7. 获取日交易数量
        daily_count = self.decision_log.get_daily_count()
        
        # 8. 创建权重结果
        weight_result = WeightResult(
            stock=stock,
            timestamp=timestamp,
            news_weight=news_weight,
            hk_us_weight=hk_us_weight,
            technical_weight=technical_weight,
            total_weight=total_weight,
            news_factor=news_factor if news_factor.news_count > 0 else None,
            hk_us_factor=hk_us_factor,
            technical_factor=technical_factor,
            decision_id=decision_id,
            constraints_valid=True,
            constraint_errors=[]
        )
        
        # 9. 验证约束
        valid, errors = self.validate_constraints(weight_result, daily_count)
        weight_result.constraints_valid = valid
        weight_result.constraint_errors = errors
        
        if not valid:
            logger.warning(f"Constraint validation failed: {errors}")
        
        logger.info(
            f"Weights calculated for {stock}: "
            f"news={news_weight:.2%}, hk_us={hk_us_weight:.2%}, "
            f"technical={technical_weight:.2%}, total={total_weight:.2%}"
        )
        
        return weight_result
    
    def get_trace(self, decision_id: str) -> dict:
        """
        获取决策追溯链路
        
        Args:
            decision_id: 决策 ID
            
        Returns:
            dict: 追溯链路
        """
        return self.decision_log.get_trace(decision_id)
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            'config': self.config.to_dict(),
            'news_monitor': self.news_monitor.get_stats(),
            'hk_us_mapping': {
                'total_pairs': len(self.hk_us_mapping.get('mapping', [])),
                'enabled_pairs': sum(
                    1 for p in self.hk_us_mapping.get('mapping', []) 
                    if p.get('enabled', False)
                )
            }
        }
    
    async def close(self):
        """关闭资源"""
        await self.news_monitor.close()


# 便捷函数
async def get_weights(
    stock: str,
    action: str = "BUY",
    hk_price_change: float = 0.0,
    us_price_change: float = 0.0
) -> WeightResult:
    """
    获取权重分配
    
    Args:
        stock: 股票代码
        action: 买入/卖出
        hk_price_change: 港股价格变化
        us_price_change: 美股价格变化
        
    Returns:
        WeightResult: 权重结果
    """
    engine = WeightEngine()
    try:
        return await engine.calculate_weights(
            stock=stock,
            action=action,
            hk_price_change=hk_price_change,
            us_price_change=us_price_change
        )
    finally:
        await engine.close()


# 测试代码
if __name__ == "__main__":
    async def test():
        engine = WeightEngine()
        
        # 测试权重计算
        stock = "00700.HK"
        result = await engine.calculate_weights(
            stock=stock,
            action="BUY",
            hk_price_change=0.02,
            us_price_change=0.015,
            technical_indicators={'rsi': 55, 'macd': 0.02, 'bb': 0.6}
        )
        
        print(f"股票：{result.stock}")
        print(f"决策 ID: {result.decision_id}")
        print(f"\n权重分配:")
        print(f"  新闻权重：{result.news_weight:.2%}")
        print(f"  港股美股权重：{result.hk_us_weight:.2%}")
        print(f"  技术权重：{result.technical_weight:.2%}")
        print(f"  总权重：{result.total_weight:.2%}")
        print(f"\n约束验证：{'通过' if result.constraints_valid else '失败'}")
        if result.constraint_errors:
            print(f"  错误：{result.constraint_errors}")
        
        # 获取追溯链路
        trace = engine.get_trace(result.decision_id)
        print(f"\n追溯链路:")
        print(json.dumps(trace, ensure_ascii=False, indent=2))
        
        # 获取统计
        stats = engine.get_stats()
        print(f"\n统计信息:")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        
        await engine.close()
    
    asyncio.run(test())
