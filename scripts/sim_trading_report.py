#!/usr/bin/env python3
"""
模拟交易报告生成器
解决模拟账户不支持成交查询的问题

功能:
1. 从订单获取成交记录
2. 持仓查询 (带重试)
3. 账户资金
4. 收益统计
5. 日报生成
"""

from futu import *
import pandas as pd
import time
from datetime import datetime, timedelta
from pathlib import Path

# 配置
HK_SIM_ACC = 8246815
US_SIM_ACC = 8246816
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "reports"
OUTPUT_DIR.mkdir(exist_ok=True)


def get_filled_orders(trd_ctx, acc_id, market=TrdMarket.HK):
    """获取已成交订单 (替代成交记录)"""
    ret, orders = trd_ctx.order_list_query(
        trd_env=TrdEnv.SIMULATE, 
        acc_id=acc_id
    )
    
    if ret != RET_OK:
        print(f"❌ 订单查询失败：{orders}")
        return None
    
    # 过滤已成交订单
    filled = orders[orders['order_status'] == 'FILLED_ALL'].copy()
    return filled


def get_positions_with_retry(trd_ctx, acc_id, max_retries=5):
    """获取持仓 (带重试机制)"""
    for i in range(max_retries):
        ret, pos = trd_ctx.position_list_query(
            trd_env=TrdEnv.SIMULATE, 
            acc_id=acc_id
        )
        
        if ret == RET_OK and not pos.empty:
            return pos
        
        if '此数据暂时还未准备好' in str(pos):
            wait_time = (i + 1) * 2
            print(f"⏳ 持仓数据准备中... ({i+1}/{max_retries}) 等待 {wait_time}秒")
            time.sleep(wait_time)
            continue
        
        print(f"❌ 持仓查询失败：{pos}")
        return None
    
    print(f"⚠️ 重试{max_retries}次后仍无法获取持仓")
    return None


def get_account_funds(trd_ctx, acc_id, currency=Currency.HKD):
    """获取账户资金"""
    ret, acc = trd_ctx.accinfo_query(
        trd_env=TrdEnv.SIMULATE, 
        acc_id=acc_id, 
        currency=currency
    )
    
    if ret != RET_OK:
        print(f"❌ 资金查询失败：{acc}")
        return None
    
    return acc.iloc[0]


def calculate_trading_stats(filled_orders):
    """计算交易统计"""
    if filled_orders is None or filled_orders.empty:
        return {}
    
    # 计算成交金额
    filled_orders['amount'] = filled_orders['dealt_qty'] * filled_orders['dealt_avg_price']
    
    # 买卖统计
    buy_orders = filled_orders[filled_orders['trd_side'] == 'BUY']
    sell_orders = filled_orders[filled_orders['trd_side'] == 'SELL']
    
    stats = {
        'total_trades': len(filled_orders),
        'buy_trades': len(buy_orders),
        'sell_trades': len(sell_orders),
        'buy_amount': buy_orders['amount'].sum(),
        'sell_amount': sell_orders['amount'].sum(),
        'net_amount': buy_orders['amount'].sum() - sell_orders['amount'].sum(),
        'total_turnover': filled_orders['amount'].sum(),
    }
    
    # 按股票统计
    stock_stats = filled_orders.groupby('code').agg({
        'dealt_qty': 'sum',
        'amount': 'sum',
        'trd_side': 'count'
    }).rename(columns={'trd_side': 'trade_count'})
    
    return stats, stock_stats


def calculate_pnl_stats(positions):
    """计算持仓盈亏统计"""
    if positions is None or positions.empty:
        return {}
    
    # 转换数值字段
    positions['unrealized_pl_float'] = positions['unrealized_pl'].apply(
        lambda x: float(x) if x != 'N/A' else 0.0
    )
    positions['pl_ratio_float'] = positions['pl_ratio_avg_cost'].apply(
        lambda x: float(x) if x != 'N/A' else 0.0
    )
    positions['market_val_float'] = positions['market_val'].astype(float)
    
    stats = {
        'total_market_val': positions['market_val_float'].sum(),
        'total_unrealized_pl': positions['unrealized_pl_float'].sum(),
        'avg_pl_ratio': (positions['unrealized_pl_float'].sum() / 
                        positions['market_val_float'].sum() * 100) if positions['market_val_float'].sum() > 0 else 0,
        'profitable_positions': len(positions[positions['unrealized_pl_float'] > 0]),
        'losing_positions': len(positions[positions['unrealized_pl_float'] <= 0]),
    }
    
    return stats


