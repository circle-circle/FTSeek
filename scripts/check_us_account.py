#!/usr/bin/env python3
"""
美股模拟账户检测与配置脚本
自动检测新开通的美股模拟账户并更新 FTSeek 配置
"""

import json
import sys
import os
from pathlib import Path
from datetime import datetime

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from futu import *


def get_us_simulate_accounts():
    """获取所有美股模拟账户
    
    注意: 美股账户需要使用 OpenUSTradeContext 获取，
    OpenSecTradeContext 只能获取港股账户。
    """
    # 使用美股交易上下文
    us_ctx = OpenUSTradeContext(
        host='127.0.0.1', 
        port=11111
    )
    
    ret, acc_list = us_ctx.get_acc_list()
    us_ctx.close()
    
    if ret != RET_OK:
        print(f"❌ 获取账户列表失败: {acc_list}")
        return []
    
    # 筛选美股模拟账户
    us_accounts = []
    for idx, row in acc_list.iterrows():
        if row['trd_env'] == 'SIMULATE':
            markets = row['trdmarket_auth']
            # 检查是否有美股权限
            if 'US' in markets:
                us_accounts.append({
                    'acc_id': row['acc_id'],
                    'acc_type': row['acc_type'],
                    'sim_acc_type': row.get('sim_acc_type', 'N/A'),
                    'markets': markets,
                    'status': row['acc_status']
                })
    
    return us_accounts


def get_account_funds(acc_id):
    """获取账户资金"""
    trd_ctx = OpenSecTradeContext(
        host='127.0.0.1', 
        port=11111, 
        security_firm=SecurityFirm.FUTUSECURITIES
    )
    
    # 切换到指定账户
    ret, data = trd_ctx.unlock_trade(password='123456')  # 模拟账户密码通常是 123456
    if ret != RET_OK:
        print(f"⚠️ 解锁交易失败: {data}")
        trd_ctx.close()
        return None
    
    # 获取账户资金
    ret, funds = trd_ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=int(acc_id))
    trd_ctx.close()
    
    if ret != RET_OK:
        print(f"⚠️ 获取资金信息失败: {funds}")
        return None
    
    return funds.to_dict('records')[0] if len(funds) > 0 else None


def update_ftseek_config(us_accounts):
    """更新 FTSeek 配置"""
    config_path = Path(__file__).parent.parent / 'config' / 'strategy_params.json'
    
    if not config_path.exists():
        print(f"⚠️ 配置文件不存在: {config_path}")
        return False
    
    # 读取配置
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    # 选择第一个可用的美股账户
    if us_accounts:
        primary = us_accounts[0]
        config['us_account'] = {
            'acc_id': str(primary['acc_id']),
            'acc_type': primary['acc_type'],
            'enabled': True,
            'detected_at': datetime.now().isoformat()
        }
        print(f"\n✅ 已更新配置: 使用账户 {primary['acc_id']}")
    else:
        config['us_account'] = {
            'acc_id': None,
            'acc_type': None,
            'enabled': False,
            'detected_at': None
        }
        print("\n⚠️ 未检测到美股账户，配置已更新为禁用状态")
    
    # 保存配置
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    
    return True


def update_watchlist_for_us():
    """更新监控列表添加美股标的"""
    watchlist_path = Path(__file__).parent.parent / 'config' / 'watchlist.json'
    
    if not watchlist_path.exists():
        print(f"⚠️ 监控列表不存在: {watchlist_path}")
        return False
    
    with open(watchlist_path, 'r', encoding='utf-8') as f:
        watchlist = json.load(f)
    
    # 检查是否已有美股配置
    if 'us_stocks' in watchlist:
        print("✅ 监控列表已包含美股配置")
        return True
    
    # 添加默认美股监控
    watchlist['us_stocks'] = {
        'enabled': True,
        'symbols': [
            {
                'code': 'US.AAPL',
                'name': 'Apple',
                'lot_size': 1,
                'min_trade': 1
            },
            {
                'code': 'US.NVDA',
                'name': 'NVIDIA',
                'lot_size': 1,
                'min_trade': 1
            },
            {
                'code': 'US.TSLA',
                'name': 'Tesla',
                'lot_size': 1,
                'min_trade': 1
            },
            {
                'code': 'US.AMZN',
                'name': 'Amazon',
                'lot_size': 1,
                'min_trade': 1
            }
        ]
    }
    
    with open(watchlist_path, 'w', encoding='utf-8') as f:
        json.dump(watchlist, f, indent=2, ensure_ascii=False)
    
    print("✅ 已添加默认美股监控列表: AAPL, NVDA, TSLA, AMZN")
    return True


def main():
    print("=" * 60)
    print("🇺🇸 FTSeek 美股模拟账户检测与配置")
    print("=" * 60)
    print(f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # 1. 检测美股账户
    print("📡 正在检测美股模拟账户...")
    us_accounts = get_us_simulate_accounts()
    
    if not us_accounts:
        print("\n❌ 未检测到美股模拟账户")
        print("\n📋 开通步骤:")
        print("   1. 打开富途牛牛客户端")
        print("   2. 进入「交易」→「模拟交易」")
        print("   3. 点击「开通模拟账户」")
        print("   4. 选择「美股现金账户」或「美股融资融券账户」")
        print("   5. 开通后重新运行本脚本")
        
        # 更新配置为禁用状态
        update_ftseek_config([])
        return 1
    
    # 2. 显示检测到的账户
    print(f"\n✅ 检测到 {len(us_accounts)} 个美股模拟账户:\n")
    print("-" * 60)
    
    for i, acc in enumerate(us_accounts, 1):
        print(f"账户 {i}:")
        print(f"  📌 账户ID: {acc['acc_id']}")
        print(f"  📌 类型: {acc['acc_type']}")
        print(f"  📌 模拟类型: {acc['sim_acc_type']}")
        print(f"  📌 市场权限: {', '.join(acc['markets'])}")
        print(f"  📌 状态: {acc['status']}")
        
        # 尝试获取资金信息
        try:
            funds = get_account_funds(acc['acc_id'])
            if funds:
                print(f"  💰 总资产: {funds.get('total_assets', 'N/A')}")
                print(f"  💰 可用资金: {funds.get('available_funds', 'N/A')}")
        except Exception as e:
            print(f"  ⚠️ 资金信息获取失败: {e}")
        
        print("-" * 60)
    
    # 3. 更新配置
    print("\n⚙️ 正在更新 FTSeek 配置...")
    update_ftseek_config(us_accounts)
    
    # 4. 更新监控列表
    print("\n📋 正在更新监控列表...")
    update_watchlist_for_us()
    
    # 5. 显示配置摘要
    print("\n" + "=" * 60)
    print("✅ 配置完成!")
    print("=" * 60)
    print("\n📊 配置摘要:")
    print(f"   美股账户ID: {us_accounts[0]['acc_id']}")
    print(f"   账户类型: {us_accounts[0]['acc_type']}")
    print("   监控美股: AAPL, NVDA, TSLA, AMZN")
    print("\n🚀 下一步:")
    print("   1. 运行 python scripts/signal_monitor.py 启动监控")
    print("   2. 或重启 FTSeek 服务以应用新配置")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())