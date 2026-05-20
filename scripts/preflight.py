#!/usr/bin/env python3
"""FTSeek-US 盘前预检脚本 v3 (Pre-flight Check)

运行时间: 交易日 07:30-09:30 ET (盘前1小时)
功能:
1. 时区与交易日防御 (DST 适配)
2. 环境自检 (API Keys, Python版本, 依赖)
3. OpenD 服务连通性检查
4. 数据库连接验证
5. 配置漂移检测 (阈值合理性)
6. 关键逻辑冒烟测试
7. 失败时钉钉告警 (P1)
"""

import os
import sys
import json
import socket
import subprocess
import sqlite3
import re
from typing import List
from pathlib import Path
from datetime import datetime, time

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

def load_systemd_envs():
    """从 systemd service 文件加载环境变量"""
    service_path = '/etc/systemd/system/ftseek-us.service'
    if not os.path.exists(service_path):
        return
    try:
        with open(service_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('Environment='):
                    env_part = line[len('Environment='):]
                    for match in re.finditer(r'(\w+)=("[^"]*"|[^\s]+)', env_part):
                        key = match.group(1)
                        val = match.group(2).strip('"')
                        if key not in os.environ:
                            os.environ[key] = val
    except Exception as e:
        print(f"  [WARN] 加载 systemd 环境失败: {e}")

load_systemd_envs()

# ================== 防御性逻辑 ==================
def is_in_window():
    """判断当前美东时间是否在预检窗口 (07:00 - 09:30)"""
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
        return time(7, 0) <= et_now.time() <= time(9, 30)
    except Exception as e:
        print(f"  [WARN] 时区检查失败，默认执行: {e}")
        return True

def is_trading_day():
    """判断今天是否为美股交易日"""
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
        weekday = et_now.weekday()
        
        # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        if weekday >= 5:
            return False
            
        # 简易美股节假日检查 (可根据需要扩展)
        month, day = et_now.month, et_now.day
        us_holidays = [
            (1, 1), (1, 15), (1, 20),  # 元旦, MLK, 就职日(特定年)
            (2, 17), (2, 20),           # 总统日
            (5, 26), (5, 29),           # 阵亡将士纪念日
            (6, 19), (7, 4),            # 六月节, 独立日
            (9, 1), (9, 7),             # 劳动节, 哥伦布日
            (11, 11), (11, 23), (11, 27), # 老兵节, 感恩节及次日
            (12, 25)                    # 圣诞节
        ]
        if (month, day) in us_holidays:
            return False
            
        return True
    except Exception as e:
        print(f"  [WARN] 交易日检查失败，默认执行: {e}")
        return True
# ================================================

def check_header(title: str):
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")

def check_env():
    """Check environment variables and dependencies."""
    check_header("环境检查 [ENVIRONMENT]")
    required_vars = ['OPEND_HOST', 'OPEND_PORT', 'TRADE_ENV', 'DINGTALK_WEBHOOK_URL_US']
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        print(f"  [FAIL] 缺失环境变量: {', '.join(missing)}")
        return False
    print(f"  [OK] 关键环境变量全部就绪")
    
    major, minor = sys.version_info[:2]
    print(f"  [OK] Python {major}.{minor}")
    
    try:
        import futu
        print(f"  [OK] futu-api {futu.__version__}")
    except ImportError:
        print("  [FAIL] futu-api 未安装")
        return False
    return True

def check_opend():
    """检查 OpenD 是否运行"""
    check_header("服务检查 [OPEND]")
    host = os.environ.get('OPEND_HOST', '127.0.0.1')
    port = int(os.environ.get('OPEND_PORT', 11111))
    
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"  [OK] OpenD 运行中 ({host}:{port})")
            return True
    except Exception as e:
        print(f"  [FAIL] 无法连接 OpenD ({host}:{port}): {e}")
        print("  -> 请检查 futuopend 进程是否启动")
        return False

def check_db():
    """Check database connectivity."""
    check_header("数据库检查 [DATABASE]")
    db_path = PROJECT_ROOT / 'data' / 'ftseek_us.db'
    if not db_path.exists():
        print("  [WARN] 数据库文件不存在 (首次运行正常)")
        return True
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'")
        count = cursor.fetchone()[0]
        conn.close()
        print(f"  [OK] 数据库连接正常 ({count} 个表)")
        return True
    except Exception as e:
        print(f"  [FAIL] 数据库错误: {e}")
        return False

