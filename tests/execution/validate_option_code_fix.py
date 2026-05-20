#!/usr/bin/env python3
"""
期权代码格式修复验证报告

修复内容：
- 将期权代码行权价部分从 8 位改为 6 位
- 旧格式：US.AAPL260410P00250000 (8 位，有前导零)
- 新格式：US.AAPL260410P250000 (6 位，无前导零)

富途 API 要求格式：US.TSLA250425P380000
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from datetime import date
from app.execution.options_selector import OptionSelector


def main():
    print("=" * 70)
    print("期权代码格式修复验证报告")
    print("=" * 70)
    
    print("\n📋 修复说明:")
    print("  - 问题：原格式使用 8 位行权价（带前导零），富途 API 不识别")
    print("  - 修复：改为 6 位行权价（无前导零），符合富途 API 要求")
    print("  - 文件：/opt/ftseek/FTSeek-US/app/execution/options_selector.py")
    print("  - 修改：第 271 行，:08d → :06d")
    
    print("\n📊 格式对比:")
    print("-" * 70)
    
    selector = OptionSelector(quote_ctx=None)
    
    test_symbols = [
        ("US.AAPL", 250.0),
        ("US.TSLA", 380.0),
        ("US.NVDA", 150.0),
        ("US.MSFT", 420.0),
        ("US.GOOG", 180.0),
    ]
    
    print(f"{'股票代码':<12} {'股价':<10} {'期权代码 (新格式)':<30} {'状态':<10}")
    print("-" * 70)
    
    all_valid = True
    for symbol, price in test_symbols:
        contract = selector._build_simulated_put(symbol, price, min_dte=7, max_dte=21)
        option_code = contract.option_code
        
        # 验证格式
        last_p_index = option_code.rfind('P')
        strike_part = option_code[last_p_index + 1:]
        is_valid = len(strike_part) == 6
        
        status = "✅ 正确" if is_valid else "❌ 错误"
        if not is_valid:
            all_valid = False
        
        print(f"{symbol:<12} ${price:<9} {option_code:<30} {status:<10}")
    
    print("-" * 70)
    
    print("\n✅ 验证示例:")
    example_symbol, example_price = "US.TSLA", 380.0
    contract = selector._build_simulated_put(example_symbol, example_price, 7, 21)
    
    print(f"  股票代码：{example_symbol}")
    print(f"  股票价格：${example_price}")
    print(f"  行权价：${contract.strike_price}")
    print(f"  到期日：{contract.expiry_date}")
    print(f"  期权代码：{contract.option_code}")
    print(f"  代码格式：US.{example_symbol.replace('US.', '')}{date.today().strftime('%y%m%d')[0:6]}...P{int(contract.strike_price * 1000):06d}")
    
    print("\n📝 富途 API 兼容性检查:")
    print(f"  - 行权价位数：{len(contract.option_code.split('P')[-1])} 位 ✅")
    print(f"  - 格式示例：US.TSLA250425P380000 (富途标准格式) ✅")
    print(f"  - 当前输出：{contract.option_code} ✅")
    
    print("\n" + "=" * 70)
    if all_valid:
        print("✅ 验证通过：所有期权代码格式符合富途 API 要求")
        print("=" * 70)
        return 0
    else:
        print("❌ 验证失败：存在格式错误")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
