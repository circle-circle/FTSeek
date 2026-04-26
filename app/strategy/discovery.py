"""
策略动态发现引擎 (Strategy Discovery Engine)

自动扫描 app/strategy/ 目录, 发现所有继承自 BaseStrategy 的策略类,
并根据配置自动注册到 STRATEGY_REGISTRY.

新增策略只需:
1. 在 app/strategy/ 目录下创建 .py 文件
2. 定义继承自 BaseStrategy 的类 (带 name 属性)
3. 在 config.yaml 的 strategy.enabled 中添加策略名

无需修改 __init__.py 或 STRATEGY_REGISTRY.

用法:
    from app.strategy.discovery import discover_strategies, get_strategy_info
    
    # 自动发现并注册
    registry = discover_strategies(config=your_config)
    
    # 获取策略信息
    info = get_strategy_info("my_new_strategy")
"""

import os
import re
import logging
import importlib
import inspect
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger("ftseek.strategy.discovery")

# 跳过的文件/目录
SKIP_FILES = {
    '__init__.py', 'base.py', 'indicators.py', 'indicator_cache.py',
    'stock_pool.py', 'strategy_manager.py', 'strategy_commands.py',
    'signal_coordinator.py', 'signal_quality_scorer.py',
    'consensus_engine.py', 'consensus_enhancer.py',
    'transition_manager.py', 'tradingview_webhook.py',
    'market_regime.py', 'market_aware.py',
    'tech_enhancer.py', 'hk_specific.py',
    'lead_lag_tracker.py', 'open_close_effect.py',
    'price_break_v2.py', 'cross_market_scheduler.py',
    'cross_market_news_digest.py', 'cross_market_history.py',
    'fusion_v3.py',  # Empty shell
    # Non-strategy modules
    'confidence_adjuster.py',
}

# 默认启用的策略 (如果 config 中没有 strategy.enabled 配置)
DEFAULT_ENABLED = [
    'confluence_v2',
    'sentiment_ai',
    'open_close_effect',
    'event_driven',
    'cross_market_arbitrage',
    'enhanced_confluence',
]

# 默认禁用的策略 (即使被发现也不会注册)
DEFAULT_DISABLED = [
    'rsi', 'macd', 'bollinger', 'supertrend',
    'confluence', 'price_break', 'ma_cross', 'volume_alert',
]


def discover_strategy_classes(directory: str = None) -> Dict[str, type]:
    """
    扫描策略目录, 发现所有 BaseStrategy 子类.
    
    Args:
        directory: 策略目录路径, 默认使用 app/strategy/
    
    Returns:
        {strategy_name: strategy_class} 字典
    """
    if directory is None:
        directory = Path(__file__).parent
    
    discovered = {}
    strategy_dir = Path(directory)
    
    if not strategy_dir.exists():
        logger.warning(f"策略目录不存在: {strategy_dir}")
        return discovered
    
    # 遍历目录下所有 .py 文件
    for py_file in sorted(strategy_dir.glob('*.py')):
        filename = py_file.name
        
        # 跳过不需要扫描的文件
        if filename in SKIP_FILES:
            continue
        
        module_name = py_file.stem
        
        try:
            # 动态导入模块
            module = importlib.import_module(f'app.strategy.{module_name}')
            
            # 查找模块中所有继承自 BaseStrategy 的类
            from .base import BaseStrategy
            
            for name, obj in inspect.getmembers(module, inspect.isclass):
                # 检查是否继承自 BaseStrategy
                if (issubclass(obj, BaseStrategy) and 
                    obj is not BaseStrategy and
                    obj.__module__ == module.__name__):  # 只取本模块定义的类
                    
                    # 使用类的 name 属性作为策略名, 或使用文件名
                    strategy_name = getattr(obj, 'name', module_name)
                    
                    if strategy_name in discovered:
                        logger.warning(
                            f"策略名称冲突: {strategy_name} "
                            f"(已存在: {discovered[strategy_name].__name__}, "
                            f"新发现: {obj.__name__})"
                        )
                    else:
                        discovered[strategy_name] = obj
                        logger.debug(f"发现策略: {strategy_name} = {obj.__name__}")
        
        except Exception as e:
            logger.warning(f"扫描策略文件 {filename} 失败: {e}")
    
    return discovered


