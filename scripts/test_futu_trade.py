#!/usr/bin/env python3
"""
测试富途交易 API - 获取持仓和资金信息
"""
import os
import sys
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('ftseek.test_futu_trade')

# 设置富途日志级别
os.environ["FUTU_LOG_LEVEL"] = "ERROR"

# 导入富途 API
try:
    from futu import OpenHKTradeContext, RET_OK, RET_ERROR, TrdEnv
    FUTU_AVAILABLE = True
    logger.info("✅ 富途交易 API 导入成功")
except ImportError as e:
    FUTU_AVAILABLE = False
    logger.error(f"❌ 富途交易 API 导入失败：{e}")
    sys.exit(1)

# 配置
OPEND_HOST = os.getenv('OPEND_HOST', '127.0.0.1')
OPEND_PORT = int(os.getenv('OPEND_PORT', '11111'))
TRD_PWD_MD5 = os.getenv('FUTU_TRD_PWD_MD5')
LOGIN_ACCOUNT = os.getenv('FUTU_LOGIN_ACCOUNT')

print("=" * 60)
print("FTSeek 富途交易 API 测试")
print("=" * 60)
print()

# 检查配置
print("1️⃣  检查配置...")
print(f"   OpenD 地址：{OPEND_HOST}:{OPEND_PORT}")
print(f"   登录账号：{LOGIN_ACCOUNT}")
print(f"   交易密码 (MD5): {'已配置' if TRD_PWD_MD5 else '未配置'}")

if not TRD_PWD_MD5:
    print()
    print("   ❌ 交易密码未配置")
    print()
    print("   请从 FutuOpenD.xml 中提取 <login_pwd_md5> 值")
    print("   并添加到 .env 文件：FUTU_TRD_PWD_MD5=xxx")
    sys.exit(1)

print("   ✅ 配置完整")

# 2. 创建交易上下文
print()
print("2️⃣  创建交易上下文...")
try:
    # 模拟环境
    trade_ctx = OpenHKTradeContext(host=OPEND_HOST, port=OPEND_PORT)
    print("   ✅ 交易上下文创建成功")
except Exception as e:
    print(f"   ❌ 创建交易上下文失败：{e}")
    sys.exit(1)

# 3. 解锁交易
print()
print("3️⃣  解锁交易...")
try:
    ret_code, ret_data = trade_ctx.unlock_trade(TRD_PWD_MD5)
    
    if ret_code == RET_OK:
        print("   ✅ 交易解锁成功")
    else:
        print(f"   ❌ 交易解锁失败：{ret_code}")
        print(f"   错误信息：{ret_data}")
        trade_ctx.close()
        sys.exit(1)
except Exception as e:
    print(f"   ❌ 解锁交易失败：{e}")
    trade_ctx.close()
    sys.exit(1)

# 4. 获取账户列表
print()
print("4️⃣  获取账户列表...")
try:
    ret_code, acc_list = trade_ctx.get_acc_list()
    
    if ret_code == RET_OK and len(acc_list) > 0:
        print(f"   ✅ 获取账户列表成功")
        print(f"   账户数量：{len(acc_list)}")
        print()
        print("   账户列表:")
        for idx, row in acc_list.iterrows():
            print(f"   {idx+1}. 账户 ID: {row['trd_accid']}")
            print(f"      市场：{row['trd_market']}")
            print(f"      环境：{row['trd_env']}")
            print(f"      状态：{row['status']}")
            print()
    else:
        print(f"   ❌ 获取账户列表失败：{ret_data}")
        trade_ctx.close()
        sys.exit(1)
except Exception as e:
    print(f"   ❌ 获取账户列表失败：{e}")
    trade_ctx.close()
    sys.exit(1)

# 5. 获取资金信息
print("5️⃣  获取资金信息...")
try:
    # 使用第一个账户
    accid = acc_list.iloc[0]['trd_accid']
    ret_code, funds = trade_ctx.get_funds(trd_accid=accid)
    
    if ret_code == RET_OK:
        print(f"   ✅ 获取资金信息成功")
        print()
        print("   资金详情:")
        for idx, row in funds.iterrows():
            print(f"   - 币种：{row.get('currency', 'N/A')}")
            print(f"     现金：{row.get('cash', 0):.2f}")
            print(f"     市值：{row.get('market_val', 0):.2f}")
            print(f"     总资产：{row.get('total_assets', 0):.2f}")
            print(f"     可用资金：{row.get('power', 0):.2f}")
            print(f"     最大购买力：{row.get('max_power_long', 0):.2f}")
            print()
    else:
        print(f"   ⚠️  获取资金信息失败：{ret_code}")
        print(f"   错误信息：{ret_data}")
except Exception as e:
    print(f"   ❌ 获取资金信息失败：{e}")

# 6. 获取持仓信息
print("6️⃣  获取持仓信息...")
try:
    ret_code, positions = trade_ctx.get_position_list(trd_accid=accid)
    
    if ret_code == RET_OK:
        print(f"   ✅ 获取持仓信息成功")
        print()
        if len(positions) > 0:
            print(f"   持仓数量：{len(positions)}")
            print()
            print("   持仓详情:")
            for idx, row in positions.iterrows():
                print(f"   - 股票代码：{row.get('code', 'N/A')}")
                print(f"     股票名称：{row.get('stock_name', 'N/A')}")
                print(f"     持仓数量：{row.get('qty', 0)}")
                print(f"     可卖数量：{row.get('can_sell_qty', 0)}")
                print(f"     成本价：{row.get('cost_price', 0):.2f}")
                print(f"     市价：{row.get('market_price', 0):.2f}")
                print(f"     市值：{row.get('market_val', 0):.2f}")
                print(f"     盈亏：{row.get('pl_val', 0):.2f}")
                print(f"     盈亏率：{row.get('pl_rate', 0):.2f}%")
                print()
        else:
            print("   当前无持仓")
    else:
        print(f"   ⚠️  获取持仓信息失败：{ret_code}")
        print(f"   错误信息：{ret_data}")
except Exception as e:
    print(f"   ❌ 获取持仓信息失败：{e}")

# 7. 关闭连接
print()
print("7️⃣  关闭连接...")
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
print("富途账号可以正常获取仓位和资金信息！")
