#!/usr/bin/env python3
"""
交易模式切换脚本 - 安全切换 模拟盘/实盘(保守)/实盘(正常)

使用:
    python scripts/switch_trading_mode.py simulate
    python scripts/switch_trading_mode.py live_conservative
    python scripts/switch_trading_mode.py live_normal           # 需先通过就绪检查
    python scripts/switch_trading_mode.py live_normal --force   # 跳过就绪检查（不推荐）
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------- #
# 模式预设配置
# ---------------------------------------------------------------------- #
MODE_PRESETS: Dict[str, Dict] = {
    "simulate": {
        "TRADE_ENV": "1",
        "FUTU_TRADE_ENV": "1",
        "LIVE_CONSERVATIVE": "0",
        "description": "模拟盘 - 全功能验证，无真实资金",
        "risk_params": {
            "max_position_pct": 15.0,
            "max_daily_loss_pct": 5.0,
            "max_total_exposure_pct": 80.0,
            "circuit_breaker_loss_pct": 8.0,
        },
    },
    "live_conservative": {
        "TRADE_ENV": "0",
        "FUTU_TRADE_ENV": "0",
        "LIVE_CONSERVATIVE": "1",
        "description": "实盘保守模式 - 仓位减半，风控收紧",
        "risk_params": {
            "max_position_pct": 8.0,
            "max_daily_loss_pct": 2.0,
            "max_total_exposure_pct": 40.0,
            "circuit_breaker_loss_pct": 3.0,
        },
    },
    "live_normal": {
        "TRADE_ENV": "0",
        "FUTU_TRADE_ENV": "0",
        "LIVE_CONSERVATIVE": "0",
        "description": "实盘正常模式 - 标准参数",
        "risk_params": {
            "max_position_pct": 12.0,
            "max_daily_loss_pct": 3.0,
            "max_total_exposure_pct": 60.0,
            "circuit_breaker_loss_pct": 5.0,
        },
    },
}


# ---------------------------------------------------------------------- #
# .env 操作
# ---------------------------------------------------------------------- #
def backup_env_file(env_path: Path) -> Path | None:
    """备份当前 .env 文件，返回备份路径"""
    if not env_path.exists():
        return None
    backup_name = f".env.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_path = env_path.parent / backup_name
    shutil.copy2(env_path, backup_path)
    print(f"  备份: {backup_path}")
    return backup_path


def update_env_file(env_path: Path, updates: Dict[str, str]) -> bool:
    """更新 .env 文件中的指定变量；不存在的键将追加到文件末尾"""
    if not env_path.exists():
        print(f"  ❌ .env 文件不存在: {env_path}")
        return False

    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    updated_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------- #
# 就绪检查
# ---------------------------------------------------------------------- #
def run_readiness_check() -> bool:
    """运行实盘就绪检查"""
    from scripts.live_readiness_check import LiveReadinessChecker

    checker = LiveReadinessChecker()
    return checker.print_report()


# ---------------------------------------------------------------------- #
# 切换主流程
# ---------------------------------------------------------------------- #
def switch_mode(target_mode: str, force: bool = False) -> bool:
    if target_mode not in MODE_PRESETS:
        print(f"❌ 未知模式: {target_mode}")
        print(f"   可用模式: {', '.join(MODE_PRESETS.keys())}")
        return False

    preset = MODE_PRESETS[target_mode]
    project_root = Path(__file__).parent.parent
    env_path = project_root / ".env"

    print("\n" + "=" * 60)
    print("  交易模式切换")
    print("=" * 60)
    print(f"  目标模式: {target_mode}")
    print(f"  说明    : {preset['description']}")
    print("-" * 60)

    # 切换到实盘前先运行就绪检查
    if target_mode.startswith("live") and not force:
        print("\n  运行实盘就绪检查...")
        is_ready = run_readiness_check()
        if not is_ready:
            print("  ❌ 就绪检查未通过，拒绝切换到实盘")
            print("  💡 使用 --force 跳过检查（不推荐）")
            return False

    # 备份当前配置
    print("\n  步骤 1: 备份当前配置")
    backup_env_file(env_path)

    # 更新 .env
    print("  步骤 2: 更新环境变量")
    env_updates = {
        "TRADE_ENV": preset["TRADE_ENV"],
        "FUTU_TRADE_ENV": preset["FUTU_TRADE_ENV"],
        "LIVE_CONSERVATIVE": preset["LIVE_CONSERVATIVE"],
    }
    if not update_env_file(env_path, env_updates):
        return False
    for k, v in env_updates.items():
        print(f"    {k}={v}")

    # 输出风控参数建议
    print("\n  步骤 3: 风控参数（建议值，对应 risk.mode_adaptation 配置段）")
    for k, v in preset["risk_params"].items():
        print(f"    {k}: {v}")

    print("\n" + "-" * 60)
    print(f"  ✅ 模式切换完成: {target_mode}")
    print("  ⚠️  请重启服务使配置生效:  ./stop.sh && ./start.sh")
    print("=" * 60 + "\n")
    return True


def _print_usage() -> None:
    print(
        "使用方法: python scripts/switch_trading_mode.py "
        "[simulate|live_conservative|live_normal] [--force]"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _print_usage()
        sys.exit(1)

    target = sys.argv[1]
    force = "--force" in sys.argv

    if target in ("-h", "--help"):
        _print_usage()
        sys.exit(0)

    success = switch_mode(target, force=force)
    sys.exit(0 if success else 1)
