"""
Playbook Screener - 选股筛选器

从 watchlist 中筛选 Top N 候选股票，基于可配置的因子权重和市场环境。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
import yaml

from app.playbook.market_regime import MarketRegime
from app.playbook.data_collector import PlaybookDataCollector, PlaybookData

logger = logging.getLogger(__name__)


@dataclass
class ScreenResult:
    """筛选结果"""
    symbol: str
    score: int
    factors: Dict[str, Any]  # 触发的因子详情
    data: PlaybookData  # 完整的 6 维度数据
    
    def __repr__(self):
        return f"ScreenResult({self.symbol}, score={self.score})"


class PlaybookScreener:
    """选股筛选器"""
    
    def __init__(
        self,
        data_collector: PlaybookDataCollector,
        config_path: str = "config/playbook_screening.yaml"
    ):
        self.data_collector = data_collector
        self.config_path = Path(config_path)
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """加载筛选配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load screening config: {e}")
            return {"screening_factors": {}, "weight_adjustments": []}
    
    def screen(
        self,
        watchlist: List[str],
        regime: MarketRegime,
        positions: List[Dict[str, Any]],
        top_n: Optional[int] = None
    ) -> List[ScreenResult]:
        """
        筛选 Top N 候选股票
        
        Args:
            watchlist: 候选股票列表
            regime: 当前市场环境
            positions: 当前持仓列表 (每个 dict 需含 'symbol' 和 'industry' 键)
            top_n: 返回 Top N，默认根据 regime 决定
            
        Returns:
            按分数降序排列的 ScreenResult 列表
        """
        # 0. 根据 regime 确定 top_n
        if top_n is None:
            top_n = self._get_top_n_by_regime(regime)
        
        if top_n == 0:
            logger.info(f"[SCREENER] Regime {regime.name} → top_n=0, skip screening")
            return []
        
        logger.info(f"[SCREENER] Screening {len(watchlist)} symbols, regime={regime.name}, top_n={top_n}")
        
        candidates = []
        held_symbols = {p.get('symbol') for p in positions}
        
        for symbol in watchlist:
            # 1. 排除已有持仓
            if symbol in held_symbols:
                logger.debug(f"[SCREENER] {symbol} excluded: already held")
                continue
            
            # 2. 收集 6 维度数据
            try:
                data = self.data_collector.collect(symbol)
            except Exception as e:
                logger.warning(f"[SCREENER] {symbol} data collection failed: {e}")
                continue
            
            # 3. 新鲜度硬过滤 (核心反幻觉逻辑)
            quality_summary = self._get_quality_summary(data)
            ok_count = quality_summary["ok_count"]
            
            # 全部维度 missing → 跳过该标的
            if ok_count == 0:
                logger.debug(f"[SCREENER] {symbol} excluded: all dimensions missing")
                continue
            
            # 财报 < 3 天 → 排除
            days_until = data.get_days_until_earnings()
            if days_until is not None and days_until < 3:
                logger.debug(f"[SCREENER] {symbol} excluded: earnings in {days_until} days")
                continue
            
            # 4. 计算分数 (只使用已验证的条件)
            score, triggered_factors = self._calculate_score(data, positions)
            
            # 5. 行业集中度惩罚
            sector = data.get_sector()
            if sector:
                same_sector_count = sum(
                    1 for p in positions 
                    if self._get_position_sector(p) == sector
                )
                if same_sector_count >= 2:
                    score -= 3
                    triggered_factors['sector_concentration'] = {
                        'sector': sector,
                        'count': same_sector_count,
                        'penalty': -3
                    }
            
            candidates.append(ScreenResult(
                symbol=symbol,
                score=score,
                factors=triggered_factors,
                data=data
            ))
        
        # 6. 按分数降序排序
        candidates.sort(key=lambda x: x.score, reverse=True)
        
        # 7. 返回 Top N
        result = candidates[:top_n]
        logger.info(f"[SCREENER] Selected {len(result)} candidates: {[c.symbol for c in result]}")
        
        return result
    
    def _get_quality_summary(self, data: PlaybookData) -> Dict[str, Any]:
        """获取数据质量摘要"""
        qualities = [
            data.fundamental.quality,
            data.technical.quality,
            data.insider.quality,
            data.news.quality,
            data.earnings.quality,
            data.sentiment.quality,
        ]
        ok_count = sum(1 for q in qualities if q == "ok")
        return {
            "ok_count": ok_count,
            "qualities": qualities,
        }
    
    def _get_top_n_by_regime(self, regime: MarketRegime) -> int:
        """根据市场环境确定 top_n"""
        mapping = {
            MarketRegime.RISK_ON: 10,
            MarketRegime.CAUTIOUS: 5,
            MarketRegime.RISK_OFF: 3,
            MarketRegime.NO_TRADE: 0,
        }
        return mapping.get(regime, 5)
    
    def _should_exclude_earnings(self, symbol: str) -> bool:
        """检查是否应该因为财报日期排除"""
        # TODO: 从 earnings_calendar 获取财报日期
        # 如果距离财报 < 3 天，返回 True
        return False
    
    def _get_position_sector(self, position: Dict[str, Any]) -> Optional[str]:
        """获取持仓的行业分类"""
        # 兼容 'sector' 和 'industry' 两种字段名
        return position.get('sector') or position.get('industry')
    
    def _calculate_score(
        self,
        data: PlaybookData,
        positions: List[Dict[str, Any]]
    ) -> tuple[int, Dict[str, Any]]:
        """
        计算筛选分数
        
        Returns:
            (总分, 触发的因子详情)
        """
        score = 0
        triggered_factors = {}
        
        factors_config = self.config.get('screening_factors', {})
        
        # 先评估 insider 相关因子（互斥逻辑）
        cluster_score = data.get_insider_cluster_score()
        net_buy = data.get_insider_net_buy()
        
        insider_triggered = False
        if cluster_score > 0.5 and 'insider_cluster_buy' in factors_config:
            weight = factors_config['insider_cluster_buy'].get('weight', 0)
            score += weight
            triggered_factors['insider_cluster_buy'] = {
                'weight': weight,
                'description': factors_config['insider_cluster_buy'].get('description', ''),
                'value': cluster_score
            }
            insider_triggered = True
        
        if not insider_triggered and net_buy > 0 and 'insider_net_buy' in factors_config:
            weight = factors_config['insider_net_buy'].get('weight', 0)
            score += weight
            triggered_factors['insider_net_buy'] = {
                'weight': weight,
                'description': factors_config['insider_net_buy'].get('description', ''),
                'value': net_buy
            }
        
        if net_buy < 0 and 'insider_net_sell' in factors_config:
            weight = factors_config['insider_net_sell'].get('weight', 0)
            score += weight
            triggered_factors['insider_net_sell'] = {
                'weight': weight,
                'description': factors_config['insider_net_sell'].get('description', ''),
                'value': net_buy
            }
        
        # 评估其他因子（跳过已处理的 insider 因子）
        for factor_name, factor_cfg in factors_config.items():
            if factor_name in ('insider_cluster_buy', 'insider_net_buy', 'insider_net_sell'):
                continue  # 已处理
            
            weight = factor_cfg.get('weight', 0)
            condition = factor_cfg.get('condition')
            
            # 评估因子是否触发
            triggered = self._evaluate_factor(factor_name, data, condition)
            
            if triggered:
                score += weight
                triggered_factors[factor_name] = {
                    'weight': weight,
                    'description': factor_cfg.get('description', ''),
                    'value': triggered  # 实际值
                }
        
        # 应用权重调整历史
        adjustments = self.config.get('weight_adjustments', [])
        for adj in adjustments:
            factor = adj.get('factor')
            delta = adj.get('delta', 0)
            if factor in triggered_factors:
                score += delta
                triggered_factors[factor]['adjustment'] = delta
        
        return score, triggered_factors
    
    def _evaluate_factor(
        self,
        factor_name: str,
        data: PlaybookData,
        condition: Optional[str]
    ) -> Any:
        """
        评估单个因子是否触发
        
        Returns:
            触发时返回实际值，未触发返回 None
        """
        # 特殊因子处理
        if factor_name == 'insider_cluster_buy':
            cluster_score = data.get_insider_cluster_score()
            return cluster_score if cluster_score > 0.5 else None
        
        elif factor_name == 'insider_net_buy':
            net_buy = data.get_insider_net_buy()
            return net_buy if net_buy > 0 else None
        
        elif factor_name == 'insider_net_sell':
            net_buy = data.get_insider_net_buy()
            return net_buy if net_buy < 0 else None
        
        elif factor_name == 'macd_golden_cross':
            macd_signal = data.get_macd_signal()
            return macd_signal if macd_signal == 'golden_cross' else None
        
        elif factor_name == 'pe_below_industry':
            pe_ratio = data.get_pe_below_industry()
            return pe_ratio if pe_ratio else None
        
        # 通用条件评估
        elif condition:
            return self._evaluate_condition(condition, data)
        
        return None
    
    def _evaluate_condition(self, condition: str, data: PlaybookData) -> Any:
        """
        评估通用条件表达式
        
        Examples:
            "rsi_14 < 40"       (字段 vs 数值)
            "price > ma20"      (字段 vs 字段)
            "sentiment_score > 0"
        """
        try:
            parts = condition.split()
            if len(parts) != 3:
                return None
            
            field, op, threshold_str = parts
            
            # 获取左侧字段值
            left_value = self._get_field_value(field, data)
            if left_value is None:
                return None
            
            # 右侧可能是数值或字段名
            try:
                right_value = float(threshold_str)
            except ValueError:
                # 不是数字，尝试作为字段名
                right_value = self._get_field_value(threshold_str, data)
                if right_value is None:
                    return None
            
            # 评估条件
            if op == '<' and left_value < right_value:
                return left_value
            elif op == '>' and left_value > right_value:
                return left_value
            elif op == '<=' and left_value <= right_value:
                return left_value
            elif op == '>=' and left_value >= right_value:
                return left_value
            elif op == '==' and left_value == right_value:
                return left_value
            
        except Exception as e:
            logger.debug(f"Condition evaluation failed: {condition}, error: {e}")
        
        return None
    
    def _get_field_value(self, field: str, data: PlaybookData) -> Optional[float]:
        """从 PlaybookData 获取字段值"""
        field_map = {
            'rsi_14': data.get_rsi,
            'price': data.get_price,
            'ma20': data.get_ma20,
            'sentiment_score': data.get_sentiment_score,
            'beat_rate': data.get_earnings_beat_rate,
            'volume_5d_avg_ratio': data.get_volume_ratio,
            'days_until_earnings': data.get_days_until_earnings,
        }
        
        getter = field_map.get(field)
        if getter:
            try:
                return getter()
            except Exception:
                return None
        
        return None
