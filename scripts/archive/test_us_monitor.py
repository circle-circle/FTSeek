#!/usr/bin/env python3.8
"""
FTSeek-US 美股行情监控测试脚本

测试富途接口获取美股价格
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

from futu import OpenQuoteContext, RET_OK

print("=" * 80)
print("  FTSeek-US 美股行情测试")
print("=" * 80)
print()

# 测试股票
test_stocks = ["US.AAPL", "US.MSFT", "US.NVDA"]

print(f"测试股票：{test_stocks}")
print()

# 连接富途
quote_ctx = OpenQuoteContext(
    host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
    port=int(os.getenv("FUTU_OPEND_PORT", "11111"))
)

print(f"连接：{os.getenv('FUTU_OPEND_HOST')}:{os.getenv('FUTU_OPEND_PORT')}")
print()

# 获取行情
for code in test_stocks:
    print(f"获取 {code} 行情...")
    
    ret_code, ret_data = quote_ctx.get_stock_quote([code])
    
    if ret_code == RET_OK and ret_data is not None and len(ret_data) > 0:
        row = ret_data[0]
        
        last_price = row.get("last_price", 0)
        change = row.get("change", 0)
        change_percent = row.get("change_percent", 0)
        open_price = row.get("open", 0)
        high_price = row.get("high", 0)
        low_price = row.get("low", 0)
        prev_close = row.get("prev_close", 0)
        
        print(f"  ✅ {code}:")
        print(f"     当前价：${last_price:.2f}")
        print(f"     涨跌：{change:+.2f} ({change_percent:+.2f}%)")
        print(f"     开盘：${open_price:.2f}")
        print(f"     最高：${high_price:.2f}")
        print(f"     最低：${low_price:.2f}")
        print(f"     昨收：${prev_close:.2f}")
    else:
        print(f"  ❌ {code}: 获取失败 - {ret_data}")
    
    print()

quote_ctx.close()

print("=" * 80)
print("  测试完成")
print("=" * 80)
