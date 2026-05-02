"""
配置验证工具 — 验证 unified_config.yaml 完整性和一致性

用法:
    python -m app.core.config_validator
    
或直接导入:
    from app.core.config_validator import validate_config
    issues = validate_config()
"""

import os
import sys
import logging
from typing import List, Dict, Any
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.core.config_loader import cfg, ConfigLoader

logger = logging.getLogger("ftseek.config_validator")


# 必需的配置项
_REQUIRED_KEYS = [
    "base.project_name",
    "base.version",
    "futu.host",
    "futu.port",
    "futu.trade_env",
    "futu.simulate_account_id",
    "opend.host",
    "opend.port",
    "trading.watch_list_hk",
    "trading.watch_list_us",
    "trading.max_open_positions",
    "trading.default_position_size",
    "risk_limits.max_single_position",
    "risk_limits.max_daily_loss",
    "risk_limits.max_drawdown",
    "stop_loss.default_percent",
    "take_profit.default_percent",
    "strategy.consensus.threshold",
    "strategy.consensus.window_seconds",
]

# 配置类型检查
_TYPE_CHECKS = {
    "futu.port": int,
    "futu.trade_env": int,
    "opend.port": int,
    "trading.max_open_positions": int,
    "trading.default_position_size": float,
    "risk_limits.max_single_position": float,
    "risk_limits.max_daily_loss": float,
    "risk_limits.max_drawdown": float,
    "stop_loss.default_percent": float,
    "take_profit.default_percent": float,
    "strategy.consensus.threshold": float,
    "strategy.consensus.window_seconds": int,
}


def validate_config() -> List[Dict[str, str]]:
    """
    验证配置完整性和一致性
    
    Returns:
        问题列表，每项包含:
        {
            "level": "error" | "warning" | "info",
            "key": "配置键",
            "message": "问题描述"
        }
    """
    issues = []
    
    # 1. 检查配置文件是否存在
    config_path = cfg.config_path
    if not config_path.exists():
        issues.append({
            "level": "error",
            "key": "config_file",
            "message": f"配置文件不存在: {config_path}"
        })
        return issues
    
    # 2. 检查必需配置项
    for key in _REQUIRED_KEYS:
        value = cfg.get(key)
        if value is None:
            issues.append({
                "level": "error",
                "key": key,
                "message": f"必需配置项缺失: {key}"
            })
        elif isinstance(value, str) and not value:
            issues.append({
                "level": "warning",
                "key": key,
                "message": f"配置项为空: {key}"
            })
    
    # 3. 类型检查
    for key, expected_type in _TYPE_CHECKS.items():
        value = cfg.get(key)
        if value is not None and not isinstance(value, expected_type):
            try:
                expected_type(value)
            except (ValueError, TypeError):
                issues.append({
                    "level": "error",
                    "key": key,
                    "message": f"类型错误: {key} 应为 {expected_type.__name__}, 实际为 {type(value).__name__}"
                })
    
    # 4. 值范围检查
    value_ranges = {
        "stop_loss.default_percent": (0.01, 0.10),
        "take_profit.default_percent": (0.02, 0.20),
        "risk_limits.max_drawdown": (0.05, 0.30),
        "risk_limits.max_daily_loss": (0.01, 0.10),
    }
    
    for key, (min_val, max_val) in value_ranges.items():
        value = cfg.get(key)
        if value is not None:
            if not (min_val <= value <= max_val):
                issues.append({
                    "level": "warning",
                    "key": key,
                    "message": f"值超出合理范围: {key} = {value} (应在 {min_val}-{max_val} 之间)"
                })
    
    # 5. watchlist 检查
    watchlist_hk = cfg.get("trading.watch_list_hk", [])
    if isinstance(watchlist_hk, list):
        if len(watchlist_hk) == 0:
            issues.append({
                "level": "warning",
                "key": "trading.watch_list_hk",
                "message": "港股监控列表为空"
            })
        elif len(watchlist_hk) > 50:
            issues.append({
                "level": "warning",
                "key": "trading.watch_list_hk",
                "message": f"港股监控列表过长 ({len(watchlist_hk)} 只)，可能超过富途订阅限制"
            })
        
        # 检查股票代码格式
        for code in watchlist_hk:
            if not code.startswith(("HK.", "US.")):
                issues.append({
                    "level": "error",
                    "key": "trading.watch_list_hk",
                    "message": f"股票代码格式错误: {code} (应以 HK. 或 US. 开头)"
                })
    
    # 6. 订阅槽位检查
    subscribe_types = cfg.get("strategy.subscribe_types", ["QUOTE", "K_1M"])
    if isinstance(watchlist_hk, list) and isinstance(subscribe_types, list):
        slots = len(watchlist_hk) * len(subscribe_types)
        if slots > 100:
            issues.append({
                "level": "error",
                "key": "subscription_slots",
                "message": f"订阅槽位 {slots} 超过富途限制 100 ({len(watchlist_hk)} × {len(subscribe_types)})"
            })
    
    # 7. 环境变量解析检查
    all_config = cfg.all
    unresolved = []
    for key, value in _flatten_dict(all_config).items():
        if isinstance(value, str) and "${" in value and "}" in value:
            unresolved.append(key)
    
    if unresolved:
        issues.append({
            "level": "info",
            "key": "env_vars",
            "message": f"以下配置项包含未解析的环境变量: {', '.join(unresolved[:5])}"
        })
    
    return issues


