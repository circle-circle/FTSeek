#!/usr/bin/env python3
"""快速测试 FutuOpenD 连接"""
import sys
import os
os.environ["FUTU_LOG_LEVEL"] = "ERROR"

from futu import OpenQuoteContext, OpenSecTradeContext, TrdMarket

print("测试 FutuOpenD 连接...")

# 测试行情连接
try:
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    ret, data = quote_ctx.get_market_snapshot(['HK.00700'])
    if ret == 0:
        print(f"✓ 行情连接成功！腾讯控股：${data['last_price'].iloc[0]:.2f}")
    else:
        print(f"✗ 行情获取失败：{data}")
    quote_ctx.close()
except Exception as e:
    print(f"✗ 行情连接失败：{e}")

# 测试交易连接
try:
    trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111, filter_trdmarket=TrdMarket.HK)
    ret, acc_data = trd_ctx.get_acc_list()
    if ret == 0 and len(acc_data) > 0:
        print(f"✓ 交易连接成功！账户数：{len(acc_data)}")
        acc_id = acc_data.iloc[0]['acc_id']
        
        # 获取历史订单 (最近 30 天)
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        end = datetime.now().strftime('%Y-%m-%d')
        
        print(f"获取历史订单：{start} 至 {end}")
        ret, orders = trd_ctx.get_history_order_list(acc_id=acc_id, start=start, end=end)
        if ret == 0:
            print(f"✓ 历史订单：{len(orders)} 条")
            if len(orders) > 0:
                print(f"  最早：{orders['create_time'].min()}")
                print(f"  最晚：{orders['create_time'].max()}")
        else:
            print(f"✗ 历史订单获取失败：{orders}")
        
        # 获取持仓
        ret, pos = trd_ctx.get_position_list(acc_id=acc_id)
        if ret == 0:
            print(f"✓ 当前持仓：{len(pos)} 只股票")
        
        # 获取资金
        ret, funds = trd_ctx.get_acc_funds(acc_id=acc_id)
        if ret == 0:
            print(f"✓ 账户资金：总资产={funds['total_asset'].iloc[0]:.2f}")
    else:
        print(f"✗ 交易连接失败：{acc_data}")
    trd_ctx.close()
except Exception as e:
    print(f"✗ 交易连接失败：{e}")

print("\n完成!")
