#!/usr/bin/env python3
"""
富途账户信息查询工具
解决模拟账户无法查询成交记录的问题
"""

from futu import *
import sys
from datetime import datetime

# 账户配置
ACCOUNTS = {
    'hk_sim': {'id': 8246815, 'env': TrdEnv.SIMULATE, 'market': TrdMarket.HK, 'name': '港股模拟'},
    'us_sim': {'id': 8246816, 'env': TrdEnv.SIMULATE, 'market': TrdMarket.US, 'name': '美股模拟'},
    'hk_option_sim': {'id': 8246818, 'env': TrdEnv.SIMULATE, 'market': TrdMarket.HK, 'name': '港股期权模拟'},
    'us_option_sim': {'id': 8246819, 'env': TrdEnv.SIMULATE, 'market': TrdMarket.US, 'name': '美股期权模拟'},
    'real': {'id': 281756480166019917, 'env': TrdEnv.REAL, 'market': TrdMarket.NONE, 'name': '实盘主账户'},
}

def print_accounts():
    """打印所有账户列表"""
    print("\n" + "="*60)
    print("📋 富途账户列表")
    print("="*60)
    
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.NONE,
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    
    ret, data = trd_ctx.get_acc_list()
    if ret == RET_OK:
        print("\n模拟账户 (不支持成交查询):")
        sim_accs = data[data['trd_env'] == 'SIMULATE']
        for _, acc in sim_accs.iterrows():
            print(f"  ID: {acc['acc_id']} | 市场: {acc['trdmarket_auth']}")
        
        print("\n实盘账户 (支持所有查询):")
        real_accs = data[data['trd_env'] == 'REAL']
        for _, acc in real_accs.iterrows():
            status = acc['acc_status']
            status_icon = '✅' if status == 'ACTIVE' else '❌'
            print(f"  {status_icon} ID: {acc['acc_id']} | 市场: {acc['trdmarket_auth']} | 状态: {status}")
    else:
        print(f"❌ 查询失败: {data}")
    
    trd_ctx.close()

def get_orders_as_deals(acc_key='hk_sim'):
    """获取订单数据作为成交记录（模拟账户解决方案）"""
    acc = ACCOUNTS[acc_key]
    
    print(f"\n📊 {acc['name']} 今日成交记录 (从订单获取)")
    print("="*60)
    
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=acc['market'],
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    
    ret, orders = trd_ctx.order_list_query(trd_env=acc['env'], acc_id=acc['id'])
    
    if ret == RET_OK:
        # 过滤已成交订单
        filled = orders[orders['order_status'] == 'FILLED_ALL']
        
        if filled.empty:
            print("  今日无成交")
        else:
            total_trades = len(filled)
            print(f"  成交笔数: {total_trades}")
            print()
            
            for _, order in filled.iterrows():
                side_icon = '🟢' if order['trd_side'] == 'BUY' else '🔴'
                print(f"  {side_icon} {order['code']}")
                print(f"      方向: {order['trd_side']}")
                print(f"      数量: {order['deelt_qty']}股")
                print(f"      价格: {order['dealt_avg_price']}")
                print(f"      时间: {order['create_time']}")
                print()
    else:
        print(f"❌ 查询失败: {orders}")
    
    trd_ctx.close()

def get_positions(acc_key='hk_sim'):
    """获取持仓"""
    acc = ACCOUNTS[acc_key]
    
    print(f"\n📈 {acc['name']} 持仓")
    print("="*60)
    
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=acc['market'],
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    
    # 模拟账户持仓查询可能失败，添加重试
    max_retries = 3
    for i in range(max_retries):
        ret, pos = trd_ctx.position_list_query(trd_env=acc['env'], acc_id=acc['id'])
        
        if ret == RET_OK:
            if pos.empty:
                print("  无持仓")
            else:
                total_value = pos['market_val'].astype(float).sum()
                print(f"  持仓数量: {len(pos)}笔 | 总市值: {total_value:.2f}")
                print()
                
                for _, p in pos.iterrows():
                    pl_ratio = float(p['pl_ratio_avg_cost']) if p['pl_ratio_avg_cost'] != 'N/A' else 0
                    pl_icon = '📈' if pl_ratio >= 0 else '📉'
                    print(f"  {pl_icon} {p['code']} ({p['stock_name']})")
                    print(f"      持仓: {p['qty']}股 | 可卖: {p['can_sell_qty']}股")
                    print(f"      现价: {p['nominal_price']} | 市值: {p['market_val']}")
                    print(f"      盈亏: {p['unrealized_pl']} ({pl_ratio:.2f}%)")
                    print()
            break
        
        if '此数据暂时还未准备好' in str(pos):
            print(f"  ⏳ 数据准备中... (尝试 {i+1}/{max_retries})")
            import time
            time.sleep(2)
            continue
        
        print(f"❌ 查询失败: {pos}")
        break
    
    trd_ctx.close()

def get_account_funds(acc_key='hk_sim'):
    """获取账户资金"""
    acc = ACCOUNTS[acc_key]
    
    print(f"\n💰 {acc['name']} 资金")
    print("="*60)
    
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=acc['market'],
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    
    currency = Currency.HKD if 'hk' in acc_key else Currency.USD
    ret, info = trd_ctx.accinfo_query(trd_env=acc['env'], acc_id=acc['id'], currency=currency)
    
    if ret == RET_OK:
        row = info.iloc[0]
        print(f"  总资产: {row['total_assets']}")
        print(f"  现金: {row['cash']}")
        print(f"  市值: {row['market_val']}")
        print(f"  购买力: {row['power']}")
        print(f"  风控: {row['risk_status']}")
    else:
        print(f"❌ 查询失败: {info}")
    
    trd_ctx.close()

def main():
    """主函数"""
    print("\n🚀 富途账户查询工具")
    print("="*60)
    
    # 打印账户列表
    print_accounts()
    
    # 模拟账户查询（使用订单代替成交）
    print("\n📌 模拟账户查询 (使用订单数据代替成交)")
    get_orders_as_deals('hk_sim')
    get_positions('hk_sim')
    get_account_funds('hk_sim')
    
    # 实盘账户查询
    print("\n📌 实盘账户查询")
    get_positions('real')
    get_account_funds('real')
    
    print("\n" + "="*60)
    print("✅ 查询完成")
    print("="*60)
    
    print("\n💡 提示:")
    print("  - 模拟账户不支持成交记录查询，使用订单数据代替")
    print("  - 实盘账户支持所有查询类型")
    print("  - 文档: docs/FUTU_ACCOUNT_CONFIG.md")

if __name__ == '__main__':
    main()