def get_strategy_info(strategy_name: str = None) -> Dict[str, Any]:
    """
    获取策略信息.
    
    Args:
        strategy_name: 策略名, 如果为 None 则返回所有策略信息
    
    Returns:
        策略信息字典
    """
    from . import STRATEGY_REGISTRY
    from .base import BaseStrategy
    
    info = {}
    
    for name, cls in STRATEGY_REGISTRY.items():
        if strategy_name and name != strategy_name:
            continue
        
        # 获取策略类的信息
        cls_info = {
            'class': cls.__name__,
            'module': cls.__module__,
            'doc': (cls.__doc__ or '').strip()[:100] if cls.__doc__ else '',
            'enabled': True,
        }
        
        # 尝试获取类的 name 属性
        if hasattr(cls, 'name'):
            cls_info['name'] = cls.name
        
        info[name] = cls_info
    
    return info if strategy_name is None else info.get(strategy_name)


def discover_strategies(
    config: Any = None,
    enabled_list: List[str] = None,
    disabled_list: List[str] = None,
    registry: Dict[str, type] = None,
) -> Dict[str, type]:
    """
    自动发现并注册策略.
    
    Args:
        config: 配置对象 (支持 config_center.get())
        enabled_list: 显式启用的策略列表
        disabled_list: 显式禁用的策略列表  
        registry: 目标注册表, 默认使用 STRATEGY_REGISTRY
    
    Returns:
        更新后的注册表
    """
    from . import STRATEGY_REGISTRY
    from .base import BaseStrategy
    
    if registry is None:
        registry = STRATEGY_REGISTRY
    
    # 1. 从配置读取启用/禁用列表
    if enabled_list is None:
        if config:
            enabled_cfg = config.get('strategy.enabled')
            if enabled_cfg:
                if hasattr(enabled_cfg, 'to_dict'):
                    enabled_cfg = enabled_cfg.to_dict()
                if isinstance(enabled_cfg, dict):
                    enabled_list = list(enabled_cfg.keys())
                elif isinstance(enabled_cfg, list):
                    enabled_list = enabled_cfg
        
        if enabled_list is None:
            enabled_list = DEFAULT_ENABLED
    
    if disabled_list is None:
        if config:
            disabled_cfg = config.get('strategy.disabled')
            if disabled_cfg:
                if hasattr(disabled_cfg, 'to_dict'):
                    disabled_cfg = disabled_cfg.to_dict()
                if isinstance(disabled_cfg, dict):
                    disabled_list = list(disabled_cfg.keys())
                elif isinstance(disabled_cfg, list):
                    disabled_list = disabled_cfg
        
        if disabled_list is None:
            disabled_list = DEFAULT_DISABLED
    
    # 2. 自动发现所有策略类
    all_strategies = discover_strategy_classes()
    
    # 3. 过滤: 只注册启用的, 排除禁用的
    registered = 0
    skipped_disabled = 0
    skipped_not_enabled = 0
    
    for name, cls in all_strategies.items():
        if name in disabled_list:
            skipped_disabled += 1
            continue
        
        if name in enabled_list:
            registry[name] = cls
            registered += 1
        elif name in registry:
            # Already manually registered, keep it
            registered += 1
        else:
            skipped_not_enabled += 1
            logger.debug(f"策略 {name} 未在启用列表中, 跳过注册")
    
    logger.info(
        f"策略发现完成: 注册={registered}, "
        f"已禁用={skipped_disabled}, 未启用={skipped_not_enabled}, "
        f"总计={len(registry)}"
    )
    
    return registry
