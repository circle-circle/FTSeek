#!/usr/bin/env python3.8
"""
开盘前功能验证脚本
每日开盘前自动执行检查
"""
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

print("=" * 70)
print("🔍 FTSeek 开盘前功能验证")
print("=" * 70)
print(f"📅 检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# 检查结果
checks = {"passed": 0, "failed": 0, "items": []}

def check(name, success, msg=""):
    checks["items"].append({"name": name, "success": success, "msg": msg})
    if success:
        checks["passed"] += 1
        print(f"✅ {name}")
    else:
        checks["failed"] += 1
        print(f"❌ {name}: {msg}")

# ============================================================
# 1. 系统服务检查
# ============================================================
print("-" * 70)
print("🖥️ 1. 系统服务检查")
print("-" * 70)

# 检查 OpenD
import subprocess
result = subprocess.run(['systemctl', 'is-active', 'opend'], capture_output=True, text=True)
check("OpenD 服务", result.stdout.strip() == 'active', result.stdout.strip())

# 检查回调服务器
try:
    resp = requests.get('http://localhost:8901/health', timeout=5)
    check("回调服务器", resp.status_code == 200, resp.json().get('status'))
except Exception as e:
    check("回调服务器", False, str(e))

# 检查端口
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
result = sock.connect_ex(('127.0.0.1', 11111))
check("OpenD 端口 (11111)", result == 0, "正常" if result == 0 else "无法连接")
sock.close()

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
result = sock.connect_ex(('127.0.0.1', 8901))
check("回调端口 (8901)", result == 0, "正常" if result == 0 else "无法连接")
sock.close()

# ============================================================
# 2. 富途 SDK 检查
# ============================================================
print()
print("-" * 70)
print("📊 2. 富途 SDK 检查")
print("-" * 70)

try:
    from futu import OpenQuoteContext, OpenHKTradeContext
    check("SDK 导入", True)
    
    # 行情连接
    try:
        quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        check("行情连接", True)
        
        # 测试获取报价
        ret, data = quote_ctx.get_market_snapshot('HK.00700')
        if ret == 0 and len(data) > 0:
            check("股票报价", True, f"腾讯：{data['last_price'].iloc[0]:.2f}港元")
        else:
            check("股票报价", False, str(data))
        
        quote_ctx.close()
    except Exception as e:
        check("行情连接", False, str(e))
    
    # 交易连接
    try:
        trade_ctx = OpenHKTradeContext(host='127.0.0.1', port=11111)
        check("交易连接", True)
        
        # 解锁交易 (模拟盘无密码)
        ret, msg = trade_ctx.unlock_trade(password='')
        if ret == 0:
            check("交易解锁", True, "模拟盘")
            
            # 查询持仓
            ret, data = trade_ctx.get_position_list()
            if ret == 0:
                check("持仓查询", True, f"{len(data)}个持仓" if len(data) > 0 else "无持仓")
            else:
                check("持仓查询", False, str(data))
            
            # 查询资金
            ret, data = trade_ctx.get_funds()
            if ret == 0:
                check("资金查询", True)
            else:
                check("资金查询", False, str(data))
        else:
            check("交易解锁", False, msg)
        
        trade_ctx.close()
    except Exception as e:
        check("交易连接", False, str(e))
        
except Exception as e:
    check("SDK 导入", False, str(e))

# ============================================================
# 3. 交易环境检查
# ============================================================
print()
print("-" * 70)
print("🔒 3. 交易环境检查")
print("-" * 70)

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / '.env')

# 检查模拟盘保护
force_simulate = os.getenv('FORCE_SIMULATE_MODE', 'true').lower() == 'true'
real_locked = os.getenv('REAL_TRADING_LOCKED', 'true').lower() == 'true'

check("强制模拟盘", force_simulate, "已启用" if force_simulate else "未启用")
check("实盘锁定", real_locked, "已锁定" if real_locked else "未锁定")

trade_env = os.getenv('TRADE_ENV', '1')
check("交易环境", trade_env == '1', "模拟盘" if trade_env == '1' else "实盘")

# ============================================================
# 4. 推送功能检查
# ============================================================
print()
print("-" * 70)
print("📱 4. 推送功能检查")
print("-" * 70)

webhook_url = os.getenv('DINGTALK_WEBHOOK_URL', '')
secret = os.getenv('DINGTALK_SECRET', '')

check("钉钉 Webhook", bool(webhook_url), "已配置" if webhook_url else "未配置")
check("钉钉 Secret", bool(secret), "已配置" if secret else "未配置")

# 测试推送
if webhook_url and secret:
    try:
        from app.v4.notifier.dingtalk_v4 import DingTalkNotifierV4
        notifier = DingTalkNotifierV4(webhook_url=webhook_url, secret=secret)
        
        test_msg = f"🧪 FTSeek 开盘前测试推送\n\n时间：{datetime.now().strftime('%H:%M:%S')}\n状态：系统正常"
        result = notifier.send(test_msg, msg_type='markdown')
        
        check("推送测试", result.get('success', False), "成功" if result.get('success') else "失败")
    except Exception as e:
        check("推送测试", False, str(e))

# ============================================================
# 5. 交易时间检查
# ============================================================
print()
print("-" * 70)
print("⏰ 5. 交易时间检查")
print("-" * 70)

now = datetime.now()
print(f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"星期：{now.strftime('%A')}")

hour = now.hour
minute = now.minute
time_now = hour * 60 + minute
is_weekday = now.weekday() < 5

print("\n📊 港股交易时间:")
print("  早盘：09:30 - 12:00")
print("  午盘：13:00 - 16:00")

if is_weekday:
    if 570 <= time_now < 720:
        print("  状态：✅ 早盘交易中")
        check("交易时间", True, "早盘交易中")
    elif 780 <= time_now < 960:
        print("  状态：✅ 午盘交易中")
        check("交易时间", True, "午盘交易中")
    else:
        print("  状态：⏸️ 休市")
        check("交易时间", True, "休市但可提前准备")
else:
    print("  状态：⏸️ 周末休市")
    check("交易时间", True, "周末休市")

# ============================================================
# 6. 总结报告
# ============================================================
print()
print("=" * 70)
print("📊 检查总结")
print("=" * 70)

total = checks["passed"] + checks["failed"]
print(f"总检查项：{total}")
print(f"✅ 通过：{checks['passed']}")
print(f"❌ 失败：{checks['failed']}")
print(f"通过率：{checks['passed']/total*100:.1f}%")

if checks["failed"] == 0:
    print("\n🎉 所有检查通过！系统已就绪！")
    print("\n✅ 明日开盘可以直接操作！")
else:
    print("\n⚠️ 部分检查失败，请修复后再开盘！")
    print("\n失败项目:")
    for item in checks["items"]:
        if not item["success"]:
            print(f"  ❌ {item['name']}: {item['msg']}")

print()
print("=" * 70)

# 保存检查结果
result_file = Path(__file__).parent.parent / 'data' / 'pre_market_check.json'
result_file.parent.mkdir(exist_ok=True)

import json
with open(result_file, 'w', encoding='utf-8') as f:
    json.dump({
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "passed": checks["passed"],
        "failed": checks["failed"],
        "items": checks["items"]
    }, f, indent=2, ensure_ascii=False)

print(f"📝 检查结果已保存：{result_file}")
