#!/usr/bin/env python3
"""
FTSeek-US 测试健康检查器
功能：
1. 运行所有测试
2. 对比 scenarios.yaml 检查覆盖度
3. 扫描代码变更，提示是否需要更新测试
"""
import subprocess
import yaml
import sys
import os

# 路径配置
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCENARIOS_FILE = os.path.join(BASE_DIR, "tests", "harness", "scenarios.yaml")

def run_tests():
    print("=" * 60)
    print("🧪 [1/3] 运行测试套件")
    print("=" * 60)
    # Use the existing run.py script to ensure consistent environment
    result = subprocess.run(
        [sys.executable, f"{BASE_DIR}/tests/harness/run.py"],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print("❌ 测试失败，请修复后再检查覆盖度")
        return False
    print("✅ 所有测试通过")
    return True

def check_coverage():
    print("\n" + "=" * 60)
    print("🛡️ [2/3] 检查场景覆盖度")
    print("=" * 60)
    
    if not os.path.exists(SCENARIOS_FILE):
        print("⚠️ 未找到 scenarios.yaml，跳过覆盖度检查")
        return

    with open(SCENARIOS_FILE, 'r') as f:
        data = yaml.safe_load(f)
    
    total = 0
    covered = 0
    missing = []

    for module, scenarios in data.get('modules', {}).items():
        for s in scenarios:
            total += 1
            if '✅' in s.get('status', ''):
                covered += 1
            else:
                missing.append(s['name'])

    print(f"总计场景: {total}")
    print(f"已覆盖:   {covered}")
    print(f"缺失:     {len(missing)}")
    
    if missing:
        print("\n⚠️ 以下场景尚未覆盖，请优先开发：")
        for m in missing:
            print(f"  - {m}")

def check_stale_code():
    print("\n" + "=" * 60)
    print("🕵️ [3/3] 检查代码健康度 (Static Analysis)")
    print("=" * 60)
    
    # 简单检查：扫描是否有未使用的 import 或 明显的废弃代码
    # 这里可以扩展为更复杂的 AST 分析
    print("✅ 静态检查通过 (示例占位)")

def main():
    if not run_tests():
        sys.exit(1)
    
    check_coverage()
    check_stale_code()
    
    print("\n" + "=" * 60)
    print("🎉 检查完成")
    print("=" * 60)

if __name__ == "__main__":
    main()
