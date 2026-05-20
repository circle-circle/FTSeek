#!/usr/bin/env python3
"""
FTSeek-US SSOT 配置一致性检查 (pre-commit 用)
检查 unified_config.yaml 与 config_defaults.py 之间是否存在漂移。
"""
import sys
import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = PROJECT_ROOT / "config" / "unified_config.yaml"


def _resolve_env_vars(value):
    """展开 ${VAR} 环境变量占位符"""
    import re

    if isinstance(value, str):
        return re.sub(
            r"\$\{(\w+)\}",
            lambda m: "RESOLVED",
            value,
        )
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def main():
    if not YAML_PATH.exists():
        print(f"❌ 配置文件不存在: {YAML_PATH}")
        sys.exit(1)

    with open(YAML_PATH) as f:
        raw = yaml.safe_load(f) or {}

    # 展开环境变量 (避免 ${VAR} 被当作字面值比较)
    raw = _resolve_env_vars(raw)
    yaml_data = {k.lower(): v for k, v in raw.items()}

    sys.path.insert(0, str(PROJECT_ROOT))
    from app.core.config_audit import audit_config_consistency

    warnings = audit_config_consistency(yaml_data)
    blocking = [w for w in warnings if "[BLOCKING]" in w]

    if blocking:
        print(f"❌ SSOT 配置漂移: {len(blocking)} 个 BLOCKING 差异")
        for w in blocking:
            print(f"  {w}")
        print("\n请同步修改 app/core/config_defaults.py 中的对应默认值后重新提交。")
        sys.exit(1)

    if warnings:
        print(f"⚠️ {len(warnings)} 个非阻塞警告 (不阻止提交)")
        for w in warnings:
            print(f"  {w}")

    print("✅ SSOT 配置一致性检查通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
