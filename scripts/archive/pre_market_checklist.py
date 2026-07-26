#!/usr/bin/env python3
"""
周一开盘前检查清单 (2026-06-02)

检查项:
1. 系统健康状态
2. 当前持仓风险评估
3. VIX 和市场状态
4. RFC-055 配置验证
5. ORB 引擎状态
6. 信号管道就绪检查
"""

import sqlite3
import json
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("data/ftseek_us.db")

def check_system_health():
    """检查系统健康状态"""
    print("\n" + "="*70)
    print("1. 系统健康状态")
    print("="*70)

    try:
        result = subprocess.run(
            ["systemctl", "status", "ftseek-us", "--no-pager"],
            capture_output=True, text=True, timeout=5
        )
        if "active (running)" in result.stdout:
            print("✅ 服务运行正常")
            # 提取运行时间
            for line in result.stdout.split('\n'):
                if "Active:" in line:
                    print(f"   {line.strip()}")
                    break
        else:
            print("❌ 服务未运行")
            return False
    except Exception as e:
        print(f"❌ 检查失败: {e}")
        return False

    # 健康端点检查
    try:
        import urllib.request
        response = urllib.request.urlopen("http://localhost:8899/health", timeout=5)
        data = json.loads(response.read())
        if data.get("status") == "ok":
            print(f"✅ 健康检查通过 (timestamp: {data.get('timestamp')})")
        else:
            print(f"⚠️  健康检查异常: {data}")
    except Exception as e:
        print(f"❌ 健康端点检查失败: {e}")
        return False

    return True

def check_current_positions():
    """检查当前持仓风险"""
    print("\n" + "="*70)
    print("2. 当前持仓风险评估")
    print("="*70)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute("""
        SELECT symbol, direction, size, avg_entry_price,
               ROUND((avg_entry_price * size), 2) as position_value,
               trace_id, strategy_id
        FROM open_positions
        WHERE (sync_status IS NULL OR sync_status NOT IN
            ('closed_by_daemon', 'closed_by_scheduler', 'closed_by_sync',
             'closed_emergency', 'stale_not_in_broker'))
    """)

    positions = cursor.fetchall()
    conn.close()

    if not positions:
        print("✅ 无当前持仓")
        return True

    print(f"当前持仓数: {len(positions)}")
    print(f"{'Symbol':<10} {'Direction':<10} {'Size':<8} {'Entry':<12} {'Value':<15} {'Trace ID':<25}")
    print("-" * 90)

    total_value = 0
    for pos in positions:
        symbol, direction, size, entry, value, trace_id, strategy = pos
        total_value += value
        print(f"{symbol:<10} {direction:<10} {size:<8.1f} ${entry:<11.2f} ${value:<14.2f} {trace_id or 'N/A':<25}")

    print("-" * 90)
    print(f"总持仓价值: ${total_value:,.2f}")

    # 风险评估
    if len(positions) > 5:
        print(f"⚠️  持仓数量过多 ({len(positions)} > 5)")

    # 检查是否都是 SHORT（符合 RFC-055 优化）
    short_count = sum(1 for p in positions if p[1] == 'SHORT')
    if short_count == len(positions):
        print(f"✅ 所有持仓都是 SHORT（符合 RFC-055 SHORT-first 策略）")
    else:
        print(f"⚠️  有 {len(positions) - short_count} 个非 SHORT 持仓")

    return True

def check_market_conditions():
    """检查市场状态"""
    print("\n" + "="*70)
    print("3. 市场状态")
    print("="*70)

    # 检查 VIX
    try:
        with open("logs/ftseek_us.log", "r") as f:
            lines = f.readlines()
            for line in reversed(lines[-100:]):
                if "VIX" in line and "指数" in line:
                    print(f"最近 VIX: {line.strip()}")
                    # 提取 VIX 值
                    if "15." in line or "16." in line or "17." in line:
                        print("✅ VIX < 22（允许交易）")
                    elif "22." in line or "23." in line or "24." in line:
                        print("⚠️  VIX 接近 22 阈值")
                    else:
                        print("⚠️  VIX 可能 >= 22（阻止交易）")
                    break
    except Exception as e:
        print(f"⚠️  无法读取 VIX: {e}")

    # 检查是否是交易日
    today = datetime.now()
    weekday = today.weekday()
    if weekday >= 5:
        print(f"⚠️  今天是周末（{today.strftime('%A')}），市场休市")
    else:
        print(f"✅ 今天是交易日（{today.strftime('%A')}）")

    return True

