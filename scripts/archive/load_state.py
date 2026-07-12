#!/usr/bin/env python3.11
"""
FTSeek-US 状态加载脚本

从保存的文件中加载配置和状态
"""
import os
import json
import glob
from datetime import datetime

# 保存目录
SAVE_DIR = "data/snapshots"

print("=" * 80)
print("  FTSeek-US 状态加载")
print("=" * 80)
print()

# 查找最新的保存文件
files = glob.glob(f"{SAVE_DIR}/state_*.json")
if not files:
    print("❌ 未找到保存的状态文件")
    exit(1)

# 按时间排序，取最新的
latest_file = sorted(files)[-1]
print(f"1. 加载文件：{latest_file}")
print("-" * 40)

# 加载 JSON
with open(latest_file, 'r', encoding='utf-8') as f:
    state = json.load(f)

print(f"   保存时间：{state['timestamp']}")
print()

# 显示配置
print("2. API Keys 配置")
print("-" * 40)
for key, value in state['api_config'].items():
    masked = f"{value[:10]}...{value[-5:]}" if len(value) > 15 else "***"
    print(f"   {key}: {masked}")
print()

print("3. 监控股票池")
print("-" * 40)
for symbol in state['watchlist']:
    print(f"   {symbol}")
print()

print("4. 持仓配置")
print("-" * 40)
for symbol, pos in state['positions'].items():
    print(f"   {symbol}: cost=${pos['cost']}, qty={pos['qty']}, add_times={pos['add_times']}")
print()

print("5. 价格快照")
print("-" * 40)
for symbol, price_info in state['prices'].items():
    print(f"   {symbol}: ${price_info['price']:.2f} ({price_info['change']:+.2f}, {price_info['change_percent']:+.2f}%)")
print()

print("6. 交易配置")
print("-" * 40)
config = state['config']
print(f"   止损：-{config['STOP_LOSS_RATIO']*100:.0f}%")
print(f"   止盈 1: +{config['TAKE_PROFIT_1_RATIO']*100:.0f}%")
print(f"   止盈 2: +{config['TAKE_PROFIT_2_RATIO']*100:.0f}%")
print(f"   止盈 3: +{config['TAKE_PROFIT_3_RATIO']*100:.0f}%")
print(f"   补仓：下跌{config['ADD_POSITION_RATIO']*100:.0f}%")
print(f"   最多补仓：{config['MAX_ADD_POSITION_TIMES']}次")
print()

print("=" * 80)
print(f"  ✅ 状态加载成功")
print("=" * 80)
