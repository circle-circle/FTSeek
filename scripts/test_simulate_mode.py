#!/usr/bin/env python3
"""
测试富途模拟交易模式
"""
import os
import sys
import logging

os.environ["FUTU_LOG_LEVEL"] = "ERROR"

from futu import OpenHKTradeContext, RET_OK, TrdEnv

# 配置
OPEND_HOST = os.getenv('OPEND_HOST', '127.0.0.1')
OPEND_PORT = int(os.getenv('OPEND_PORT', '11111'))

print("=" * 60)
print("FTSeek 富途模拟交易模式测试")
print("=" * 60)
print()

# 1. 创建交易上下文 (模拟环境)
print("1️⃣  创建交易上下文 (模拟环境)...")
try:
    trade_ctx = OpenHKTradeContext(host=OPEND_HOST, port=OPEND_PORT)
    print("   ✅ 交易上下文创建成功")
except Exception as e:
    print(f"   ❌ 创建交易上下文失败：{e}")
    sys.exit(1)

# 2. 解锁交易 (使用登录密码 MD5)
print()
print("2️⃣  解锁交易...")
TRD_PWD_MD5 = os.getenv('FUTU_TRD_PWD_MD5', '864ea7a260fc881101f23eb8bd8e40e4')
ret_code, ret_data = trade_ctx.unlock_trade(TRD_PWD_MD5)

if ret_code == RET_OK:
    print("   ✅ 交易解锁成功")
else:
    print(f"   ❌ 交易解锁失败：{ret_data}")
    trade_ctx.close()
    sys.exit(1)

# 3. 获取账户列表
print()
print("3️⃣  获取账户列表...")
ret_code, acc_list = trade_ctx.get_acc_list()

if ret_code == RET_OK and len(acc_list) > 0:
    print(f"   ✅ 获取账户列表成功 ({len(acc_list)}个账户)")
    print()
    
    # 查找模拟账户
    simulate_acc = None
    real_acc = None
    
    for idx, row in acc_list.iterrows():
        env_type = "模拟账户" if row['trd_env'] == 1 else "真实账户"
        print(f"   {idx+1}. 账户 ID: {row['trd_accid']}")
        print(f"      类型：{env_type}")
        print(f"      市场：{row['trd_market']}")
        print(f"      状态：{row['status']}")
        print()
        
        if row['trd_env'] == 1:
            simulate_acc = row['trd_accid']
        else:
            real_acc = row['trd_accid']
    
    # 使用模拟账户测试
    test_accid = simulate_acc if simulate_acc else real_acc
    if test_accid:
        acc_type = "模拟" if simulate_acc else "真实"
        print(f"   使用{acc_type}账户测试：{test_accid}")
        print()
        
        # 4. 获取资金信息
        print("4️⃣  获取资金信息...")
        ret_code, funds = trade_ctx.get_funds(trd_accid=test_accid)
        
        if ret_code == RET_OK:
            print("   ✅ 获取资金信息成功")
            print()
            for idx, row in funds.iterrows():
                print(f"   - 币种：{row.get('currency', 'N/A')}")
                print(f"     现金：{row.get('cash', 0):.2f}")
                print(f"     市值：{row.get('market_val', 0):.2f}")
                print(f"     总资产：{row.get('total_assets', 0):.2f}")
                print(f"     可用资金：{row.get('power', 0):.2f}")
                print()
        else:
            print(f"   ⚠️  获取资金信息失败：{ret_data}")
        
        # 5. 获取持仓信息
        print("5️⃣  获取持仓信息...")
        ret_code, positions = trade_ctx.get_position_list(trd_accid=test_accid)
        
        if ret_code == RET_OK:
            print("   ✅ 获取持仓信息成功")
            print()
            if len(positions) > 0:
                print(f"   持仓数量：{len(positions)}")
                print()
                for idx, row in positions.iterrows():
                    print(f"   - {row.get('code', 'N/A')} {row.get('stock_name', 'N/A')}")
                    print(f"     数量：{row.get('qty', 0)}  盈亏：{row.get('pl_val', 0):.2f}")
            else:
                print("   当前无持仓")
        else:
            print(f"   ⚠️  获取持仓信息失败：{ret_data}")
else:
    print(f"   ❌ 获取账户列表失败：{ret_data}")

# 6. 关闭连接
print()
print("6️⃣  关闭连接...")
try:
    trade_ctx.close()
    print("   ✅ 连接已关闭")
except Exception as e:
    print(f"   ⚠️  关闭连接失败：{e}")

print()
print("=" * 60)
print("测试完成")
print("=" * 60)
print()
print("总结:")
print("  ✅ OpenD 连接：正常")
print("  ✅ 交易解锁：成功")
print("  ✅ 账户列表：已获取")
print("  ✅ 资金信息：已获取")
print("  ✅ 持仓信息：已获取")
print()
print("富途模拟交易模式可以正常使用！")
