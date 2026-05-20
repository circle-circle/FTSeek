#!/usr/bin/env python3
"""
FTSeek-US 期权持仓清理脚本 (实盘版)
平仓所有期权持仓，确保仅保留正股交易

⚠️ 警告：此脚本操作实盘账户，请谨慎使用!

用法:
    python3 scripts/close_all_options_real.py
"""

import sys
import os
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek-US')
os.environ.setdefault('FUTU_HOST', '127.0.0.1')
os.environ.setdefault('FUTU_PORT', '11111')

from futu import *


def create_us_trade_context(trd_env=TrdEnv.REAL):
    """创建美股交易上下文"""
    ctx = OpenUSTradeContext(
        host=os.getenv('FUTU_HOST', '127.0.0.1'),
        port=int(os.getenv('FUTU_PORT', '11111'))
    )
    return ctx


def get_account_list(ctx, trd_env=TrdEnv.REAL):
    """获取账户列表"""
    ret, acc_list = ctx.get_acc_list()
    if ret != RET_OK:
        print(f"❌ 获取账户列表失败：{acc_list}")
        return []
    
    # 筛选账户
    accounts = []
    for idx, row in acc_list.iterrows():
        accounts.append({
            'acc_id': row['acc_id'],
            'acc_type': row['acc_type'],
            'trd_env': row['trd_env']
        })
    
    return accounts


def unlock_trade(ctx, acc_id, trd_env=TrdEnv.REAL):
    """解锁交易 - 交互式输入密码"""
    print("\n🔐 需要交易密码解锁账户")
    print(f"   账户：{acc_id}")
    print(f"   环境：{'实盘' if trd_env == TrdEnv.REAL else '模拟'}")
    print("\n⚠️  警告：这是实盘账户，操作将产生真实交易!\n")
    
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


def get_option_positions(ctx, acc_id, trd_env=TrdEnv.REAL):
    """获取期权持仓"""
    ret, positions = ctx.get_position_list(trd_env=trd_env, acc_id=acc_id)
    if ret != RET_OK:
        print(f"❌ 获取持仓失败：{positions}")
        return []
    
    # 筛选期权持仓 (期权代码包含日期，如 US.NVDA260410P00170000)
    option_positions = []
    for idx, row in positions.iterrows():
        code = row.get('code', '')
        # 期权代码特征：包含日期 (6 位数字) + Call/Put 标识
        if any(c.isdigit() for c in code) and len(code) > 15:
            option_positions.append({
                'code': code,
                'qty': row.get('qty', 0),
                'side': row.get('side', 'LONG'),
                'cost_price': row.get('cost_price', 0),
                'market_price': row.get('market_price', 0),
                'market_val': row.get('market_val', 0),
                'pnl': row.get('pl', 0)
            })
    
    return option_positions


def close_option_position(ctx, acc_id, code, qty, side, trd_env=TrdEnv.REAL):
    """平仓单个期权持仓"""
    # 确定平仓方向 (与持仓相反)
    action = OrderType.SELL if side == 'BUY' or side == 'LONG' else OrderType.BUY
    
    # 提交订单
    ret, data = ctx.place_order(
        trd_env=trd_env,
        acc_id=acc_id,
        code=code,
        qty=abs(qty),
        side=action,
        order_type=OrderType.MARKET,  # 市价单快速平仓
        price=0,  # 市价单价格为 0
        adjust_limit=0,
        trd_side=TrdSide.SELL if action == OrderType.SELL else TrdSide.BUY
    )
    
    if ret != RET_OK:
        print(f"  ❌ 平仓失败 {code}: {data}")
        return False, data
    
    order_id = data.get('order_id', 'N/A')
    print(f"  ✅ 已提交平仓订单 {code} × {qty} | 订单 ID: {order_id}")
    return True, order_id