def check_rfc055_config():
    """验证 RFC-055 配置"""
    print("\n" + "="*70)
    print("4. RFC-055 配置验证")
    print("="*70)

    try:
        with open("config/unified_config.yaml", "r") as f:
            content = f.read()

        checks = [
            ("LONG 最小信心 0.90", "LONG:" in content and "0.90" in content),
            ("SHORT 最小信心 0.55", "SHORT:" in content and "0.55" in content),
            ("VIX 门禁 22", "vix_block_threshold: 22" in content or "max_vix_threshold: 22" in content),
            ("黑名单扩充", "US.LMT" in content and "US.UNH" in content),
        ]

        all_passed = True
        for name, passed in checks:
            status = "✅" if passed else "❌"
            print(f"{status} {name}")
            if not passed:
                all_passed = False

        return all_passed
    except Exception as e:
        print(f"❌ 配置检查失败: {e}")
        return False

def check_orb_status():
    """检查 ORB 引擎状态"""
    print("\n" + "="*70)
    print("5. ORB 辅线状态")
    print("="*70)

    try:
        with open("logs/ftseek_us.log", "r") as f:
            lines = f.readlines()
            orb_lines = [l for l in lines if "[ORB]" in l]

        if not orb_lines:
            print("⚠️  未找到 ORB 日志")
            return False

        # 检查最近的 ORB 日志
        last_orb = orb_lines[-1]
        print(f"最近 ORB 日志: {last_orb.strip()}")

        if "mode=observe" in last_orb:
            print("✅ ORB 运行在 observe 模式")
        elif "mode=shadow" in last_orb:
            print("✅ ORB 运行在 shadow 模式")
        elif "mode=active" in last_orb:
            print("✅ ORB 运行在 active 模式")
        else:
            print("⚠️  ORB 模式未知")

        # 统计 ORB 日志数量
        print(f"ORB 日志总数: {len(orb_lines)}")

        return True
    except Exception as e:
        print(f"❌ ORB 检查失败: {e}")
        return False

def check_signal_pipeline():
    """检查信号管道"""
    print("\n" + "="*70)
    print("6. 信号管道就绪检查")
    print("="*70)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 检查最近的信号
    cursor.execute("""
        SELECT COUNT(*) as total,
               MAX(created_at) as last_signal
        FROM trade_attribution
        WHERE created_at >= datetime('now', '-7 days')
    """)

    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        total, last_signal = row
        print(f"最近 7 天信号数: {total}")
        print(f"最后信号时间: {last_signal}")

        if total > 0:
            print("✅ 信号管道正常")
            return True
        else:
            print("⚠️  最近 7 天无信号")
            return False
    else:
        print("❌ 无法查询信号数据")
        return False

def main():
    print("╔" + "="*68 + "╗")
    print("║" + " "*15 + "周一开盘前检查清单" + " "*34 + "║")
    print("╚" + "="*68 + "╝")
    print(f"\n检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    results = {
        "系统健康": check_system_health(),
        "持仓风险": check_current_positions(),
        "市场状态": check_market_conditions(),
        "RFC-055 配置": check_rfc055_config(),
        "ORB 状态": check_orb_status(),
        "信号管道": check_signal_pipeline(),
    }

    print("\n" + "="*70)
    print("检查结果汇总")
    print("="*70)

    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{status} - {name}")

    all_passed = all(results.values())

    print("\n" + "="*70)
    if all_passed:
        print("✅ 所有检查通过，系统已准备好迎接周一开盘")
    else:
        print("⚠️  部分检查失败，请检查上述问题")
    print("="*70)

if __name__ == "__main__":
    main()
