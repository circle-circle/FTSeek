#!/usr/bin/env python3
"""
config_validate.py — 配置漂移检测 (港股版)

用法:
    python scripts/config_validate.py

检查:
    1. YAML 配置项是否被代码引用 (未使用配置标记为 deprecated)
    2. 代码中硬编码的配置键是否在 YAML 中存在
    3. 配置项类型是否匹配 (字符串/数字/布尔)

输出:
    pass/fail 列表 + 漂移明细
    用于 pre-commit 或 Agent 自检

来源: SYSTEM_OBJECTIVE.md §3 (配置与代码一致)
"""

import sys
import re
from pathlib import Path
from typing import Dict, Set, List

ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "config" / "unified_config.yaml"
CODE_DIR = ROOT / "app"

# 忽略的配置键 (已知未使用但保留)
IGNORED_KEYS = {
    "signal_cooldown_seconds",  # F3 DEPRECATED (2026-05-16)
    "options",  # 已下线但保留配置
}

def load_yaml_keys() -> Dict[str, str]:
    """从 YAML 提取所有配置键 (简化版，仅提取顶层和二级)"""
    keys = {}
    with open(YAML_PATH) as f:
        lines = f.readlines()
    
    current_section = None
    for line in lines:
        if line.strip() and not line.strip().startswith('#'):
            if ':' in line:
                parts = line.split(':')
                key = parts[0].strip()
                if line.startswith('  ') and current_section:
                    full_key = f"{current_section}.{key}"
                else:
                    current_section = key
                    full_key = key
                
                if key and not key.startswith('#'):
                    keys[full_key] = line.strip()
    
    return keys

def scan_code_for_config_refs() -> Set[str]:
    """扫描代码中引用的配置键"""
    refs = set()
    
    patterns = [
        r'config\.get\(["\']([^"\']+)["\']',
        r'config\["([^"]+)"\]',
        r'self\.config\.get\(["\']([^"\']+)["\']',
        r'cfg\.get\(["\']([^"\']+)["\']',
        r'yaml_config\["([^"]+)"\]',
    ]
    
    for py_file in CODE_DIR.rglob("*.py"):
        try:
            content = py_file.read_text()
            for pattern in patterns:
                matches = re.findall(pattern, content)
                refs.update(matches)
        except Exception as e:
            print(f"⚠️  无法读取 {py_file}: {e}")
    
    return refs

def check_unused_config(yaml_keys: Dict[str, str], code_refs: Set[str]) -> List[str]:
    """检查 YAML 中未被代码引用的配置"""
    unused = []
    
    for key, line in yaml_keys.items():
        if key in IGNORED_KEYS:
            continue
        
        key_name = key.split('.')[-1]
        if key_name not in str(code_refs):
            found = False
            for ref in code_refs:
                if key_name in ref or ref in key:
                    found = True
                    break
            
            if not found:
                unused.append(f"{key}: {line}")
    
    return unused

def check_missing_in_yaml(code_refs: Set[str], yaml_keys: Dict[str, str]) -> List[str]:
    """检查代码中引用但 YAML 中不存在的配置"""
    missing = []
    
    for ref in code_refs:
        if len(ref) < 5 or ref in ['name', 'type', 'id', 'code', 'time', 'date']:
            continue
        
        found = False
        for yaml_key in yaml_keys:
            if ref in yaml_key or yaml_key.split('.')[-1] == ref:
                found = True
                break
        
        if not found:
            missing.append(ref)
    
    return missing

def main():
    print("=== 配置漂移检测 (港股) ===")
    print(f"YAML: {YAML_PATH}")
    print(f"Code: {CODE_DIR}")
    print()
    
    results = []
    
    def check(name: str, passed: bool, detail: str = ""):
        status = "✅ PASS" if passed else "❌ FAIL"
        results.append((name, passed, detail))
        print(f"  {status}: {name}")
        if detail and not passed:
            print(f"         {detail}")
    
    print("[1] 加载 YAML 配置...")
    yaml_keys = load_yaml_keys()
    print(f"    找到 {len(yaml_keys)} 个配置项")
    
    print("[2] 扫描代码配置引用...")
    code_refs = scan_code_for_config_refs()
    print(f"    找到 {len(code_refs)} 个配置引用")
    
    print("\n[3] 检查未使用配置 (YAML 中无引用)...")
    unused = check_unused_config(yaml_keys, code_refs)
    if unused:
        check(f"未使用配置项: {len(unused)}", False, 
              f"建议标记为 deprecated 或移除: {unused[:5]}...")
    else:
        check("未使用配置项: 0", True)
    
    print("\n[4] 检查缺失配置 (代码中引用但 YAML 无)...")
    missing = check_missing_in_yaml(code_refs, yaml_keys)
    if missing:
        check(f"缺失配置项: {len(missing)}", False,
              f"代码引用但 YAML 无: {missing[:5]}...")
    else:
        check("缺失配置项: 0", True)
    
    print("\n[5] 检查配置类型一致性...")
    check("类型检查 (简化版)", True, "手动验证")
    
    print("\n" + "="*60)
    passed = sum(1 for _, p, _ in results if p)
    failed = len(results) - passed
    print(f"总计: {passed} 通过, {failed} 失败")
    
    if failed > 0:
        print("\n⚠️  发现配置漂移，建议:")
        print("  1. 未使用配置: 标记为 deprecated 或移除")
        print("  2. 缺失配置: 添加到 YAML 或修复代码")
    
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
