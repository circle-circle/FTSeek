#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置迁移工具

功能:
1. 将旧配置文件合并到新的统一配置
2. 备份旧配置
3. 验证新配置的完整性
4. 提供回滚功能

使用方法:
    python config/migrate_to_unified.py
"""

import os
import sys
import json
import yaml
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

CONFIG_DIR = Path(__file__).parent
BACKUP_DIR = CONFIG_DIR / "backups" / datetime.now().strftime("%Y%m%d_%H%M%S")


def load_yaml(path: Path) -> Dict:
    """加载 YAML 文件"""
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path) -> Dict:
    """加载 JSON 文件"""
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_yaml(data: Dict, path: Path):
    """保存 YAML 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def deep_merge(base: Dict, override: Dict) -> Dict:
    """深度合并字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def backup_configs():
    """备份现有配置文件"""
    print(f"[备份] 创建备份目录：{BACKUP_DIR}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    files_to_backup = [
        "unified_config.yaml",
        "risk_config.yaml",
        "strategy_params.json",
        "dynamic_exit_params.yaml",
        "position_strategy.yaml",
        "app_config.yaml",
        "system_config.yaml",
        "alert_rules.yaml",
    ]

    for filename in files_to_backup:
        src = CONFIG_DIR / filename
        if src.exists():
            dst = BACKUP_DIR / filename
            shutil.copy2(src, dst)
            print(f"  ✓ 已备份：{filename}")

    print(f"[备份] 完成，备份位置：{BACKUP_DIR}")
    return BACKUP_DIR


def migrate_old_configs() -> Dict[str, Any]:
    """迁移旧配置文件到新统一配置"""
    print("\n[迁移] 开始加载旧配置文件...")

    # 加载所有旧配置
    configs = {}

    # 1. risk_config.yaml
    risk_config = load_yaml(CONFIG_DIR / "risk_config.yaml")
    if risk_config:
        print("  ✓ 已加载：risk_config.yaml")
        configs['risk'] = risk_config

    # 2. strategy_params.json
    strategy_params = load_json(CONFIG_DIR / "strategy_params.json")
    if strategy_params:
        print("  ✓ 已加载：strategy_params.json")
        configs['strategy'] = strategy_params

    # 3. dynamic_exit_params.yaml
    exit_params = load_yaml(CONFIG_DIR / "dynamic_exit_params.yaml")
    if exit_params:
        print("  ✓ 已加载：dynamic_exit_params.yaml")
        configs['exit'] = exit_params

    # 4. position_strategy.yaml
    position_config = load_yaml(CONFIG_DIR / "position_strategy.yaml")
    if position_config:
        print("  ✓ 已加载：position_strategy.yaml")
        configs['position'] = position_config

    # 5. alert_rules.yaml
    alert_rules = load_yaml(CONFIG_DIR / "alert_rules.yaml")
    if alert_rules:
        print("  ✓ 已加载：alert_rules.yaml")
        configs['alerts'] = alert_rules

    return configs


def build_unified_config(old_configs: Dict[str, Any]) -> Dict[str, Any]:
    """构建统一配置"""
    print("\n[构建] 合并配置到新格式...")

    # 基础模板
    unified = {
        'version': '2.0.0',
        'updated': datetime.now().strftime('%Y-%m-%d'),
        'description': 'FTSeek 统一配置文件',
    }

    # 1. 从 risk_config 迁移
    if 'risk' in old_configs:
        risk = old_configs['risk']

        # 交易模式
        unified['trading'] = {
            'mode': risk.get('trading', {}).get('mode', 'paper'),
            'max_daily_trades': risk.get('risk_limits', {}).get('max_daily_trades', 10),
        }

        # 风控限制
        unified['risk_limits'] = risk.get('risk_limits', {})

        # 止盈配置
        unified['take_profit'] = risk.get('take_profit', {})

        # 信号冷却
        unified['signal_cooldown'] = risk.get('signal_cooldown', {})

        # 告警配置
        unified['notification'] = {
            'dingtalk': {
                'enabled': True,
                'webhook_url': risk.get('alerts', {}).get('dingtalk_webhook', ''),
                'secret': risk.get('alerts', {}).get('dingtalk_secret', ''),
            }
        }

        print("  ✓ 已迁移：风控配置")

    # 2. 从 strategy_params 迁移
    if 'strategy' in old_configs:
        strategy = old_configs['strategy']

        unified['strategy'] = {
            'enabled': {
                'primary': strategy.get('enabled_strategies', {}).get('primary', 'confluence_v2'),
                'trend': 'supertrend',
                'sentiment': 'sentiment_ai',
            },
            'weights': strategy.get('strategy_weights', {
                'confluence_v2': 1.5,
                'supertrend': 1.0,
                'sentiment_ai': 0.8,
            }),
            'consensus': strategy.get('consensus', {}),
            'ai': {
                'weight': strategy.get('ai_weight', 0.15),
                'min_accuracy': strategy.get('ai_min_accuracy', 0.5),
            },
            'fusion': strategy.get('fusion', {}),
        }

        print("  ✓ 已迁移：策略配置")

    # 3. 从 dynamic_exit_params 迁移
    if 'exit' in old_configs:
        unified['dynamic_exit'] = old_configs['exit']
        print("  ✓ 已迁移：动态止盈止损配置")

    # 4. 从 position_strategy 迁移
    if 'position' in old_configs:
        position = old_configs['position']

        unified['trading']['max_single_position'] = position.get('max_single_ratio', 0.20)
        unified['trading']['max_total_position'] = position.get('max_total_ratio', 0.60)

        print("  ✓ 已迁移：仓位配置")

    # 5. 从 alert_rules 迁移
    if 'alerts' in old_configs:
        unified['notification']['alerts'] = old_configs['alerts']
        print("  ✓ 已迁移：告警规则")

    return unified


def validate_config(config: Dict) -> List[str]:
    """验证配置完整性"""
    print("\n[验证] 检查配置完整性...")
    errors = []

    # 检查必要字段
    required_fields = [
        ('trading', '交易配置'),
        ('risk_limits', '风控限制'),
        ('strategy', '策略配置'),
    ]

    for field, name in required_fields:
        if field not in config:
            errors.append(f"缺少必要配置：{name} ({field})")

    # 检查风控字段
    if 'risk_limits' in config:
        risk = config['risk_limits']
        if 'max_drawdown' not in risk:
            errors.append("风控配置缺少 max_drawdown")
        if 'max_daily_trades' not in risk:
            errors.append("风控配置缺少 max_daily_trades")

    if errors:
        print("  ✗ 发现错误:")
        for error in errors:
            print(f"    - {error}")
    else:
        print("  ✓ 配置验证通过")

    return errors


def main():
    """主函数"""
    print("=" * 60)
    print("FTSeek 配置迁移工具 v1.0")
    print("=" * 60)

    # 1. 备份现有配置
    backup_dir = backup_configs()

    # 2. 加载旧配置
    old_configs = migrate_old_configs()

    if not old_configs:
        print("\n[警告] 未找到任何旧配置文件")
        return False

    # 3. 构建统一配置
    unified_config = build_unified_config(old_configs)

    # 4. 验证配置
    errors = validate_config(unified_config)

    if errors:
        print("\n[警告] 配置验证失败，但仍会生成文件")
        print(f"[提示] 请手动修复：{', '.join(errors)}")

    # 5. 保存新配置
    output_path = CONFIG_DIR / "config.yaml"
    print(f"\n[保存] 写入新配置：{output_path}")

    save_yaml(unified_config, output_path)
    print(f"  ✓ 配置已保存")

    # 6. 输出总结
    print("\n" + "=" * 60)
    print("迁移完成!")
    print("=" * 60)
    print(f"\n新配置文件：{output_path}")
    print(f"备份位置：{backup_dir}")
    print("\n下一步:")
    print("  1. 检查新配置文件 config.yaml")
    print("  2. 手动补充缺失的配置项")
    print("  3. 修改代码使用新的 ConfigCenter")
    print("  4. 测试配置加载")
    print("  5. 确认无误后将旧配置移至 config/deprecated/")

    return len(errors) == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