def _flatten_dict(d: dict, parent_key: str = '', sep: str = '.') -> dict:
    """扁平化字典"""
    items = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.update(_flatten_dict(v, new_key, sep))
        else:
            items[new_key] = v
    return items


def print_validation_report():
    """打印验证报告"""
    print("=" * 60)
    print("📋 FTSeek 配置验证报告")
    print("=" * 60)
    print(f"配置文件: {cfg.config_path}")
    print(f"加载时间: {cfg.load_time}")
    print(f"配置项数: {len(_flatten_dict(cfg.all))}")
    print()
    
    issues = validate_config()
    
    if not issues:
        print("✅ 配置验证通过，无问题")
    else:
        errors = [i for i in issues if i["level"] == "error"]
        warnings = [i for i in issues if i["level"] == "warning"]
        infos = [i for i in issues if i["level"] == "info"]
        
        if errors:
            print(f"❌ 错误 ({len(errors)}):")
            for issue in errors:
                print(f"  - [{issue['key']}] {issue['message']}")
        
        if warnings:
            print(f"\n⚠️  警告 ({len(warnings)}):")
            for issue in warnings:
                print(f"  - [{issue['key']}] {issue['message']}")
        
        if infos:
            print(f"\nℹ️  提示 ({len(infos)}):")
            for issue in infos:
                print(f"  - [{issue['key']}] {issue['message']}")
    
    print()
    
    # 配置摘要
    print("【配置摘要】")
    print(f"  项目: {cfg.get('base.project_name', 'N/A')} v{cfg.get('base.version', 'N/A')}")
    print(f"  市场: {cfg.get('base.market', 'N/A')}")
    print(f"  交易环境: {cfg.get('futu.trade_env', 'N/A')} (1=模拟, 0=实盘)")
    print(f"  港股数量: {len(cfg.get('trading.watch_list_hk', []))}")
    print(f"  美股数量: {len(cfg.get('trading.watch_list_us', []))}")
    print(f"  止损: {cfg.get('stop_loss.default_percent', 'N/A'):.2%}")
    print(f"  止盈: {cfg.get('take_profit.default_percent', 'N/A'):.2%}")
    print(f"  最大回撤: {cfg.get('risk_limits.max_drawdown', 'N/A'):.2%}")
    print(f"  共识阈值: {cfg.get('strategy.consensus.threshold', 'N/A')}")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    print_validation_report()
