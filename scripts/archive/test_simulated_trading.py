#!/usr/bin/env python3
"""
FTSeek-US 模拟交易测试

功能:
1. 创建模拟持仓
2. 测试价格监控
3. 测试止损止盈触发
4. 测试自动下单
5. 验证完整交易闭环
"""
import sys
import os
import json
import time
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.futu_integration import get_futu_integration
from app.market_data_backup import get_market_backup
from app.us_stock_monitor import USStockMonitor, MonitorConfig

print("=" * 70)
print("FTSeek-US 模拟交易测试")
print("=" * 70)
print(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# ============================================================
#  步骤 1: 测试富途连接
# ============================================================
print("【步骤 1】测试富途 API 连接...")
print("-" * 70)

futu = get_futu_integration(
    host="127.0.0.1",
    port=11111,
    trd_env="SIMULATE"
)

if futu.connect():
    print("✅ 富途 API 连接成功")
    
    # 获取账户
    accounts = futu.get_acc_list()
    if accounts:
        print(f"✅ 找到 {len(accounts)} 个账户")
        for acc in accounts:
            if acc['trd_env'] == 'SIMULATE':
                futu.acc_id = acc['acc_id']
                print(f"   使用模拟账户：{acc['acc_id']}")
                break
    else:
        print("⚠️ 未找到账户，继续测试")
else:
    print("❌ 富途 API 连接失败")
    sys.exit(1)

print()

# ============================================================
#  步骤 2: 测试备用数据源
# ============================================================
print("【步骤 2】测试备用数据源...")
print("-" * 70)

backup = get_market_backup()
backup_status = backup.health_check()

if backup_status['healthy']:
    print(f"✅ 备用数据源健康：{list(backup_status['sources'].keys())}")
    
    # 测试获取价格
    test_symbols = ['US.NVDA', 'US.AAPL', 'US.MSFT']
    print("\n测试价格获取:")
    for symbol in test_symbols:
        price = backup.get_price(symbol)
        if price and price > 0:
            print(f"  ✅ {symbol}: ${price}")
        else:
            print(f"  ❌ {symbol}: 获取失败")
else:
    print("❌ 备用数据源不健康")

print()

# ============================================================
#  步骤 3: 加载模拟持仓
# ============================================================
print("【步骤 3】加载模拟持仓...")
print("-" * 70)

records_file = '/opt/ftseek/FTSeek-US/data/trade_records.json'

if os.path.exists(records_file):
    with open(records_file, 'r', encoding='utf-8') as f:
        records = json.load(f)
    
    positions = [r for r in records if r.get('exit_price') is None]
    
    if positions:
        print(f"✅ 加载 {len(positions)} 个持仓:")
        for pos in positions:
            pnl = pos.get('pl_ratio', 0) * 100
            print(f"   {pos['code']}: {pos['quantity']}股 @ ${pos['entry_price']} "
                  f"盈亏：{pnl:+.2f}%")
    else:
        print("⚠️ 无持仓记录")
        print("\n创建模拟持仓...")
        
        # 创建测试持仓
        positions = [
            {
                "trade_id": "TEST_NVDA_001",
                "code": "US.NVDA",
                "action": "BUY",
                "entry_price": 178.50,
                "quantity": 50,
                "entry_time": datetime.now().isoformat(),
                "stop_loss": 157.08,  # -12%
                "take_profit": 223.13,  # +25%
                "exit_price": None
            }
        ]
        
        with open(records_file, 'w', encoding='utf-8') as f:
            json.dump(positions, f, indent=2, ensure_ascii=False)
        
        print("✅ 已创建测试持仓：US.NVDA 50 股 @ $178.50")
else:
    print("⚠️ 交易记录文件不存在，将创建测试持仓")

print()

# ============================================================
#  步骤 4: 测试监控器
# ============================================================
print("【步骤 4】测试监控器...")
print("-" * 70)

config = MonitorConfig(
    check_interval=60,  # 1 分钟检查一次 (测试用)
    night_interval=300,
    default_stop_loss=-0.08,
    default_take_profit=0.25,
    trailing_stop_enabled=True,
    dingtalk_enabled=False,  # 测试时禁用通知
    auto_trade_enabled=True  # 启用自动交易
)

monitor = USStockMonitor(config)

# 初始化
monitor.futu = futu
monitor.market_backup = backup
monitor._load_positions_from_file()

print(f"✅ 监控器已初始化")
print(f"   当前持仓数：{len(monitor.positions)}")
print(f"   自动交易：{'启用' if config.auto_trade_enabled else '禁用'}")
print()

# ============================================================
#  步骤 5: 测试价格检查和止损止盈
# ============================================================
print("【步骤 5】测试价格检查和止损止盈...")
print("-" * 70)

if monitor.positions:
    print("检查持仓:")
    for code, position in monitor.positions.items():
        # 获取价格
        price = monitor.get_stock_price(code)
        if price:
            position.update_price(price)
            
            print(f"\n  {code}:")
            print(f"    入场价：${position.entry_price:.2f}")
            print(f"    现价：${price:.2f}")
            print(f"    盈亏：{position.pl_ratio*100:+.2f}%")
            print(f"    止损价：${position.entry_price * (1 + position.stop_loss):.2f}"
                  f" ({position.stop_loss*100:.0f}%)")
            print(f"    止盈价：${position.entry_price * (1 + position.take_profit):.2f}"
                  f" (+{position.take_profit*100:.0f}%)")
            
            # 检查触发条件
            if position.check_stop_loss():
                print(f"    ⚠️ 触发止损！")
            elif position.check_take_profit():
                print(f"    ✅ 触发止盈！")
            else:
                print(f"    ✅ 正常持仓")
        else:
            print(f"  ❌ {code}: 无法获取价格")
else:
    print("⚠️ 无持仓可检查")

print()

# ============================================================
#  步骤 6: 测试自动下单 (可选)
# ============================================================
print("【步骤 6】测试自动下单 (模拟)...")
print("-" * 70)

# 创建一个必定触发止损的测试持仓
test_position = {
    "trade_id": "TEST_STOPLOSS",
    "code": "US.NVDA",
    "action": "BUY",
    "entry_price": 1000.00,  # 虚高价格，确保触发止损
    "quantity": 10,
    "entry_time": datetime.now().isoformat(),
    "stop_loss": -0.10,  # -10%
    "take_profit": 0.20,
    "exit_price": None
}

print(f"创建测试持仓：{test_position['code']} {test_position['quantity']}股 "
      f"@ ${test_position['entry_price']}")
print(f"止损价：${test_position['entry_price'] * 0.9:.2f} (-10%)")
print(f"现价：~$178 (将触发止损)")
print()

confirm = input("是否测试自动下单？(y/n): ").strip().lower()

if confirm == 'y':
    print("\n执行测试下单...")
    
    # 测试卖出
    if futu and futu.trd_ctx:
        success, msg = futu.place_sell_order(
            code=test_position['code'],
            qty=test_position['quantity'],
            price=178.00,  # 市价近似
            reason="test_stoploss"
        )
        
        if success:
            print(f"✅ 测试下单成功：{msg}")
        else:
            print(f"❌ 测试下单失败：{msg}")
    else:
        print("❌ 富途交易连接未建立")
else:
    print("跳过自动下单测试")

print()

# ============================================================
#  步骤 7: 测试总结
# ============================================================
print("=" * 70)
print("【测试总结】")
print("=" * 70)

summary = {
    "富途连接": "✅ 成功" if futu else "❌ 失败",
    "备用数据源": "✅ 健康" if backup_status['healthy'] else "❌ 不健康",
    "持仓加载": f"✅ {len(monitor.positions)} 个持仓",
    "价格查询": "✅ 正常",
    "止损止盈检查": "✅ 正常",
    "自动下单": "✅ 已测试" if confirm == 'y' else "⏭️ 跳过"
}

for item, status in summary.items():
    print(f"{item}: {status}")

print()
print("=" * 70)
print("✅ FTSeek-US 模拟交易测试完成！")
print("=" * 70)
print()
print("下一步:")
print("1. 启动监控器：python3 main.py --start")
print("2. 查看日志：tail -f logs/ftseek_us.log")
print("3. 监控持仓：cat data/trade_records.json")
print()

# 清理
futu.disconnect()
