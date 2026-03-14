#!/usr/bin/env python3
"""
富途 OpenD 连接测试 & 历史数据获取能力检查
检查账号可用性、权限、历史交易记录获取能力
"""
import sys
import os
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 设置富途日志级别
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"

from futu import OpenQuoteContext, OpenSecTradeContext, TrdEnv, TrdMarket

print("=" * 60)
print("富途 OpenD 连接测试 & 历史数据获取能力检查")
print("=" * 60)
print(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# ============================================================
# 1. 检查 OpenD 连接 (行情)
# ============================================================
print("【1】检查 OpenD 行情连接...")
print("-" * 60)

try:
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    print("✓ OpenD 行情连接成功 (127.0.0.1:11111)")
    
    # 测试获取行情
    ret, data = quote_ctx.get_market_snapshot(['HK.00700'])
    if ret == 0:
        print(f"✓ 港股行情获取成功：腾讯控股 现价=${data['last_price'].iloc[0]:.2f}")
    else:
        print(f"✗ 港股行情获取失败：{data}")
    
    # 测试美股权限
    ret, data = quote_ctx.get_user_info()
    if ret == 0:
        print("✓ 用户信息获取成功")
        # 检查权限列
        for col in data.columns:
            if 'qot' in col.lower() or 'right' in col.lower():
                print(f"  - {col}: {data.iloc[0][col]}")
    else:
        print(f"✗ 用户信息获取失败：{data}")
    
    quote_ctx.close()
    print()
    
except Exception as e:
    print(f"✗ OpenD 行情连接失败：{e}")
    print("  可能原因:")
    print("  1. FutuOpenD 未启动")
    print("  2. 端口 11111 被占用")
    print("  3. 配置文件 FutuOpenD.xml 有误")
    print()
    quote_ctx = None

# ============================================================
# 2. 检查交易 API 连接
# ============================================================
print("【2】检查交易 API 连接...")
print("-" * 60)

try:
    trd_ctx = OpenSecTradeContext(
        host='127.0.0.1',
        port=11111,
        filter_trdmarket=TrdMarket.HK
    )
    print("✓ 交易 API 连接成功")
    
    # 尝试获取账户信息
    ret, data = trd_ctx.get_acc_list()
    if ret == 0:
        print(f"✓ 账户列表获取成功，共 {len(data)} 个账户")
        for idx, row in data.iterrows():
            print(f"  账户{idx+1}:")
            print(f"    - 账户 ID: {row['acc_id']}")
            print(f"    - 市场：{row['trd_env']}")
            print(f"    - 货币：{row['currency']}")
    else:
        print(f"✗ 账户列表获取失败：{data}")
    
    trd_ctx.close()
    print()
    
except Exception as e:
    print(f"✗ 交易 API 连接失败：{e}")
    print("  可能原因:")
    print("  1. 需要在 OpenD 配置文件中进行交易确认")
    print("  2. 交易密码未正确配置")
    print("  3. 需要手动解锁交易账户")
    print()
    trd_ctx = None

# ============================================================
# 3. 检查历史订单获取能力
# ============================================================
print("【3】检查历史订单获取能力...")
print("-" * 60)

if trd_ctx is None:
    print("⚠ 交易 API 未连接，跳过历史订单测试")
    print()
else:
    try:
        # 重新连接 (上面的连接已关闭)
        trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111, filter_trdmarket=TrdMarket.HK)
        
        # 获取账户 ID
        ret, acc_data = trd_ctx.get_acc_list()
        if ret != 0 or len(acc_data) == 0:
            print("✗ 无法获取账户 ID，跳过历史订单测试")
        else:
            acc_id = acc_data.iloc[0]['acc_id']
            print(f"使用账户 ID: {acc_id}")
            
            # 尝试获取历史订单 (2026-01-01 至今)
            start_date = '2026-01-01'
            end_date = datetime.now().strftime('%Y-%m-%d')
            
            print(f"尝试获取历史订单：{start_date} 至 {end_date}")
            ret, data = trd_ctx.get_history_order_list(
                acc_id=acc_id,
                start=start_date,
                end=end_date
            )
            
            if ret == 0:
                print(f"✓ 历史订单获取成功，共 {len(data)} 条记录")
                if len(data) > 0:
                    print(f"  最早订单：{data['create_time'].min()}")
                    print(f"  最晚订单：{data['create_time'].max()}")
                    print(f"  订单状态分布:")
                    print(data['order_status'].value_counts())
                    print(f"  买卖分布:")
                    print(data['side'].value_counts())
            else:
                print(f"✗ 历史订单获取失败：{data}")
                print("  可能原因:")
                print("  1. 富途 API 不支持获取这么久远的历史订单")
                print("  2. 账户没有交易记录")
                print("  3. 需要调整查询时间范围")
            
            # 尝试获取更近期的订单 (最近 30 天)
            print()
            print("尝试获取最近 30 天订单...")
            start_date_30 = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            ret, data = trd_ctx.get_history_order_list(
                acc_id=acc_id,
                start=start_date_30,
                end=end_date
            )
            
            if ret == 0 and len(data) > 0:
                print(f"✓ 最近 30 天订单：{len(data)} 条")
            else:
                print(f"  最近 30 天订单：0 条或获取失败")
        
        trd_ctx.close()
        print()
        
    except Exception as e:
        print(f"✗ 历史订单测试失败：{e}")
        print()

