"""
FTSeek 策略包 v3.0

统一策略接口:
- BaseStrategy: 策略基类
- Signal: 信号数据类
- SignalAction: 信号动作枚举

内置策略:
- FusionV2Strategy: 融合策略 (技术 + 情绪)
- MACrossStrategy: 均线交叉策略
- RSIReboundStrategy: RSI 反弹策略
"""
import os
import yaml
from typing import Dict, Optional, Type
from pathlib import Path

from app.strategy.base import BaseStrategy, Signal, SignalAction

# 导入策略实现
from .fusion_v2 import FusionV2Strategy
from .ma_cross import MACrossStrategy
from .rsi_rebound import RSIReboundStrategy

# 策略注册表
STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    'fusion_v2': FusionV2Strategy,
    'ma_cross': MACrossStrategy,
    'rsi_rebound': RSIReboundStrategy,
}


def load_config(config_path: str = None) -> Dict:
    """
    加载策略配置文件
    
    Args:
        config_path: 配置文件路径，默认使用内置配置
        
    Returns:
        配置字典
    """
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    
    if not os.path.exists(config_path):
        return {}
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def create_strategy(name: str, config: Dict = None) -> Optional[BaseStrategy]:
    """
    创建策略实例
    
    Args:
        name: 策略名称 (fusion_v2/ma_cross/rsi_rebound)
        config: 策略配置
        
    Returns:
        策略实例，如果策略不存在则返回 None
    """
    strategy_class = STRATEGY_REGISTRY.get(name)
    if strategy_class is None:
        return None
    
    return strategy_class(config)


def get_all_strategies(config: Dict = None) -> Dict[str, BaseStrategy]:
    """
    获取所有启用的策略实例
    
    Args:
        config: 策略配置
        
    Returns:
        策略实例字典 {name: strategy}
    """
    if config is None:
        config = load_config()
    
    strategies = {}
    strategies_config = config.get('strategies', {})
    
    for name, strat_config in strategies_config.items():
        if strat_config.get('enabled', True):
            strategy = create_strategy(name, strat_config.get('parameters', {}))
            if strategy:
                strategies[name] = strategy
    
    return strategies


def get_strategy_weights(config: Dict = None) -> Dict[str, float]:
    """
    获取策略权重配置
    
    Args:
        config: 策略配置
        
    Returns:
        权重的字典 {name: weight}
    """
    if config is None:
        config = load_config()
    
    weights = {}
    strategies_config = config.get('strategies', {})
    
    for name, strat_config in strategies_config.items():
        weights[name] = strat_config.get('weight', 1.0)
    
    return weights


__all__ = [
    'BaseStrategy',
    'Signal',
    'SignalAction',
    'FusionV2Strategy',
    'MACrossStrategy',
    'RSIReboundStrategy',
    'STRATEGY_REGISTRY',
    'load_config',
    'create_strategy',
    'get_all_strategies',
    'get_strategy_weights',
]
