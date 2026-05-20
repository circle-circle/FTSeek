#!/usr/bin/env python3
"""
测试备用数据源

功能:
- 测试 TwelveData/Finnhub/Alpha Vantage
- 验证 Fallback 机制
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.market_data_backup import get_market_backup

def test_backup_sources():
    """测试备用数据源"""
    print("=" * 60)
    print("测试备用数据源")
    print("=" * 60)
    
    backup = get_market_backup()
    
    # 健康检查
    print("\n【健康检查】")
    status = backup.health_check()
    print(f"  状态：{'✅ 健康' if status['healthy'] else '❌ 不健康'}")
    print(f"  可用数据源：{list(status['sources'].keys())}")
    
    # 测试股票
    test_symbols = [
        'US.AAPL',
        'US.NVDA',
        'US.MSFT',
        'HK.00700',
    ]
    
    print("\n【价格测试】")
    for symbol in test_symbols:
        print(f"\n测试 {symbol}:")
        
        # 获取价格
        price = backup.get_price(symbol)
        if price and price > 0:
            print(f"  ✅ 价格：${price}")
        else:
            print(f"  ❌ 获取失败")
        
        # 获取完整数据
        data = backup.get_market_data(symbol)
        if data:
            print(f"  ✅ 完整数据:")
            print(f"     价格：${data.get('price', 'N/A')}")
            print(f"     涨跌：{data.get('change', 'N/A')} ({data.get('change_percent', 'N/A')}%)")
            print(f"     成交量：{data.get('volume', 'N/A'):,}")
            print(f"     最高：${data.get('high', 'N/A')}")
            print(f"     最低：${data.get('low', 'N/A')}")
        else:
            print(f"  ❌ 完整数据获取失败")
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_backup_sources()
