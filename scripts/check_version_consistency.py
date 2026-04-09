#!/usr/bin/env python3
"""
FTSeek 版本一致性检查脚本
版本：v5.0.0
功能：检查所有配置文件的版本一致性
"""

import os
import sys
import yaml
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple

# 颜色输出
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'
    BOLD = '\033[1m'

def print_success(msg):
    print(f"{Colors.GREEN}✓{Colors.END} {msg}")

def print_error(msg):
    print(f"{Colors.RED}✗{Colors.END} {msg}")

def print_warning(msg):
    print(f"{Colors.YELLOW}⚠{Colors.END} {msg}")

def print_info(msg):
    print(f"{Colors.BLUE}ℹ{Colors.END} {msg}")

def load_yaml_file(path: Path) -> dict:
    """加载 YAML 文件"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        return {'_error': str(e)}

def load_json_file(path: Path) -> dict:
    """加载 JSON 文件"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception as e:
        return {'_error': str(e)}

def extract_version(config: dict) -> str:
    """从配置中提取版本号"""
    # 尝试多种版本号位置
    if 'version' in config:
        ver = config['version']
        return str(ver) if not isinstance(ver, dict) else None
    
    if '_meta' in config and 'version' in config['_meta']:
        return config['_meta']['version']
    
    return None

def check_file_version(path: Path, expected_version: str) -> Tuple[bool, str, str]:
    """
    检查文件版本
    
    Returns:
        (是否通过，实际版本，消息)
    """
    if not path.exists():
        return (True, 'N/A', f"文件不存在 (可能已弃用)")
    
    if path.suffix in ['.yaml', '.yml']:
        config = load_yaml_file(path)
    elif path.suffix == '.json':
        config = load_json_file(path)
    else:
        return (True, 'N/A', f"跳过非配置文件")
    
    if '_error' in config:
        return (False, 'ERROR', f"加载失败：{config['_error']}")
    
    actual_version = extract_version(config)
    
    if actual_version is None:
        return (True, 'N/A', f"无版本号字段")
    
    # 版本号比较 (宽松匹配，允许主版本一致)
    actual_major = actual_version.split('.')[0]
    expected_major = expected_version.split('.')[0]
    
    if actual_major == expected_major:
        return (True, actual_version, f"版本一致 (v{actual_version})")
    else:
        return (False, actual_version, f"版本不一致 (期望 v{expected_version}, 实际 v{actual_version})")

def main():
    """主函数"""
    print(f"\n{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"{Colors.BOLD}FTSeek 版本一致性检查{Colors.END}")
    print(f"{Colors.BOLD}{'='*70}{Colors.END}\n")
    
    config_dir = Path(__file__).parent.parent / 'config'
    version_file = config_dir / 'version.yaml'
    
    # 加载版本管理文件
    if not version_file.exists():
        print_error(f"版本管理文件不存在：{version_file}")
        sys.exit(1)
    
    version_config = load_yaml_file(version_file)
    expected_version = version_config.get('version', '5.0.0')
    
    print_info(f"期望版本：v{expected_version}")
    print_info(f"配置目录：{config_dir}\n")
    
    # 检查结果统计
    results = {
        'passed': 0,
        'failed': 0,
        'warnings': 0,
        'skipped': 0
    }
    
    # 检查活跃配置文件
    print(f"{Colors.BOLD}检查活跃配置文件:{Colors.END}")
    print("-" * 70)
    
    active_configs = version_config.get('configs', {})
    
    for config_name, config_info in active_configs.items():
        if config_info.get('status') != 'active':
            continue
        
        file_path = config_dir / config_info.get('file', f"{config_name}.yaml")
        passed, actual_ver, message = check_file_version(file_path, expected_version)
        
        if passed:
            if actual_ver == 'N/A':
                print_warning(f"{config_name:20} {message}")
                results['warnings'] += 1
            else:
                print_success(f"{config_name:20} {message}")
                results['passed'] += 1
        else:
            print_error(f"{config_name:20} {message}")
            results['failed'] += 1
    
    # 检查 FTSeek-US 配置
    print(f"\n{Colors.BOLD}检查 FTSeek-US 配置文件:{Colors.END}")
    print("-" * 70)
    
    us_config_dir = Path('/opt/ftseek/FTSeek-US/config')
    us_configs = version_config.get('us_configs', {})
    
    for config_name, config_info in us_configs.items():
        file_path = us_config_dir / config_info.get('file', f"{config_name}.yaml")
        passed, actual_ver, message = check_file_version(file_path, expected_version)
        
        if passed:
            if actual_ver == 'N/A':
                print_warning(f"{config_name:20} {message}")
                results['warnings'] += 1
            else:
                print_success(f"{config_name:20} {message}")
                results['passed'] += 1
        else:
            print_error(f"{config_name:20} {message}")
            results['failed'] += 1
    
    # 检查已弃用文件
    print(f"\n{Colors.BOLD}检查已弃用配置文件:{Colors.END}")
    print("-" * 70)
    
    for config_name, config_info in active_configs.items():
        if config_info.get('status') != 'deprecated':
            continue
        
        file_path = config_dir / config_info.get('file', f"{config_name}.yaml")
        exists = file_path.exists()
        remove_after = config_info.get('remove_after', '')
        
        if exists:
            today = datetime.now().date()
            # Handle both string and date types from YAML
            if isinstance(remove_after, str):
                remove_date = datetime.strptime(remove_after, '%Y-%m-%d').date()
            elif isinstance(remove_after, datetime):
                remove_date = remove_after.date()
            else:
                remove_date = None
            if remove_date and today > remove_date:
                print_warning(f"{config_name:20} 已过期，应删除 (过期日：{remove_after})")
                results['warnings'] += 1
            else:
                print_info(f"{config_name:20} 保留中 (删除期限：{remove_after})")
                results['skipped'] += 1
        else:
            print_success(f"{config_name:20} 已清理")
            results['passed'] += 1
    
    # 汇总结果
    print(f"\n{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"{Colors.BOLD}检查结果汇总:{Colors.END}")
    print(f"{'='*70}")
    print_success(f"通过：{results['passed']}")
    if results['failed'] > 0:
        print_error(f"失败：{results['failed']}")
    if results['warnings'] > 0:
        print_warning(f"警告：{results['warnings']}")
    print_info(f"跳过：{results['skipped']}")
    
    # 退出码
    if results['failed'] > 0:
        print(f"\n{Colors.RED}❌ 版本一致性检查失败{Colors.END}\n")
        sys.exit(1)
    else:
        print(f"\n{Colors.GREEN}✅ 版本一致性检查通过{Colors.END}\n")
        sys.exit(0)

if __name__ == "__main__":
    main()
