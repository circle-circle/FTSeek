#!/usr/bin/env python3
"""快速获取富途账户概览"""
import sys
import os
from datetime import datetime, timedelta

os.environ["FUTU_LOG_LEVEL"] = "ERROR"

from futu import OpenSecTradeContext, TrdMarket

print("富途账户概览")
print("=" * 60)

trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111, filter_trdmarket=TrdMarket.HK)

ret, acc_data = trd_ctx.get_acc_list()
if ret != 0:
    print(f"错误：{acc_data}")
    sys.exit(1)

print(f"账户数：{len(acc_data)}\n")

for idx, row in acc_data.iterrows():
    acc_id = row['acc_id']
    trd_env = row['trd_env']
    
    print(f"【账户{idx+1}】{acc_id} (环境：{trd_env})")
    
    # 持仓
    ret, pos = trd_ctx.get_position_list(acc_id=acc_id)
    if ret == 0:
        print(f"  持仓：{len(pos)} 只股票")
        if len(pos) > 0:
            for _, p in pos.head(3).iterrows():
                print(f"    {p['code']}: {p['pl_ratio']:.1f}%")
            if len(pos) > 3:
                print(f"    ... 还有 {len(pos)-3} 只")
    
    # 资金
    ret, funds = trd_ctx.get_acc_funds(acc_id=acc_id)
    if ret == 0:
        print(f"  总资产：{funds['total_asset'].iloc[0]:.0f}")
        print(f"  现金：{funds['cash'].iloc[0]:.0f}")
    
    # 最近成交 (简化)
    start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    end = datetime.now().strftime('%Y-%m-%d')
    
    try:
        ret, deals = trd_ctx.get_history_deal_list(acc_id=acc_id, start=start, end=end)
        if ret == 0 and len(deals) > 0:
            print(f"  近 7 天成交：{len(deals)} 条")
            buys = len(deals[deals['side'] == 'BUY'])
            sells = len(deals[deals['side'] == 'SELL'])
            print(f"    买入：{buys} | 卖出：{sells}")
    except Exception:
        pass  # get_history_deal_list failed
    
    print()

trd_ctx.close()
print("完成!")
