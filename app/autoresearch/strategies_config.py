"""
AutoResearch 策略配置

定义 3 个优化策略的参数空间和目标。
"""

from .search_engine import ParameterSpace

# 策略参数空间配置
STRATEGY_PARAM_SPACES = {
    # confluence_v2 (主力策略 - 多因子融合)
    'confluence_v2': {
        'min_factors': ParameterSpace(
            name='min_factors',
            param_type='int',
            min_value=2,
            max_value=4,
        ),
        'rsi_period': ParameterSpace(
            name='rsi_period',
            param_type='int',
            min_value=10,
            max_value=20,
        ),
        'rsi_overbought': ParameterSpace(
            name='rsi_overbought',
            param_type='int',
            min_value=65,
            max_value=80,
        ),
        'rsi_oversold': ParameterSpace(
            name='rsi_oversold',
            param_type='int',
            min_value=20,
            max_value=35,
        ),
        'macd_fast': ParameterSpace(
            name='macd_fast',
            param_type='int',
            min_value=8,
            max_value=16,
        ),
        'macd_slow': ParameterSpace(
            name='macd_slow',
            param_type='int',
            min_value=20,
            max_value=32,
        ),
        'bb_period': ParameterSpace(
            name='bb_period',
            param_type='int',
            min_value=15,
            max_value=25,
        ),
        'bb_std': ParameterSpace(
            name='bb_std',
            param_type='float',
            min_value=1.5,
            max_value=2.5,
            step=0.1,
        ),
        'st_multiplier': ParameterSpace(
            name='st_multiplier',
            param_type='float',
            min_value=2.5,
            max_value=3.5,
            step=0.1,
        ),
    },
    
    # supertrend (辅助策略 - 趋势跟踪)
    'supertrend': {
        'atr_period': ParameterSpace(
            name='atr_period',
            param_type='int',
            min_value=8,
            max_value=14,
        ),
        'base_multiplier': ParameterSpace(
            name='base_multiplier',
            param_type='float',
            min_value=2.0,
            max_value=3.5,
            step=0.1,
        ),
        'range_adjust_factor': ParameterSpace(
            name='range_adjust_factor',
            param_type='float',
            min_value=1.0,
            max_value=1.5,
            step=0.1,
        ),
    },
    
    # sentiment_ai (辅助策略 - AI 情绪)
    'sentiment_ai': {
        'lookback_hours': ParameterSpace(
            name='lookback_hours',
            param_type='int',
            min_value=12,
            max_value=48,
        ),
        'max_news': ParameterSpace(
            name='max_news',
            param_type='int',
            min_value=5,
            max_value=20,
        ),
        'sentiment_threshold': ParameterSpace(
            name='sentiment_threshold',
            param_type='float',
            min_value=0.2,
            max_value=0.5,
            step=0.05,
        ),
    },
}

# 优化目标配置
OPTIMIZATION_TARGETS = {
    'confluence_v2': {
        'target_win_rate': 0.65,       # 目标胜率 65%
        'target_sharpe': 1.8,          # 目标夏普比率 1.8
        'max_drawdown_limit': 0.10,    # 最大回撤 10%
        'min_trades': 50,              # 最小交易次数
    },
    'supertrend': {
        'target_win_rate': 0.60,       # 目标胜率 60%
        'target_sharpe': 1.5,          # 目标夏普比率 1.5
        'max_drawdown_limit': 0.12,    # 最大回撤 12%
        'min_trades': 30,              # 最小交易次数
    },
    'sentiment_ai': {
        'target_win_rate': 0.75,       # 目标胜率 75%
        'target_sharpe': 1.2,          # 目标夏普比率 1.2
        'max_drawdown_limit': 0.15,    # 最大回撤 15%
        'min_trades': 100,             # 最小交易次数
    },
}

# 回测配置
BACKTEST_CONFIG = {
    'initial_capital': 100000,         # 初始资金 10 万
    'commission_rate': 0.0003,         # 佣金费率 0.03%
    'slippage': 0.001,                 # 滑点 0.1%
    'train_period': '2025-01-01 ~ 2025-12-31',  # 训练集 1 年
    'test_period': '2026-01-01 ~ 2026-03-29',   # 测试集 3 个月
}

def get_param_space(strategy_name: str) -> dict:
    """
    获取策略参数空间
    
    Args:
        strategy_name: 策略名称
    
    Returns:
        参数空间字典
    """
    if strategy_name not in STRATEGY_PARAM_SPACES:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    return STRATEGY_PARAM_SPACES[strategy_name]

def get_optimization_target(strategy_name: str) -> dict:
    """
    获取优化目标
    
    Args:
        strategy_name: 策略名称
    
    Returns:
        优化目标字典
    """
    if strategy_name not in OPTIMIZATION_TARGETS:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    return OPTIMIZATION_TARGETS[strategy_name]
