#!/usr/bin/env python3
"""
测试富途 API 集成

功能:
- 测试富途连接
- 同步持仓
- 测试自动下单
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.futu_integration import get_futu_integration

def test_connection():
    """测试富途连接"""
    print("=" * 60)
    print("测试富途 API 连接")
    print("=" * 60)
    
    futu = get_futu_integration(
        host="127.0.0.1",
        port=11111,
        trd_env="SIMULATE"
    )
    
    if not futu.connect():
        print("❌ 富途连接失败")
        return False
    
    print("✅ 富途连接成功")
    
    # 获取账户列表
    print("\n【账户列表】")
    accounts = futu.get_acc_list()
    if accounts:
        for acc in accounts:
            print(f"  账户 ID: {acc['acc_id']}")
            print(f"    环境：{acc['trd_env']}")
            print(f"    货币：{acc['currency']}")
            print(f"    权限：{acc.get('trdmarket_auth', [])}")
            print(f"    角色：{acc.get('acc_role', 'NORMAL')}")
    else:
        print("  无账户")
    
    # 获取持仓
    if accounts and len(accounts) > 0:
        acc_id = accounts[0]['acc_id']
        futu.acc_id = acc_id
        print(f"使用账户：{acc_id}")
    else:
        print("❌ 无可用账户，跳过持仓测试")
        futu.disconnect()
        return False
        
        print(f"\n【持仓列表】(账户 {acc_id})")
        positions = futu.get_positions(acc_id)
        if positions:
            for pos in positions:
                print(f"  {pos['code']} {pos['name']}")
                print(f"    数量：{pos['qty']}")
                print(f"    成本：${pos['avg_cost']}")
                print(f"    现价：${pos['current_price']}")
                print(f"    市值：${pos['market_val']:,.2f}")
                print(f"    盈亏：{pos['pl_ratio']:.2f}%")
        else:
            print("  无持仓")
        
        # 获取资金
        print(f"\n【账户资金】")
        funds = futu.get_funds(acc_id)
        if funds:
            print(f"  总资产：${funds['total_assets']:,.2f}")
            print(f"  现金：${funds['cash']:,.2f}")
            print(f"  市值：${funds['market_val']:,.2f}")
            print(f"  可用资金：${funds['available_funds']:,.2f}")
        else:
            print("  获取失败")
    
    # 测试获取股价
    print(f"\n【测试股价查询】")
    test_codes = ['US.AAPL', 'US.NVDA', 'HK.00700']
    for code in test_codes:
        price = futu.get_stock_price(code)
        if price:
            print(f"  ✅ {code}: ${price}")
        else:
            print(f"  ❌ {code}: 获取失败")
    
    # 加载审计记录
    print(f"\n【审计日志】")
    records = futu.load_audit_records(limit=5)
    if records:
        for rec in records[:5]:
            print(f"  {rec.get('timestamp', 'N/A')[:19]}")
            print(f"    {rec.get('action', 'N/A')} {rec.get('code', 'N/A')}")
            print(f"    数量：{rec.get('quantity', 'N/A')} 价格：${rec.get('price', 'N/A')}")
    else:
        print("  无记录")
    
    # 同步持仓
    print(f"\n【同步持仓】")
    success = futu.sync_positions_to_local(
        '/opt/ftseek/FTSeek-US/data/trade_records.json',
        acc_id
    )
    if success:
        print("  ✅ 同步成功")
    else:
        print("  ❌ 同步失败")
    
    futu.disconnect()
    print("\n" + "=" * 60)
    return True


if __name__ == "__main__":
    test_connection()
