#!/usr/bin/env python3
"""
FTSeek-US 配置漂移检查器 (Config Drift Detector)

用途:
    定期检查 config_us.py 是否存在未经授权的硬编码配置。
    如果 Agent 或开发者绕过了 unified_config.yaml 直接修改了代码，
    此脚本将发出警告。

运行方式:
    python3 scripts/check_config_drift.py
"""
import re
import sys
from pathlib import Path

CONFIG_US_PATH = Path(__file__).resolve().parent.parent / "config" / "config_us.py"

# 允许的模式 (即合法的 Loader 导入或环境变量)
ALLOWED_PATTERNS = [
    r"^\s*#",                          # 注释
    r"^\s*$",                          # 空行
    r"^\s*import\s",                   # import 语句
    r"^\s*from\s.*\simport\s",         # from ... import ...
    r"^\s*def\s",                      # 函数定义
    r"^\s*class\s",                    # 类定义
    r"^\s*_loader\s*=\s*",             # Loader 初始化
    r"^\s*EXECUTION_CONFIG\s*=\s*_loader",   # 合法的 Loader 属性导出
    r"^\s*OBSERVABILITY_CONFIG\s*=\s*_loader",
    r"^\s*DATABASE_CONFIG\s*=\s*_loader",
    r"^\s*LOGGING_CONFIG\s*=\s*_loader",
    r"^\s*AGENT_CONFIG\s*=\s*_loader",
    r"^\s*FEATURE_FLAGS\s*=\s*_loader",
    r"^\s*RISK_CONFIG\s*=\s*_loader",
    r"^\s*DECISION_CONFIG\s*=\s*_loader",
    r"^\s*WATCH_LIST_US\s*=\s*_loader",
    r"^\s*BASE_DIR\s*=\s*",            # 路径定义
    r"^\s*DATA_DIR\s*=\s*",
    r"^\s*LOG_DIR\s*=\s*",
    r"^\s*CONFIG_DIR\s*=\s*",
    r"^\s*WATCHLIST_FILE\s*=\s*",
    r"^\s*WATCHLIST_CONFIG\s*=\s*json\.load", # JSON 加载
    r"^\s*WATCHLIST_CONFIG\s*=\s*\{",         # JSON 兜底
    r"^\s*return\s",                   # 返回值
    r"^\s*raise\s",                    # 异常
    r"^\s*try:",                       # Try 块
    r"^\s*except",                     # Except 块
    r"^\s*if\s",                       # 条件
    r"^\s*else:",                      # 条件
]

def is_allowed_line(line: str) -> bool:
    """检查该行是否属于合法模式"""
    stripped = line.strip()
    for pattern in ALLOWED_PATTERNS:
        if re.match(pattern, stripped):
            return True
    return False

def check_config_us():
    if not CONFIG_US_PATH.exists():
        print(f"❌ 错误: 找不到文件 {CONFIG_US_PATH}")
        return False

    drifts = []
    with open(CONFIG_US_PATH, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    for line_num, line in enumerate(lines, start=1):
        # 排除合法模式
        if is_allowed_line(line):
            continue
        
        # 检测潜在的硬编码配置赋值 (例如 MAX_POSITION = 0.2)
        # 模式: 大写变量名 = 值
        if re.match(r"\s*[A-Z_][A-Z0-9_]*\s*=\s*.+", line):
            # 排除掉环境变量读取 (os.getenv)
            if "os.getenv" not in line:
                drifts.append((line_num, line.strip()))

    if drifts:
        print("⚠️ 检测到配置漂移 (Config Drift):")
        print("以下变量似乎在 config_us.py 中直接赋值，未使用 Loader 导入:")
        for line_num, code in drifts:
            print(f"   Line {line_num}: {code[:60]}...")
        print("\n🛑 请将上述配置移至 config/unified_config.yaml 并通过 _loader 导出。")
        return False
    else:
        print("✅ 配置一致性检查通过: config_us.py 无硬编码漂移。")
        return True

if __name__ == "__main__":
    success = check_config_us()
    sys.exit(0 if success else 1)
