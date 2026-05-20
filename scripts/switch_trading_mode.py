#!/usr/bin/env python3.8
"""
FTSeek 交易模式切换工具

安全地在模拟盘和实盘之间切换配置。

用法:
    python3.8 scripts/switch_trading_mode.py                 # 显示当前模式
    python3.8 scripts/switch_trading_mode.py simulate        # 切换到模拟盘
    python3.8 scripts/switch_trading_mode.py live_conservative  # 切换到保守实盘
    python3.8 scripts/switch_trading_mode.py live_normal     # 切换到正常实盘
"""
import os
import sys
import re
import shutil
from datetime import datetime
from pathlib import Path

# 确保可以 import app 模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_PATH = PROJECT_ROOT / "config" / "unified_unified_config.yaml"
BACKUP_DIR = PROJECT_ROOT / "config" / "backups"

# ── ANSI 颜色 ─────────────────────────────────────────────────────────
class C:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    END = '\033[0m'


# ── 预设配置 ──────────────────────────────────────────────────────────
# 每个预设定义了需要在 unified_unified_config.yaml 中修改的 key-value 对
# key 使用 YAML 层级路径表示

PRESETS = {
    'simulate': {
        'description': '模拟盘 (默认安全模式)',
        'risk_level': 'LOW',
        'changes': {
            'futu.trade_env': '1',
            'futu.force_simulate': 'true',
            'futu.allow_real_trading': 'false',
            'trading.mode': 'paper',
            'risk_limits.max_single_position': '0.20',
            'risk_limits.max_drawdown': '0.12',
            'risk_limits.max_daily_trades': '10',
        },
    },
    'live_conservative': {
        'description': '实盘保守 (首月推荐)',
        'risk_level': 'MEDIUM',
        'changes': {
            'futu.trade_env': '0',
            'futu.force_simulate': 'false',
            'futu.allow_real_trading': 'true',
            'trading.mode': 'real',
            'risk_limits.max_single_position': '0.05',
            'risk_limits.max_drawdown': '0.05',
            'risk_limits.max_daily_trades': '3',
        },
    },
    'live_normal': {
        'description': '实盘正常 (权重收敛后)',
        'risk_level': 'HIGH',
        'changes': {
            'futu.trade_env': '0',
            'futu.force_simulate': 'false',
            'futu.allow_real_trading': 'true',
            'trading.mode': 'real',
            'risk_limits.max_single_position': '0.15',
            'risk_limits.max_drawdown': '0.08',
            'risk_limits.max_daily_trades': '8',
        },
    },
}

# YAML key 到正则匹配的映射
# 这些正则用于在 unified_unified_config.yaml 中定位并替换特定行
KEY_PATTERNS = {
    'futu.trade_env': (r'(\s*trade_env:\s*)\S+', r'\g<1>{value}'),
    'futu.force_simulate': (r'(\s*force_simulate:\s*)\S+', r'\g<1>{value}'),
    'futu.allow_real_trading': (r'(\s*allow_real_trading:\s*)\S+', r'\g<1>{value}'),
    'trading.mode': (r'(\s*mode:\s*)(paper|real|"paper"|"real")', r'\g<1>{value}'),
    'risk_limits.max_single_position': (r'(\s*max_single_position:\s*)\S+', r'\g<1>{value}'),
    'risk_limits.max_drawdown': (r'(\s*max_drawdown:\s*)\S+', r'\g<1>{value}'),
    'risk_limits.max_daily_trades': (r'(\s*max_daily_trades:\s*)\S+', r'\g<1>{value}'),
}


def detect_current_mode() -> str:
    """检测当前配置模式"""
    content = CONFIG_PATH.read_text(encoding='utf-8')

    # 检查 trade_env
    m = re.search(r'trade_env:\s*(\d+)', content)
    trade_env = int(m.group(1)) if m else 1

    # 检查 force_simulate
    m = re.search(r'force_simulate:\s*(true|false)', content, re.IGNORECASE)
    force_sim = m.group(1).lower() == 'true' if m else True

    if trade_env == 1 or force_sim:
        return 'simulate'

    # 检查 max_single_position 区分 conservative vs normal
    m = re.search(r'max_single_position:\s*([\d.]+)', content)
    max_pos = float(m.group(1)) if m else 0.20
    if max_pos <= 0.05:
        return 'live_conservative'
    return 'live_normal'


def show_status():
    """显示当前模式状态"""
    current = detect_current_mode()
    preset = PRESETS[current]

    print(f"\n{C.BOLD}当前交易模式: {C.CYAN}{current}{C.END}")
    print(f"描述: {preset['description']}")
    print(f"风险等级: {preset['risk_level']}")
    print(f"\n可用模式:")
    for name, p in PRESETS.items():
        marker = f" {C.GREEN}<-- 当前{C.END}" if name == current else ""
        print(f"  {name:25s} {p['description']}{marker}")
    print()


