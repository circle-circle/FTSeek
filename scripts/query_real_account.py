#!/usr/bin/env python3
"""
富途实盘账号历史交易记录与收益查询
查询实盘账号的历史订单、成交记录、持仓和收益情况
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
import json

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek-US')
os.environ.setdefault('FUTU_HOST', '127.0.0.1')
os.environ.setdefault('FUTU_PORT', '11111')

from futu import *


def create_trade_context(trd_env=TrdEnv.REAL):
    """创建交易上下文"""
    ctx = OpenSecTradeContext(
        host=os.getenv('FUTU_HOST', '127.0.0.1'),
        port=int(os.getenv('FUTU_PORT', '11111')),
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    return ctx


def get_account_list(ctx, trd_env=TrdEnv.REAL):
    """获取账户列表"""
    ret, acc_list = ctx.get_acc_list()
    if ret != RET_OK:
        print(f"❌ 获取账户列表失败：{acc_list}")
        return []
    
    # 筛选实盘账户
    real_accounts = []
    for idx, row in acc_list.iterrows():
        if row['trd_env'] == 'REAL' or trd_env == TrdEnv.REAL:
            real_accounts.append({
                'acc_id': row['acc_id'],
                'acc_type': row['acc_type'],
                'trd_env': row['trd_env'],
                'trdmarket_auth': row.get('trdmarket_auth', '')
            })
    
    return real_accounts


def unlock_trade(ctx, acc_id, password=None):
    """解锁交易"""
    # 尝试从环境变量获取密码
    if not password:
        password = os.getenv('FUTU_TRD_PWD_MD5', '')
    
    # 如果环境变量没有，尝试交互式输入
    if not password:
        print("\n⚠️ 未设置 FUTU_TRD_PWD_MD5 环境变量")
        try:
            password = input("请输入交易密码 (MD5 加密): ").strip()
            if not password:
                return False, "未提供密码"
        except (EOFError, KeyboardInterrupt):
            print("\n❌ 用户取消输入")
            return False, "用户取消"
    
    ret, data = ctx.unlock_trade(password=password)
    if ret != RET_OK:
        print(f"❌ 解锁交易失败：{data}")
        return False, data
    
    print(f"✅ 账户 {acc_id} 解锁成功")
    return True, data


def get_account_funds(ctx, acc_id, trd_env=TrdEnv.REAL):
    """获取账户资金"""
    ret, funds = ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
    if ret != RET_OK:
        print(f"❌ 获取资金信息失败：{funds}")
        return None
    
    if len(funds) == 0:
        return None
    
    return funds.to_dict('records')[0]


def get_positions(ctx, acc_id, trd_env=TrdEnv.REAL):
    """获取持仓列表"""
    ret, positions = ctx.get_position_list(trd_env=trd_env, acc_id=acc_id)
    if ret != RET_OK:
        print(f"❌ 获取持仓失败：{positions}")
        return []
    
    return positions.to_dict('records')


def get_history_orders(ctx, acc_id, trd_env=TrdEnv.REAL, days=30):
    """获取历史订单"""
    # 计算日期范围
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    print(f"📅 查询日期范围：{start_date} 至 {end_date}")
    
    ret, orders = ctx.get_history_order_list(
        trd_env=trd_env,
        acc_id=acc_id,
        start=start_date,
        end=end_date
    )
    
    if ret != RET_OK:
        print(f"❌ 获取历史订单失败：{orders}")
        return []
    
    return orders.to_dict('records')


def get_filled_orders(ctx, acc_id, trd_env=TrdEnv.REAL, days=30):
    """获取已成交订单"""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    ret, orders = ctx.get_history_order_list(
        trd_env=trd_env,
        acc_id=acc_id,
        start=start_date,
        end=end_date,
        status=OrderStatus.FILLED  # 只查询已成交订单
    )
    
    if ret != RET_OK:
        print(f"❌ 获取成交订单失败：{orders}")
        return []
    
    return orders.to_dict('records')


def calculate_profit_loss(filled_orders):
    """计算盈亏"""
    if not filled_orders:
        return {
            'total_profit': 0,
            'total_loss': 0,
            'net_profit': 0,
            'win_count': 0,
            'loss_count': 0,
            'win_rate': 0
        }
    
    total_profit = 0
    total_loss = 0
    win_count = 0
    loss_count = 0
    
    for order in filled_orders:
        # 计算单笔盈亏 (简化计算)
        qty = order.get('qty', 0)
        price = order.get('price', 0)
        
        # 注意：这里需要更复杂的逻辑来计算实际盈亏
        # 因为需要匹配买入和卖出订单
        # 这里仅做简单统计
        
        if order.get('side') == 'BUY':
            pass  # 买入订单，暂不计算
        elif order.get('side') == 'SELL':
            # 卖出订单，可能是止盈或止损
            pass
    
    return {
        'total_profit': total_profit,
        'total_loss': total_loss,
        'net_profit': total_profit + total_loss,
        'win_count': win_count,
        'loss_count': loss_count,
        'win_rate': win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0
    }


def format_currency(value, currency='HKD'):
    """格式化货币"""
    if value is None:
        return f'N/A {currency}'
    return f'{value:,.2f} {currency}'


def print_account_summary(acc_id, funds, positions, filled_orders):
    """打印账户摘要"""
    print("\n" + "=" * 70)
    print(f"📊 账户 {acc_id} 交易摘要")
    print("=" * 70)
    
    # 资金信息
    if funds:
        print("\n💰 账户资金:")
        print(f"   总资产：{format_currency(funds.get('total_assets'))}")
        print(f"   可用资金：{format_currency(funds.get('available_funds'))}")
        print(f"   现金：{format_currency(funds.get('cash'))}")
        print(f"   市值：{format_currency(funds.get('market_val'))}")
        print(f"   盈亏：{format_currency(funds.get('pnl'))}")
    
    # 持仓信息
    print(f"\n📈 当前持仓：{len(positions)} 只")
    if positions:
        for pos in positions[:5]:  # 显示前 5 只
            print(f"   • {pos.get('code', 'N/A')} - {pos.get('qty', 0)} 股 @ {pos.get('cost_price', 0):.2f}")
        if len(positions) > 5:
            print(f"   ... 还有 {len(positions) - 5} 只持仓")
    
    # 成交统计
    print(f"\n📉 历史成交：{len(filled_orders)} 笔")
    if filled_orders:
        # 按股票统计
        stock_stats = {}
        for order in filled_orders:
            code = order.get('code', 'UNKNOWN')
            if code not in stock_stats:
                stock_stats[code] = {'count': 0, 'buy': 0, 'sell': 0}
            stock_stats[code]['count'] += 1
            if order.get('side') == 'BUY':
                stock_stats[code]['buy'] += 1
            else:
                stock_stats[code]['sell'] += 1
        
        print("\n   按股票统计:")
        for code, stats in sorted(stock_stats.items(), key=lambda x: x[1]['count'], reverse=True)[:10]:
            print(f"   • {code}: {stats['count']} 笔 (买:{stats['buy']} / 卖:{stats['sell']})")


def export_to_json(data, filename):
    """导出数据到 JSON 文件"""
    output_dir = Path('/opt/ftseek/FTSeek/reports')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 添加时间戳
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'{filename}_{timestamp}.json'
    
    # 序列化数据 (处理 datetime 对象)
    def json_serial(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=json_serial)
    
    print(f"\n💾 数据已导出：{output_file}")
    return output_file


def main():
    print("=" * 70)
    print("🚀 富途实盘账号历史交易记录查询")
    print("=" * 70)
    print(f"查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. 创建交易上下文
    print("\n🔌 连接富途 OpenD...")
    ctx = create_trade_context(trd_env=TrdEnv.REAL)
    
    # 2. 获取账户列表
    print("\n📋 获取实盘账户列表...")
    accounts = get_account_list(ctx, trd_env=TrdEnv.REAL)
    
    if not accounts:
        print("\n❌ 未找到实盘账户")
        print("\n可能的原因:")
        print("   1. OpenD 未配置实盘环境")
        print("   2. 未登录实盘账号")
        print("   3. 网络问题导致连接失败")
        ctx.close()
        return 1
    
    print(f"\n✅ 找到 {len(accounts)} 个实盘账户:")
    for acc in accounts:
        print(f"   • 账户 ID: {acc['acc_id']} | 类型：{acc['acc_type']} | 市场：{acc['trdmarket_auth']}")
    
    # 3. 选择账户 (默认使用第一个)
    if len(accounts) > 1:
        print("\n⚠️ 检测到多个账户，使用第一个账户")
    
    target_acc = accounts[0]
    acc_id = target_acc['acc_id']
    print(f"\n🎯 目标账户：{acc_id}")
    
    # 4. 解锁交易
    print("\n🔓 解锁交易...")
    success, msg = unlock_trade(ctx, acc_id)
    if not success:
        print("\n❌ 解锁失败，无法查询交易记录")
        ctx.close()
        return 1
    
    # 5. 获取账户资金
    print("\n💰 获取账户资金...")
    funds = get_account_funds(ctx, acc_id)
    
    # 6. 获取持仓
    print("\n📈 获取持仓列表...")
    positions = get_positions(ctx, acc_id)
    
    # 7. 获取历史成交
    print("\n📉 获取历史成交记录 (最近 30 天)...")
    filled_orders = get_filled_orders(ctx, acc_id, days=30)
    
    # 8. 获取历史订单
    print("\n📋 获取历史订单 (最近 30 天)...")
    history_orders = get_history_orders(ctx, acc_id, days=30)
    
    # 9. 打印摘要
    print_account_summary(acc_id, funds, positions, filled_orders)
    
    # 10. 导出数据
    export_data = {
        'query_time': datetime.now().isoformat(),
        'account_id': acc_id,
        'funds': funds,
        'positions': positions,
        'filled_orders': filled_orders,
        'history_orders': history_orders,
        'summary': {
            'total_positions': len(positions),
            'total_filled_orders': len(filled_orders),
            'total_history_orders': len(history_orders)
        }
    }
    
    export_to_json(export_data, 'futu_real_account_report')
    
    # 11. 关闭连接
    ctx.close()
    print("\n✅ 查询完成，连接已关闭")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
