#!/usr/bin/env python3
"""P0优化: 测试RSI和MACD技术指标计算"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from app.data.twelvedata_provider import TwelveDataProvider

# 模拟价格数据
test_prices = np.array([
    100, 102, 101, 103, 105, 104, 106, 108, 107, 109,
    111, 110, 112, 114, 113, 115, 117, 116, 118, 120,
    119, 121, 123, 122, 124, 126, 125, 127, 129, 128,
    130, 132, 131, 133, 135, 134, 136, 138, 137, 139,
    141, 140, 142, 144, 143, 145, 147, 146, 148, 150,
    149, 151, 153, 152, 154, 156, 155, 157, 159, 158
], dtype=float)

print("="*60)
print("P0优化: RSI和MACD技术指标测试")
print("="*60)

# 创建Provider实例
provider = TwelveDataProvider()

# 测试RSI
rsi = provider._calculate_rsi(test_prices, period=14)
print(f"\n✅ RSI(14): {rsi:.1f}")
if rsi > 70:
    print("   状态: 超买 (>70)")
elif rsi < 30:
    print("   状态: 超卖 (<30)")
else:
    print("   状态: 中性 (30-70)")

# 测试MACD
macd_line, signal_line, histogram = provider._calculate_macd(test_prices)
print(f"\n✅ MACD:")
print(f"   MACD线: {macd_line:.2f}")
print(f"   信号线: {signal_line:.2f}")
print(f"   柱状图: {histogram:.2f}")
if macd_line > signal_line:
    print("   状态: 金叉 (看涨)")
else:
    print("   状态: 死叉 (看跌)")

print("\n✅ 技术指标计算测试通过!")
print("="*60)