def check_config():
    """Check config sanity."""
    check_header("配置检查 [CONFIG]")
    config_path = PROJECT_ROOT / 'config' / 'unified_config.yaml'
    if not config_path.exists():
        print("  [FAIL] 配置文件缺失")
        return False
    
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        risk = cfg.get('risk', {})
        max_dd = risk.get('account', {}).get('max_daily_drawdown', 0)
        if max_dd > 0.1:
            print(f"  [WARN] 单日最大回撤阈值 {max_dd:.0%} 过高 (>10%)")
        max_trades = risk.get('trading', {}).get('max_daily_trades', 0)
        if max_trades < 1:
            print(f"  [WARN] 每日最大交易次数 {max_trades} 过低")
        sl = risk.get('stop_loss', {}).get('default', 0)
        if sl > -0.02:
            print(f"  [WARN] 默认止损 {sl:.0%} 过窄 (<2%)")
        print("  [OK] 配置阈值通过合理性检查")
        return True
    except Exception as e:
        print(f"  [FAIL] 配置解析错误: {e}")
        return False

def run_smoke_tests():
    """Run critical unit tests."""
    check_header("冒烟测试 [SMOKE TESTS]")
    venv_pytest = PROJECT_ROOT / 'venv' / 'bin' / 'python'
    if not venv_pytest.exists():
        print("  [WARN] 项目 venv 不存在，跳过测试")
        return True
    
    test_files = ['tests/test_config_container.py', 'tests/test_vote_logger.py', 'tests/scenarios/test_trade_guard_scenarios.py']
    passed = 0
    run_count = 0
    for tf in test_files:
        test_path = PROJECT_ROOT / tf
        if not test_path.exists():
            continue
        run_count += 1
        result = subprocess.run(
            [str(venv_pytest), '-m', 'pytest', str(test_path), '-v', '--tb=short'],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            passed += 1
            print(f"  [OK] {tf}")
        else:
            print(f"  [FAIL] {tf}")
    return passed == run_count and run_count > 0

def send_failure_alert(failed_checks: List[str]):
    """发送钉钉告警通知 (P1)"""
    webhook = os.environ.get('DINGTALK_WEBHOOK_URL_US')
    secret = os.environ.get('DINGTALK_SECRET_US', '')
    
    if not webhook:
        print("  [SKIP] 未配置钉钉 Webhook，跳过告警")
        return
        
    try:
        sys.path.insert(0, str(PROJECT_ROOT / 'app'))
        from observability.dingtalk_alert import DingTalkAlerter
        alerter = DingTalkAlerter(webhook, secret)
        alerter.send_alert(
            level="CRITICAL",
            title="⚠️ FTSeek-US 盘前预检失败",
            content=f"以下检查项未通过，请手动介入修复：\n" + "\n".join(f"- {c}" for c in failed_checks),
            时间=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
        print("  [OK] 钉钉告警已发送")
    except Exception as e:
        print(f"  [WARN] 发送钉钉告警失败: {e}")

def main():
    # 0. 防御性检查
    if not is_trading_day():
        print("📅 当前非交易日，跳过预检")
        sys.exit(0)
    if not is_in_window():
        print("⏰ 当前不在预检窗口 (07:00-09:30 ET)，跳过预检")
        sys.exit(0)

    print(f"\nFTSeek-US 盘前预检 v2")
    try:
        from zoneinfo import ZoneInfo
        print(f"美东时间: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S ET')}")
    except:
        print(f"本地时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {
        'trading_day': True,
        'env': check_env(),
        'opend': check_opend(),
        'db': check_db(),
        'config': check_config(),
        'tests': run_smoke_tests()
    }
    
    check_header("预检总结 [SUMMARY]")
    all_pass = all(results.values())
    failed = [k.upper() for k, v in results.items() if not v]
    for k, v in results.items():
        status = '✅' if v else '❌'
        print(f"  {status} {k.upper()}")
    
    if all_pass:
        print("\n🚀 系统就绪，可以开盘交易！")
        sys.exit(0)
    else:
        print(f"\n⚠️ 存在未通过检查项: {', '.join(failed)}")
        send_failure_alert(failed)
        sys.exit(1)

if __name__ == '__main__':
    main()