def close_all_options(ctx, acc_id, trd_env=TrdEnv.REAL):
    """平仓所有期权持仓"""
    print("\n📊 获取期权持仓...")
    positions = get_option_positions(ctx, acc_id, trd_env)
    
    if not positions:
        print("✅ 无期权持仓，无需平仓")
        return {
            'total': 0,
            'success': 0,
            'failed': 0,
            'positions': []
        }
    
    print(f"\n📉 检测到 {len(positions)} 个期权持仓:\n")
    
    # 显示持仓详情
    total_val = 0
    total_pnl = 0
    for i, pos in enumerate(positions, 1):
        pnl = pos.get('pnl', 0)
        pnl_symbol = '+' if pnl >= 0 else ''
        val = pos.get('market_val', 0)
        total_val += val
        total_pnl += pnl
        print(f"  {i}. {pos['code']}")
        print(f"     数量：{pos['qty']} | 成本：${pos['cost_price']:.2f} | 市价：${pos['market_price']:.2f}")
        print(f"     市值：${val:.2f} | 盈亏：{pnl_symbol}${pnl:.2f}")
        print()
    
    print(f"📊 持仓总计：市值 ${total_val:.2f} | 盈亏 ${total_pnl:+.2f}")
    
    # 确认平仓
    print(f"\n⚠️  即将平仓 {len(positions)} 个期权持仓 (实盘交易!)...")
    confirm = input("确认平仓？输入 'YES' 确认 (大小写敏感): ").strip()
    if confirm != 'YES':
        print("❌ 用户取消平仓操作")
        return None
    
    # 执行平仓
    print("\n🔄 开始平仓...\n")
    results = {
        'total': len(positions),
        'success': 0,
        'failed': 0,
        'positions': []
    }
    
    for pos in positions:
        success, order_id = close_option_position(
            ctx, acc_id,
            pos['code'], pos['qty'], pos['side'],
            trd_env
        )
        
        if success:
            results['success'] += 1
        else:
            results['failed'] += 1
        
        results['positions'].append({
            'code': pos['code'],
            'qty': pos['qty'],
            'success': success,
            'order_id': order_id
        })
    
    return results


def export_report(results, acc_id):
    """导出平仓报告"""
    output_dir = Path('/opt/ftseek/FTSeek-US/reports')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'options_close_report_{timestamp}.json'
    
    import json
    report = {
        'timestamp': datetime.now().isoformat(),
        'account_id': acc_id,
        'environment': 'REAL',
        'action': 'close_all_options',
        'reason': '停止期权交易，仅保留正股',
        'results': results
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 平仓报告已保存：{output_file}")
    return output_file


def main():
    print("=" * 70)
    print("🚫 FTSeek-US 期权持仓清理 (实盘)")
    print("=" * 70)
    print(f"操作时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"目标：平仓所有期权持仓，确保仅保留正股交易\n")
    
    # 1. 创建交易上下文 (实盘环境)
    print("🔌 连接富途 OpenD...")
    trd_env = TrdEnv.REAL
    ctx = create_us_trade_context(trd_env=trd_env)
    print(f"⚠️  使用 REAL 实盘环境\n")
    
    # 2. 获取账户列表
    print("📋 获取账户列表...")
    accounts = get_account_list(ctx, trd_env=trd_env)
    
    if not accounts:
        print("\n❌ 未找到可用账户")
        ctx.close()
        return 1
    
    print(f"\n✅ 找到 {len(accounts)} 个账户:")
    for acc in accounts:
        print(f"   • 账户 ID: {acc['acc_id']} | 类型：{acc['acc_type']} | 环境：{acc['trd_env']}")
    
    # 3. 选择账户 (默认使用第一个)
    target_acc = accounts[0]
    acc_id = target_acc['acc_id']
    print(f"\n🎯 目标账户：{acc_id} ({target_acc['acc_type']})")
    
    # 4. 解锁交易 (交互式输入密码)
    print("\n🔓 解锁交易...")
    success, msg = unlock_trade(ctx, acc_id, trd_env)
    if not success:
        print("\n❌ 解锁失败，无法执行平仓")
        ctx.close()
        return 1
    
    # 5. 平仓所有期权
    print("\n📉 检查期权持仓...")
    results = close_all_options(ctx, acc_id, trd_env)
    
    if results is None:
        print("\n❌ 操作取消")
        ctx.close()
        return 1
    
    # 6. 显示结果
    print("\n" + "=" * 70)
    print("📊 平仓结果汇总")
    print("=" * 70)
    print(f"总持仓数：{results['total']}")
    print(f"✅ 成功：{results['success']}")
    print(f"❌ 失败：{results['failed']}")
    
    if results['success'] > 0:
        print(f"\n✅ 期权持仓清理完成!")
        export_report(results, acc_id)
    else:
        print(f"\n⚠️  无期权持仓或平仓失败")
    
    # 7. 关闭连接
    ctx.close()
    print("\n✅ 连接已关闭")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
