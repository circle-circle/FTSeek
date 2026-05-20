#!/usr/bin/env python3
"""
FTSeek-US 配置一致性检查器

功能:
1. 验证 unified_config.yaml 是唯一配置源
2. 检查旧配置文件是否已被废弃/迁移
3. 验证参数范围合理性
4. 防止配置分散化

使用方式:
    python scripts/check_config_consistency.py
"""

import sys
import json
import yaml
import os
from pathlib import Path
from typing import List, Tuple

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
UNIFIED_CONFIG = CONFIG_DIR / "unified_config.yaml"

# 应废弃的配置文件 (已合并到 unified_config.yaml)
DEPRECATED_CONFIGS = [
    "us_stock_config.yaml",      # → unified_config.yaml monitoring + defaults
    "short_strategy_config.yaml", # → unified_config.yaml options
    "dynamic_exit_params.yaml",   # → unified_config.yaml dynamic_exit
    "strategy_params.json",       # → unified_config.yaml decision
    "alert_config.json",          # → unified_config.yaml observability
]

# 应保留的配置文件
KEPT_CONFIGS = [
    "unified_config.yaml",       # 统一配置源
    "config_us.py",              # 兼容加载器 (应从 unified_config.yaml 读取)
    "watchlist_us.json",         # 待迁移到 unified_config.yaml
]


def check_file_exists(filepath: Path) -> bool:
    return filepath.exists()


def check_deprecated_configs() -> List[Tuple[str, str]]:
    """检查废弃文件是否仍存在"""
    issues = []
    for filename in DEPRECATED_CONFIGS:
        filepath = CONFIG_DIR / filename
        if filepath.exists():
            issues.append((
                "WARNING",
                f"发现废弃配置文件: {filename} - 应已合并到 unified_config.yaml"
            ))
    return issues


def check_unified_config() -> List[Tuple[str, str]]:
    """验证统一配置文件"""
    issues = []
    
    if not check_file_exists(UNIFIED_CONFIG):
        issues.append(("ERROR", f"统一配置文件不存在: {UNIFIED_CONFIG}"))
        return issues
    
    try:
        with open(UNIFIED_CONFIG, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        issues.append(("ERROR", f"YAML 解析失败: {e}"))
        return issues
    
    # 检查关键配置节是否存在
    required_sections = [
        'base', 'trading_hours', 'watchlist', 'decision', 
        'risk', 'options', 'dynamic_exit', 'execution',
        'service', 'observability', 'logging', 'database'
    ]
    
    for section in required_sections:
        if section not in config:
            issues.append(("ERROR", f"缺少必需配置节: {section}"))
    
    # 验证期权配置一致性
    opt = config.get('options', {})
    profit = opt.get('profit_target_pct', 0)
    stop = abs(opt.get('stop_loss_pct', 0))
    
    if profit > 0 and stop > 0:
        ratio = profit / stop
        if ratio < 1.0:
            issues.append(("ERROR", f"期权盈亏比不合理: 止盈/止损 = {ratio:.2f} < 1.0"))
        elif ratio < 1.5:
            issues.append(("WARNING", f"期权盈亏比较低: 止盈/止损 = {ratio:.2f}，建议 >= 1.5"))
    
    # 验证时间止损
    max_hours = opt.get('max_holding_hours', 0)
    if max_hours > 0 and max_hours < 24:
        issues.append(("WARNING", f"期权最大持仓时间 {max_hours}h < 24h，可能过短"))
    elif max_hours > 120:
        issues.append(("WARNING", f"期权最大持仓时间 {max_hours}h > 120h，可能导致价值流失"))
    
    return issues


def check_code_references() -> List[Tuple[str, str]]:
    """检查代码中是否有硬编码配置"""
    issues = []
    app_dir = BASE_DIR / "app"
    
    if not app_dir.exists():
        return issues
    
    # 搜索常见硬编码模式
    patterns = [
        (r'=\s*0\.\d{2,}', "可能的硬编码浮点数"),
        (r'[A-Z_]{4,}\s*=\s*\d+', "可能的硬编码常量"),
    ]
    
    import re
    for py_file in app_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding='utf-8')
            for pattern, desc in patterns:
                matches = re.findall(pattern, content)
                if len(matches) > 5:  # 超过 5 处可能有问题
                    issues.append((
                        "INFO",
                        f"{py_file.relative_to(BASE_DIR)}: 发现 {len(matches)} 处 {desc}"
                    ))
        except Exception:
            pass
    
    return issues


def main():
    print("=" * 60)
    print("FTSeek-US 配置一致性检查")
    print("=" * 60)
    
    all_issues = []
    
    # 1. 检查废弃文件
    print("\n[1/4] 检查废弃配置文件...")
    issues = check_deprecated_configs()
    all_issues.extend(issues)
    for level, msg in issues:
        print(f"  [{level}] {msg}")
    if not issues:
        print("  ✅ 无废弃文件")
    
    # 2. 验证统一配置
    print("\n[2/4] 验证统一配置文件...")
    issues = check_unified_config()
    all_issues.extend(issues)
    for level, msg in issues:
        print(f"  [{level}] {msg}")
    errors = [i for i in issues if i[0] == "ERROR"]
    if not errors:
        print("  ✅ 统一配置验证通过")
    
    # 3. 检查代码引用
    print("\n[3/4] 检查代码硬编码...")
    issues = check_code_references()
    all_issues.extend(issues)
    for level, msg in issues:
        print(f"  [{level}] {msg}")
    if not issues:
        print("  ✅ 无明显硬编码问题")
    
    # 4. 配置完整性
    print("\n[4/4] 配置完整性检查...")
    if UNIFIED_CONFIG.exists():
        with open(UNIFIED_CONFIG, 'r') as f:
            config = yaml.safe_load(f)
        
        sections = list(config.keys())
        print(f"  配置节数量: {len(sections)}")
        print(f"  配置节列表: {', '.join(sections)}")
    
    # 总结
    print("\n" + "=" * 60)
    errors = [i for i in all_issues if i[0] == "ERROR"]
    warnings = [i for i in all_issues if i[0] == "WARNING"]
    
    print(f"总计: {len(errors)} 错误, {len(warnings)} 警告")
    
    if errors:
        print("\n❌ 配置检查未通过，请修复错误后重试")
        return 1
    else:
        print("\n✅ 配置检查通过")
        return 0


if __name__ == "__main__":
    sys.exit(main())