def generate_report(trd_ctx, acc_id=HK_SIM_ACC, market_name="港股"):
    """生成模拟交易报告"""
    print(f"\n{'='*60}")
    print(f"📊 模拟交易报告 - {market_name}")
    print(f"账户 ID: {acc_id}")
    print(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")
    
    report_data = {
        'timestamp': datetime.now().isoformat(),
        'account_id': acc_id,
        'market': market_name,
    }
    
    # 1. 成交记录
    print("📈 1. 成交记录 (从订单获取)")
    print("-" * 40)
    filled_orders = get_filled_orders(trd_ctx, acc_id)
    
    if filled_orders is not None and not filled_orders.empty:
        trading_stats, stock_stats = calculate_trading_stats(filled_orders)
        
        print(f"   总成交笔数：{trading_stats['total_trades']}")
        print(f"   买入笔数：{trading_stats['buy_trades']}")
        print(f"   卖出笔数：{trading_stats['sell_trades']}")
        print(f"   买入金额：{trading_stats['buy_amount']:,.2f} HKD")
        print(f"   卖出金额：{trading_stats['sell_amount']:,.2f} HKD")
        print(f"   净买卖：{trading_stats['net_amount']:,.2f} HKD")
        print(f"   总成交额：{trading_stats['total_turnover']:,.2f} HKD")
        
        report_data['trading_stats'] = trading_stats
        report_data['filled_orders'] = filled_orders.to_dict('records')
        
        # 显示最近 10 笔成交
        print(f"\n   最近 10 笔成交:")
        recent = filled_orders.sort_values('create_time', ascending=False).head(10)
        for _, order in recent.iterrows():
            side_icon = '🟢' if order['trd_side'] == 'BUY' else '🔴'
            print(f"   {side_icon} {order['code']} | {order['trd_side']} | "
                  f"{order['dealt_qty']}股 @ {order['dealt_avg_price']} | "
                  f"{order['create_time']}")
    else:
        print("   今日无成交")
        report_data['trading_stats'] = {}
    
    # 2. 持仓信息
    print(f"\n📈 2. 持仓信息")
    print("-" * 40)
    positions = get_positions_with_retry(trd_ctx, acc_id)
    
    if positions is not None and not positions.empty:
        pnl_stats = calculate_pnl_stats(positions)
        
        print(f"   持仓数量：{len(positions)}笔")
        print(f"   总市值：{pnl_stats['total_market_val']:,.2f} HKD")
        print(f"   总盈亏：{pnl_stats['total_unrealized_pl']:,.2f} HKD")
        print(f"   盈亏比例：{pnl_stats['avg_pl_ratio']:.2f}%")
        print(f"   盈利持仓：{pnl_stats['profitable_positions']}笔")
        print(f"   亏损持仓：{pnl_stats['losing_positions']}笔")
        
        report_data['pnl_stats'] = pnl_stats
        report_data['positions'] = positions.to_dict('records')
        
        # 显示持仓明细
        print(f"\n   持仓明细:")
        for _, pos in positions.iterrows():
            pl_ratio = float(pos['pl_ratio_avg_cost']) if pos['pl_ratio_avg_cost'] != 'N/A' else 0
            pl_icon = '📈' if pl_ratio >= 0 else '📉'
            print(f"   {pl_icon} {pos['code']} ({pos['stock_name']})")
            print(f"       持仓：{pos['qty']}股 | 现价：{pos['nominal_price']} | "
                  f"市值：{pos['market_val']} | 盈亏：{pos['unrealized_pl']} ({pl_ratio:.2f}%)")
    else:
        print("   无法获取持仓信息")
        report_data['pnl_stats'] = {}
    
    # 3. 账户资金
    print(f"\n💰 3. 账户资金")
    print("-" * 40)
    funds = get_account_funds(trd_ctx, acc_id)
    
    if funds is not None:
        print(f"   总资产：{funds['total_assets']:,.2f} HKD")
        print(f"   现金：{funds['cash']:,.2f} HKD")
        print(f"   市值：{funds['market_val']:,.2f} HKD")
        print(f"   购买力：{funds['power']:,.2f} HKD")
        print(f"   风控状态：{funds['risk_status']}")
        
        report_data['funds'] = funds.to_dict()
    else:
        print("   无法获取资金信息")
    
    # 4. 收益统计
    print(f"\n📊 4. 收益统计")
    print("-" * 40)
    
    if 'pnl_stats' in report_data and report_data['pnl_stats'] and 'funds' in report_data:
        pnl_stats = report_data['pnl_stats']
        total_assets = float(funds['total_assets'])
        unrealized_pl = pnl_stats.get('total_unrealized_pl', 0)
        
        # 估算初始资金 (总资产 - 未实现盈亏)
        estimated_initial = total_assets - unrealized_pl
        return_rate = (unrealized_pl / estimated_initial * 100) if estimated_initial > 0 else 0
        
        print(f"   估算初始资金：{estimated_initial:,.2f} HKD")
        print(f"   总收益：{unrealized_pl:,.2f} HKD")
        print(f"   收益率：{return_rate:.2f}%")
        
        report_data['return_rate'] = return_rate
        report_data['estimated_initial'] = estimated_initial
    
    print(f"\n{'='*60}")
    
    return report_data


def save_report(report_data, filename=None):
    """保存报告到文件"""
    if filename is None:
        filename = f"sim_trading_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    output_path = OUTPUT_DIR / filename
    
    # 移除不能序列化的数据
    export_data = {k: v for k, v in report_data.items() 
                   if k not in ['filled_orders', 'positions']}
    
    import json
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    
    print(f"📁 报告已保存：{output_path}")
    return output_path


def main():
    """主函数"""
    print("\n🚀 模拟交易报告生成器")
    print("="*60)
    
    # 创建交易上下文
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.HK,
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    
    try:
        # 生成港股模拟报告
        report_data = generate_report(trd_ctx, HK_SIM_ACC, "港股模拟")
        
        # 保存报告
        save_report(report_data)
        
        print("\n✅ 报告生成完成")
        print("\n💡 提示:")
        print("   - 成交数据从订单记录获取 (order_list_query)")
        print("   - 持仓查询可能需要多次重试")
        print("   - 报告保存在 data/reports/ 目录")
        
    finally:
        trd_ctx.close()


if __name__ == '__main__':
    main()
