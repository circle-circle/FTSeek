#!/usr/bin/env python3
"""获取富途历史交易数据并生成投资风格分析"""
import sys
import os
from datetime import datetime, timedelta
import pandas as pd

os.environ["FUTU_LOG_LEVEL"] = "ERROR"
sys.path.insert(0, '/opt/ftseek/FTSeek')

from futu import OpenSecTradeContext, TrdMarket, TrdEnv

print("=" * 70)
print("富途历史交易数据获取 & 投资风格分析")
print("=" * 70)
print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# 连接交易 API
trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111, filter_trdmarket=TrdMarket.HK)

# 获取账户列表
ret, acc_data = trd_ctx.get_acc_list()
if ret != 0 or len(acc_data) == 0:
    print(f"✗ 获取账户列表失败：{acc_data}")
    sys.exit(1)

print(f"✓ 找到 {len(acc_data)} 个交易账户")
print()

# 遍历所有账户
for idx, row in acc_data.iterrows():
    acc_id = row['acc_id']
    trd_env = row['trd_env']
    currency = row['currency']
    
    print(f"【账户 {idx+1}】ID: {acc_id} | 环境：{trd_env} | 货币：{currency}")
    print("-" * 70)
    
    # 获取当前持仓
    ret, pos_data = trd_ctx.get_position_list(acc_id=acc_id)
    if ret == 0 and len(pos_data) > 0:
        print(f"当前持仓 ({len(pos_data)} 只股票):")
        for _, p in pos_data.iterrows():
            print(f"  {p['code']} {p['name']}: {p['qty']}股 | "
                  f"成本={p['cost_price']:.2f} | 现价={p['last_price']:.2f} | "
                  f"盈亏={p['pl_ratio']:.2f}%")
    else:
        print("  无持仓")
    print()
    
    # 获取账户资金
    ret, funds = trd_ctx.get_acc_funds(acc_id=acc_id)
    if ret == 0:
        print(f"账户资金:")
        print(f"  总资产：{funds['total_asset'].iloc[0]:.2f} {currency}")
        print(f"  现金：{funds['cash'].iloc[0]:.2f} {currency}")
        print(f"  市值：{funds['market_val'].iloc[0]:.2f} {currency}")
        if 'pnl' in funds.columns:
            print(f"  总盈亏：{funds['pnl'].iloc[0]:.2f} {currency}")
    print()
    
    # 获取历史订单 (最近 30 天)
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    end_date = datetime.now().strftime('%Y-%m-%d')
    
    print(f"历史订单 ({start_date} 至 {end_date}):")
    
    # 富途 API 使用 get_history_order_list 在模拟环境，真实环境用其他方法
    try:
        ret, orders = trd_ctx.get_history_order_list(
            acc_id=acc_id,
            start=start_date,
            end=end_date
        )
        
        if ret == 0 and len(orders) > 0:
            print(f"  ✓ 获取到 {len(orders)} 条订单")
            
            # 分析订单数据
            print(f"  订单状态分布:")
            print(orders['order_status'].value_counts().to_string().replace('\n', '\n  '))
            
            print(f"  买卖方向分布:")
            print(orders['side'].value_counts().to_string().replace('\n', '\n  '))
            
            # 计算交易统计
            filled_orders = orders[orders['order_status'] == 'FILLED']
            if len(filled_orders) > 0:
                print(f"  已成交订单：{len(filled_orders)} 条")
                print(f"  成交总金额：{filled_orders['price'].sum() * filled_orders['qty'].sum():.2f}")
        else:
            print(f"  无历史订单或获取失败")
            
    except Exception as e:
        print(f"  获取历史订单异常：{e}")
    
    print()
    
    # 获取历史成交 (更可靠)
    print(f"历史成交 ({start_date} 至 {end_date}):")
    try:
        ret, deals = trd_ctx.get_history_deal_list(
            acc_id=acc_id,
            start=start_date,
            end=end_date
        )
        
        if ret == 0 and len(deals) > 0:
            print(f"  ✓ 获取到 {len(deals)} 条成交记录")
            
            # 分析成交数据
            buy_deals = deals[deals['side'] == 'BUY']
            sell_deals = deals[deals['side'] == 'SELL']
            
            print(f"  买入成交：{len(buy_deals)} 条")
            print(f"  卖出成交：{len(sell_deals)} 条")
            
            if len(buy_deals) > 0:
                print(f"  买入总金额：{buy_deals['price'].sum() * buy_deals['qty'].sum():.2f}")
            if len(sell_deals) > 0:
                print(f"  卖出总金额：{sell_deals['price'].sum() * sell_deals['qty'].sum():.2f}")
            
            # 投资风格分析
            print()
            print("【投资风格分析】")
            print(f"  交易频率：{len(deals)} 条 / 30 天 = {len(deals)/30:.1f} 条/天")
            
            # 持仓周期分析 (简化版)
            if len(deals) >= 2:
                first_trade = deals['create_time'].min()
                last_trade = deals['create_time'].max()
                print(f"  交易时间跨度：{first_trade} 至 {last_trade}")
            
            # 标的分布
            if 'code' in deals.columns:
                stock_counts = deals['code'].value_counts()
                print(f"  最活跃股票:")
                for code, count in stock_counts.head(5).items():
                    print(f"    {code}: {count} 次")
        else:
            print(f"  无历史成交记录")
            
    except Exception as e:
        print(f"  获取历史成交异常：{e}")
    
    print()
    print("=" * 70)
    print()

trd_ctx.close()

print("数据获取完成!")
print()
print("💡 提示:")
print("  - 如需更长期数据 (如 2026-01-01 至今)，请在富途牛牛客户端导出 CSV 对账单")
print("  - V4 模块将包含完整的回测和投资风格分析功能 (Task 5.2)")
