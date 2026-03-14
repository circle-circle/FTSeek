#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 配置文件加载工具
"""

import yaml
from pathlib import Path
from typing import Dict, Any


def get_config_path() -> Path:
    """获取配置文件路径"""
    return Path(__file__).parent / "position_strategy.yaml"


def load_position_strategy(config_path: Path = None) -> Dict[str, Any]:
    """
    加载持仓管理策略配置
    
    Args:
        config_path: 配置文件路径，默认使用当前目录下的 position_strategy.yaml
        
    Returns:
        配置字典
        
    Raises:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: YAML 语法错误
    """
    if config_path is None:
        config_path = get_config_path()
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """
    验证配置完整性
    
    Args:
        config: 配置字典
        
    Returns:
        验证是否通过
        
    Raises:
        ValueError: 配置项缺失或类型错误
    """
    required_sections = ['position', 'pyramid', 'exit', 'cooldown']
    
    for section in required_sections:
        if section not in config:
            raise ValueError(f"配置缺少必需段落：{section}")
    
    # 验证 position 段落
    position = config['position']
    for key in ['initial_ratio', 'max_position_ratio', 'total_max_ratio']:
        if key not in position:
            raise ValueError(f"position 段落缺少必需配置：{key}")
    
    # 验证 pyramid 段落
    pyramid = config['pyramid']
    if 'enabled' not in pyramid:
        raise ValueError("pyramid 段落缺少 enabled 配置")
    if pyramid['enabled']:
        for level in ['level1', 'level2']:
            if level not in pyramid:
                raise ValueError(f"pyramid 段落缺少 {level} 配置")
            for key in ['price_increase', 'ratio', 'min_confidence']:
                if key not in pyramid[level]:
                    raise ValueError(f"pyramid.{level} 缺少必需配置：{key}")
    
    # 验证 exit 段落
    exit_config = config['exit']
    for key in ['stop_loss', 'take_profit', 'take_profit_ratio']:
        if key not in exit_config:
            raise ValueError(f"exit 段落缺少必需配置：{key}")
    
    # 验证 cooldown 段落
    cooldown = config['cooldown']
    for key in ['hours', 'max_trades_per_day', 'min_confidence', 'add_position_confidence']:
        if key not in cooldown:
            raise ValueError(f"cooldown 段落缺少必需配置：{key}")
    
    return True


def get_config_value(config: Dict[str, Any], path: str, default=None):
    """
    通过路径获取配置值
    
    Args:
        config: 配置字典
        path: 配置路径，如 'position.initial_ratio'
        default: 默认值
        
    Returns:
        配置值或默认值
    """
    keys = path.split('.')
    value = config
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value


if __name__ == '__main__':
    # 测试加载配置
    config = load_position_strategy()
    validate_config(config)
    print("✓ 配置加载并验证成功")
    print(f"  首次开仓比例：{config['position']['initial_ratio']:.0%}")
    print(f"  止损：{config['exit']['stop_loss']:.0%}")
    print(f"  止盈：{config['exit']['take_profit']:.0%}")
    print(f"  冷却时间：{config['cooldown']['hours']} 小时")
