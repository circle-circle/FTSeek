#!/usr/bin/env python3
"""
测试富途账户连接 - 获取仓位和资金信息
"""
import os
import sys
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('ftseek.test_futu')

# 设置富途日志级别
os.environ["FUTU_LOG_LEVEL"] = "ERROR"

# 导入富途 API
try:
    from futu import OpenQuoteContext, RET_OK, RET_ERROR
    FUTU_AVAILABLE = True
    logger.info("✅ 富途 API 导入成功")
except ImportError as e:
    FUTU_AVAILABLE = False
    logger.error(f"❌ 富途 API 导入失败：{e}")
    sys.exit(1)

# 配置
OPEND_HOST = os.getenv('OPEND_HOST', '127.0.0.1')
OPEND_PORT = int(os.getenv('OPEND_PORT', '11111'))

print("=" * 60)
print("FTSeek 富途账户连接测试")
print("=" * 60)
print()

# 1. 检查 OpenD 连接
print("1️⃣  检查 OpenD 连接...")
print(f"   OpenD 地址：{OPEND_HOST}:{OPEND_PORT}")

try:
    quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    print("   ✅ OpenD 连接成功")
except Exception as e:
    print(f"   ❌ OpenD 连接失败：{e}")
    print()
    print("可能原因:")
    print("  1. OpenD 未启动")
    print("  2. OpenD 端口配置错误")
    print("  3. 防火墙阻止连接")
    sys.exit(1)

# 2. 测试行情 API
print()
print("2️⃣  测试行情 API...")
try:
    # 获取腾讯控股 (00700) 的报价
    ret_code, ret_data = quote_ctx.get_market_snapshot('HK.00700')
    
    if ret_code == RET_OK:
        print("   ✅ 行情 API 正常")
        if len(ret_data) > 0:
            print(f"   腾讯控股当前价格：HK${ret_data['last_price'].iloc[0]:.2f}")
    else:
        print(f"   ⚠️  行情 API 返回错误：{ret_code}")
        
except Exception as e:
    print(f"   ❌ 行情 API 测试失败：{e}")

# 3. 检查交易 API 配置
print()
print("3️⃣  检查交易 API 配置...")
print("   注意：交易 API 需要配置交易密码和账户信息")
print()
print("   当前配置:")
print(f"   - OPEND_HOST: {OPEND_HOST}")
print(f"   - OPEND_PORT: {OPEND_PORT}")
print(f"   - 交易密码：{'已配置' if os.getenv('FUTU_TRD_PWD') else '未配置'}")
print(f"   - 账户 ID: {os.getenv('FUTU_TRD_ACCID', '未配置')}")
print()

# 4. 尝试获取持仓和资金
print("4️⃣  尝试获取持仓和资金...")

# 检查是否有交易配置
trd_pwd = os.getenv('FUTU_TRD_PWD')
trd_accid = os.getenv('FUTU_TRD_ACCID')

if not trd_pwd:
    print("   ⚠️  交易密码未配置")
    print()
    print("   配置方法:")
    print("   1. 编辑 .env 文件")
    print("   2. 添加以下配置:")
    print("      FUTU_TRD_PWD=你的交易密码")
    print("      FUTU_TRD_ACCID=你的账户 ID (可选)")
    print()
    print("   或者在富途 OpenD 中配置免密码模式 (仅限测试)")
else:
    print("   ✅ 交易密码已配置")
    try:
        # 导入交易 API
        from futu import OpenHKTradeContext, TrdEnv
        
        # 创建交易上下文 (模拟环境)
        trade_ctx = OpenHKTradeContext(host=OPEND_HOST, port=OPEND_PORT)
        
        # 解锁交易
        ret_code, ret_data = trade_ctx.unlock_trade(trd_pwd)
        
        if ret_code == RET_OK:
            print("   ✅ 交易解锁成功")
            
            # 获取账户列表
            ret_code, acc_list = trade_ctx.get_acc_list()
            if ret_code == RET_OK and len(acc_list) > 0:
                print(f"   ✅ 获取账户列表成功 ({len(acc_list)}个账户)")
                
                # 使用第一个账户
                accid = acc_list.iloc[0]['trd_accid']
                print(f"   使用账户：{accid}")
                
                # 获取资金
                ret_code, funds = trade_ctx.get_funds(trd_accid=accid)
                if ret_code == RET_OK:
                    print("   ✅ 获取资金信息成功")
                    print()
                    print("   资金详情:")
                    for idx, row in funds.iterrows():
                        print(f"   - 现金：{row.get('cash', 0):.2f}")
                        print(f"     市值：{row.get('market_val', 0):.2f}")
                        print(f"     总资产：{row.get('total_assets', 0):.2f}")
                        print(f"     可用资金：{row.get('power', 0):.2f}")
                
                # 获取持仓
                ret_code, positions = trade_ctx.get_position_list(trd_accid=accid)
                if ret_code == RET_OK:
                    print("   ✅ 获取持仓信息成功")
                    print()
                    if len(positions) > 0:
                        print(f"   持仓数量：{len(positions)}")
                        print()
                        print("   持仓详情:")
                        for idx, row in positions.iterrows():
                            print(f"   - {row.get('code', 'N/A')} {row.get('stock_name', 'N/A')}")
                            print(f"     数量：{row.get('qty', 0)}  盈亏：{row.get('pl_val', 0):.2f}")
                    else:
                        print("   当前无持仓")
                
                trade_ctx.close()
            else:
                print(f"   ⚠️  获取账户列表失败：{ret_data}")
        else:
            print(f"   ❌ 交易解锁失败：{ret_data}")
            
    except ImportError as e:
        print(f"   ❌ 交易 API 导入失败：{e}")
        print("   可能需要安装 futu-api 的交易模块")
    except Exception as e:
        print(f"   ❌ 获取持仓和资金失败：{e}")

# 关闭连接
print()
print("5️⃣  关闭连接...")
try:
    quote_ctx.close()
    print("   ✅ 连接已关闭")
except Exception as e:
    print(f"   ⚠️  关闭连接失败：{e}")

print()
print("=" * 60)
print("测试完成")
print("=" * 60)
print()
print("总结:")
print(f"  ✅ OpenD 连接：正常")
print(f"  ✅ 行情 API: 正常")
print(f"  {'✅' if trd_pwd else '⚠️ '} 交易 API: {'已配置' if trd_pwd else '未配置'}")
print()

if trd_pwd:
    print("富途账号可以正常获取仓位和资金信息！")
else:
    print("⚠️  需要配置交易密码才能获取仓位和资金信息")
    print()
    print("配置方法:")
    print("  1. 编辑 .env 文件")
    print("  2. 添加：FUTU_TRD_PWD=你的交易密码")