def backup_config() -> str:
    """备份当前配置"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"config_{timestamp}.yaml"
    shutil.copy2(CONFIG_PATH, backup_path)
    return str(backup_path)


def apply_preset(preset_name: str):
    """应用预设配置"""
    if preset_name not in PRESETS:
        print(f"{C.RED}未知预设: {preset_name}{C.END}")
        print(f"可用: {', '.join(PRESETS.keys())}")
        sys.exit(1)

    preset = PRESETS[preset_name]
    current = detect_current_mode()

    print(f"\n{C.BOLD}切换交易模式{C.END}")
    print(f"  当前: {C.YELLOW}{current}{C.END} ({PRESETS[current]['description']})")
    print(f"  目标: {C.CYAN}{preset_name}{C.END} ({preset['description']})")
    print(f"  风险等级: {preset['risk_level']}")

    # 如果切换到实盘，执行就绪检查
    if preset_name.startswith('live_'):
        print(f"\n{C.BOLD}--- 实盘准入检查 ---{C.END}")
        try:
            import sqlite3
            db_path = PROJECT_ROOT / "data" / "ftseek.db"
            if not db_path.exists():
                print(f"  {C.RED}数据库不存在，无法进行就绪检查{C.END}")
                sys.exit(1)

            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()

            # 基础检查: 交易笔数
            c.execute(
                "SELECT COUNT(*) FROM positions WHERE status='CLOSED' "
                "AND (is_add IS NULL OR is_add = 0)")
            total_trades = c.fetchone()[0] or 0

            # 胜率 (近30笔)
            c.execute(
                "SELECT realized_pnl FROM positions WHERE status='CLOSED' "
                "AND (is_add IS NULL OR is_add = 0) "
                "ORDER BY exit_time DESC LIMIT 30")
            recent = [r[0] or 0 for r in c.fetchall()]
            win_rate = sum(1 for p in recent if p > 0) / max(1, len(recent))

            conn.close()

            trades_ok = total_trades >= 100
            wr_ok = win_rate >= 0.48

            print(f"  {'PASS' if trades_ok else 'FAIL'}: "
                  f"交易笔数 {total_trades} (要求 >= 100)")
            print(f"  {'PASS' if wr_ok else 'FAIL'}: "
                  f"近30笔胜率 {win_rate:.1%} (要求 >= 48%)")

            if not (trades_ok and wr_ok):
                print(f"\n  {C.RED}准入检查未通过！建议继续模拟盘。{C.END}")
                print(f"  运行 python3.8 scripts/track_simulate_metrics.py check 查看完整报告")
                answer = input(f"\n  {C.YELLOW}强制切换？(输入 YES 确认): {C.END}")
                if answer != 'YES':
                    print("已取消")
                    sys.exit(0)
        except ImportError:
            print(f"  {C.YELLOW}无法执行就绪检查 (sqlite3 不可用)，请手动确认{C.END}")
        except Exception as e:
            print(f"  {C.YELLOW}就绪检查异常: {e}，请手动确认{C.END}")

        # 实盘确认
        if current == 'simulate':
            print(f"\n{C.RED}{C.BOLD}注意: 您正在从模拟盘切换到实盘！{C.END}")
            print(f"实盘交易将使用真实资金，请确认:")
            print(f"  1. 富途 OpenD 已启动并登录实盘账户")
            print(f"  2. 账户资金充足")
            print(f"  3. 风控参数已确认")
            answer = input(f"\n{C.YELLOW}确认切换到实盘？(输入 CONFIRM): {C.END}")
            if answer != 'CONFIRM':
                print("已取消")
                sys.exit(0)

    # 备份
    backup = backup_config()
    print(f"\n配置已备份: {backup}")

    # 应用变更
    content = CONFIG_PATH.read_text(encoding='utf-8')
    changes_applied = 0

    for key, value in preset['changes'].items():
        if key in KEY_PATTERNS:
            pattern, replacement = KEY_PATTERNS[key]
            replacement_str = replacement.replace('{value}', value)
            new_content, count = re.subn(pattern, replacement_str, content, count=1)
            if count > 0:
                content = new_content
                changes_applied += 1
                print(f"  {C.GREEN}SET{C.END} {key} = {value}")
            else:
                print(f"  {C.YELLOW}SKIP{C.END} {key} (未找到匹配)")

    CONFIG_PATH.write_text(content, encoding='utf-8')
    print(f"\n{C.GREEN}{C.BOLD}已成功切换到 [{preset_name}] 模式{C.END}")
    print(f"应用了 {changes_applied} 项配置变更")

    if preset_name.startswith('live_'):
        print(f"\n{C.YELLOW}重要: 请重启 FTSeek 服务使配置生效:{C.END}")
        print(f"  cd /opt/ftseek/FTSeek && bash stop.sh && bash start.sh")


# ── 入口 ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if not CONFIG_PATH.exists():
        print(f"{C.RED}配置文件不存在: {CONFIG_PATH}{C.END}")
        sys.exit(1)

    if len(sys.argv) < 2:
        show_status()
    else:
        target = sys.argv[1]
        if target in ('status', 'show'):
            show_status()
        else:
            apply_preset(target)