# ============================================================
# 4. 检查持仓和盈亏数据
# ============================================================
print("【4】检查持仓和盈亏数据...")
print("-" * 60)

if trd_ctx is None:
    print("⚠ 交易 API 未连接，跳过持仓测试")
    print()
else:
    try:
        trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111, filter_trdmarket=TrdMarket.HK)
        
        ret, acc_data = trd_ctx.get_acc_list()
        if ret == 0 and len(acc_data) > 0:
            acc_id = acc_data.iloc[0]['acc_id']
            
            # 获取当前持仓
            ret, data = trd_ctx.get_position_list(acc_id=acc_id)
            if ret == 0:
                print(f"✓ 当前持仓获取成功，共 {len(data)} 只股票")
                if len(data) > 0:
                    for idx, row in data.iterrows():
                        print(f"  - {row['code']} {row['name']}: {row['qty']}股, "
                              f"盈亏={row['pl_ratio']:.2f}%")
            else:
                print(f"✗ 持仓获取失败：{data}")
            
            # 获取账户资金
            ret, data = trd_ctx.get_acc_funds(acc_id=acc_id)
            if ret == 0:
                print(f"✓ 账户资金获取成功")
                print(f"  - 总资产：{data['total_asset'].iloc[0]:.2f}")
                print(f"  - 现金：{data['cash'].iloc[0]:.2f}")
                print(f"  - 市值：{data['market_val'].iloc[0]:.2f}")
                print(f"  - 盈亏：{data['pnl'].iloc[0]:.2f}")
            else:
                print(f"✗ 账户资金获取失败：{data}")
        
        trd_ctx.close()
        print()
        
    except Exception as e:
        print(f"✗ 持仓测试失败：{e}")
        print()

# ============================================================
# 5. 总结和建议
# ============================================================
print("=" * 60)
print("【总结与建议】")
print("=" * 60)
print()

if quote_ctx is not None:
    print("✓ 行情 API 可用 - 可以获取实时行情和历史 K 线")
else:
    print("✗ 行情 API 不可用 - 需要启动 FutuOpenD")

if trd_ctx is not None:
    print("✓ 交易 API 可用 - 可以获取持仓、订单、账户信息")
else:
    print("✗ 交易 API 不可用 - 需要配置交易确认")

print()
print("关于历史数据回测:")
print("1. 富途 API 支持获取历史订单，但时间范围可能有限制")
print("2. 建议查询范围：最近 30 天 -90 天")
print("3. 如需更长期数据，建议:")
print("   - 导出 CSV 进行离线分析")
print("   - 使用富途牛牛客户端的历史对账单功能")
print()
print("关于回测分析:")
print("1. 可以使用获取的历史订单进行策略回测")
print("2. 结合 K 线数据可以计算策略表现")
print("3. V4 模块将包含简化的回测功能 (Task 5.2)")
print()
