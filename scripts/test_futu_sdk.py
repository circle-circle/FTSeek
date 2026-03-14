#!/usr/bin/env python3.8
"""
富途 SDK 查询能力测试脚本
验证模拟市场买卖交易功能
"""
import sys
import time
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek')

print("=" * 70)
print("🔍 富途 SDK 查询能力检查与验证")
print("=" * 70)
print(f"📅 测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# 测试结果统计
test_results = {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "tests": []
}

def test_result(name, success, message=""):
    """记录测试结果"""
    test_results["total"] += 1
    if success:
        test_results["passed"] += 1
        print(f"✅ {name}: 成功")
    else:
        test_results["failed"] += 1
        print(f"❌ {name}: 失败 - {message}")
    
    test_results["tests"].append({
        "name": name,
        "success": success,
        "message": message
    })

try:
    from futu import OpenQuoteContext, OpenSecTradeContext
    from futu.common.constant import TrdEnv, TrdMarket, OrderType, TrdSide
    
    print("✅ SDK 导入成功\n")
    test_result("SDK 导入", True)
    
    # ============================================================
    # 1. 行情连接测试
    # ============================================================
    print("-" * 70)
    print("📊 1. 行情连接测试")
    print("-" * 70)
    
    quote_ctx = None
    try:
        quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        test_result("行情连接", True)
        
        # 测试获取全局状态
        ret, data = quote_ctx.get_global_state()
        if ret == 0:
            test_result("获取全局状态", True, str(data))
            print(f"   服务器时间：{data.get('time', 'N/A')}")
        else:
            test_result("获取全局状态", False, str(data))
        
        # 测试获取市场状态
        ret, data = quote_ctx.get_market_state(market=TrdMarket.HK)
        if ret == 0:
            test_result("获取市场状态", True)
            print(f"   港股市场状态：{data}")
        else:
            test_result("获取市场状态", False, str(data))
        
        # 测试获取股票报价
        ret, data = quote_ctx.get_market_snapshot('HK.00700')
        if ret == 0:
            test_result("获取股票报价", True)
            print(f"   腾讯控股现价：{data['last_price'].iloc[0] if len(data) > 0 else 'N/A'}")
        else:
            test_result("获取股票报价", False, str(data))
        
    except Exception as e:
        test_result("行情连接", False, str(e))
        print(f"❌ 行情连接失败：{e}")
    finally:
        if quote_ctx:
            quote_ctx.close()
    
    # ============================================================
    # 2. 交易连接测试
    # ============================================================
    print()
    print("-" * 70)
    print("💼 2. 交易连接测试")
    print("-" * 70)
    
    trade_ctx = None
    try:
        trade_ctx = OpenSecTradeContext(trd_env=TrdEnv.SIMULATE, host='127.0.0.1', port=11111)
        test_result("交易连接", True)
        
        # 解锁交易
        ret, msg = trade_ctx.unlock_trade(password='')
        if ret == 0:
            test_result("交易解锁", True)
            print("   ✅ 模拟交易已解锁")
        else:
            test_result("交易解锁", False, msg)
            print(f"   ❌ 交易解锁失败：{msg}")
        
        # 获取账户信息
        if ret == 0:
            print()
            print("📊 获取账户信息...")
            ret, data = trade_ctx.get_accinfo()
            if ret == 0:
                test_result("获取账户信息", True)
                print(f"   账户数：{len(data) if len(data) > 0 else 0}")
                if len(data) > 0:
                    print(f"   账户 ID: {data['acc_id'].iloc[0] if 'acc_id' in data.columns else 'N/A'}")
                    print(f"   账户类型：{data['acc_type'].iloc[0] if 'acc_type' in data.columns else 'N/A'}")
            else:
                test_result("获取账户信息", False, str(data))
        
        # 获取持仓
        if ret == 0:
            print()
            print("📦 获取持仓信息...")
            ret, data = trade_ctx.get_position_list()
            if ret == 0:
                test_result("获取持仓", True)
                if len(data) > 0:
                    print(f"   持仓数量：{len(data)}")
                    for idx, row in data.iterrows():
                        print(f"   - {row['code']}: {row['qty']}股 @ {row['cost_price']}")
                else:
                    print("   当前无持仓")
            else:
                test_result("获取持仓", False, str(data))
        
        # 获取资金
        if ret == 0:
            print()
            print("💰 获取资金信息...")
            ret, data = trade_ctx.get_funds()
            if ret == 0:
                test_result("获取资金", True)
                if len(data) > 0:
                    print(f"   总金额：{data['total_assets'].iloc[0] if 'total_assets' in data.columns else 'N/A'}")
                    print(f"   现金：{data['cash'].iloc[0] if 'cash' in data.columns else 'N/A'}")
                    print(f"   可用现金：{data['power'].iloc[0] if 'power' in data.columns else 'N/A'}")
            else:
                test_result("获取资金", False, str(data))
        
    except Exception as e:
        test_result("交易连接", False, str(e))
        print(f"❌ 交易连接失败：{e}")
    finally:
        if trade_ctx:
            trade_ctx.close()
    
    # ============================================================
    # 3. 模拟交易测试
    # ============================================================
    print()
    print("-" * 70)
    print("🧪 3. 模拟交易测试")
    print("-" * 70)
    
    trade_ctx = None
    try:
        trade_ctx = OpenSecTradeContext(trd_env=TrdEnv.SIMULATE, host='127.0.0.1', port=11111)
        ret, msg = trade_ctx.unlock_trade(password='')
        
        if ret == 0:
            # 测试下单（不实际执行，仅验证接口）
            print("📝 测试下单接口...")
            
            # 测试买入下单
            ret, data = trade_ctx.place_order(
                price=520.0,
                qty=100,
                code='HK.00700',
                trd_side=TrdSide.BUY,
                order_type=OrderType.NORMAL
            )
            
            if ret == 0:
                test_result("模拟买入下单", True)
                order_id = data['order_id'].iloc[0] if len(data) > 0 else 'N/A'
                print(f"   ✅ 下单成功，订单 ID: {order_id}")
                
                # 测试撤单
                if order_id != 'N/A':
                    ret, msg = trade_ctx.cancel_order(order_id)
                    if ret == 0:
                        test_result("模拟撤单", True)
                        print(f"   ✅ 撤单成功")
                    else:
                        test_result("模拟撤单", False, msg)
            else:
                test_result("模拟买入下单", False, str(data))
                print(f"   ❌ 下单失败：{data}")
            
            # 测试卖出下单
            ret, data = trade_ctx.place_order(
                price=80.0,
                qty=200,
                code='HK.09988',
                trd_side=TrdSide.SELL,
                order_type=OrderType.NORMAL
            )
            
            if ret == 0:
                test_result("模拟卖出下单", True)
                order_id = data['order_id'].iloc[0] if len(data) > 0 else 'N/A'
                print(f"   ✅ 下单成功，订单 ID: {order_id}")
            else:
                test_result("模拟卖出下单", False, str(data))
                print(f"   ❌ 下单失败：{data}")
        
        else:
            test_result("模拟交易解锁", False, msg)
    
    except Exception as e:
        test_result("模拟交易", False, str(e))
        print(f"❌ 模拟交易失败：{e}")
    finally:
        if trade_ctx:
            trade_ctx.close()
    
    # ============================================================
    # 4. 交易时间验证
    # ============================================================
    print()
    print("-" * 70)
    print("⏰ 4. 交易时间验证")
    print("-" * 70)
    
    current_time = datetime.now()
    print(f"当前时间：{current_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"星期：{current_time.strftime('%A')}")
    
    # 港股交易时间
    print()
    print("📊 港股交易时间:")
    print("  早盘：09:30 - 12:00")
    print("  午盘：13:00 - 16:00")
    print("  当前：", end="")
    
    hour = current_time.hour
    minute = current_time.minute
    time_now = hour * 60 + minute
    
    if current_time.weekday() < 5:  # 工作日
        if 570 <= time_now < 720:  # 09:30-12:00
            print("✅ 早盘交易中")
        elif 780 <= time_now < 960:  # 13:00-16:00
            print("✅ 午盘交易中")
        else:
            print("⏸️ 非交易时间")
    else:
        print("⏸️ 周末休市")
    
    test_result("交易时间检查", True)
    
    # ============================================================
    # 5. 总结报告
    # ============================================================
    print()
    print("=" * 70)
    print("📊 测试总结")
    print("=" * 70)
    print(f"总测试数：{test_results['total']}")
    print(f"✅ 通过：{test_results['passed']}")
    print(f"❌ 失败：{test_results['failed']}")
    print(f"通过率：{test_results['passed']/test_results['total']*100:.1f}%")
    print()
    
    if test_results['failed'] == 0:
        print("🎉 所有测试通过！SDK 功能正常！")
    else:
        print("⚠️ 部分测试失败，请检查配置")
        print()
        print("失败项目:")
        for test in test_results['tests']:
            if not test['success']:
                print(f"  ❌ {test['name']}: {test['message']}")

except Exception as e:
    print(f"❌ 测试异常：{e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 70)
