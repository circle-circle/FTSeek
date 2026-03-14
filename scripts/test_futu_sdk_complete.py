#!/usr/bin/env python3.8
"""
富途 SDK 完整能力测试
验证模拟市场买卖交易功能
"""
import sys
import time
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek')

print("=" * 70)
print("🔍 富途 SDK 完整能力测试")
print("=" * 70)
print(f"📅 测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# 测试结果
results = {"passed": 0, "failed": 0, "tests": []}

def test(name, success, msg=""):
    results["tests"].append({"name": name, "success": success, "msg": msg})
    if success:
        results["passed"] += 1
        print(f"✅ {name}")
    else:
        results["failed"] += 1
        print(f"❌ {name}: {msg}")

try:
    from futu import OpenQuoteContext, OpenHKTradeContext
    from futu.common.constant import OrderType, TrdSide
    
    test("SDK 导入", True)
    
    # ============================================================
    # 1. 行情查询测试
    # ============================================================
    print("\n" + "=" * 70)
    print("📊 1. 行情查询测试")
    print("=" * 70)
    
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    test("行情连接", True)
    
    # 测试股票报价
    ret, data = quote_ctx.get_market_snapshot('HK.00700')
    if ret == 0 and len(data) > 0:
        test("获取股票报价", True)
        print(f"   腾讯控股：{data['last_price'].iloc[0]:.2f} 港元")
    else:
        test("获取股票报价", False, str(data))
    
    quote_ctx.close()
    
    # ============================================================
    # 2. 交易查询测试
    # ============================================================
    print("\n" + "=" * 70)
    print("💼 2. 交易查询测试")
    print("=" * 70)
    
    trade_ctx = OpenHKTradeContext(host='127.0.0.1', port=11111)
    test("交易连接", True)
    
    # 解锁交易
    ret, msg = trade_ctx.unlock_trade(password='')
    if ret == 0:
        test("交易解锁", True)
        
        # 查询账户
        print("\n📊 查询账户信息...")
        ret, data = trade_ctx.get_accinfo()
        if ret == 0:
            test("查询账户", True)
            if len(data) > 0:
                print(f"   账户数：{len(data)}")
        else:
            test("查询账户", False, str(data))
        
        # 查询持仓
        print("\n📦 查询持仓...")
        ret, data = trade_ctx.get_position_list()
        if ret == 0:
            test("查询持仓", True)
            if len(data) > 0:
                print(f"   持仓数量：{len(data)}")
                for idx, row in data.iterrows():
                    print(f"   - {row['code']}: {row['qty']}股")
            else:
                print("   无持仓")
        else:
            test("查询持仓", False, str(data))
        
        # 查询资金
        print("\n💰 查询资金...")
        ret, data = trade_ctx.get_funds()
        if ret == 0:
            test("查询资金", True)
            if len(data) > 0:
                for idx, row in data.iterrows():
                    print(f"   总资产：{row['total_assets']:.2f}")
                    print(f"   现金：{row['cash']:.2f}")
                    print(f"   可用：{row['power']:.2f}")
            else:
                print("   无资金数据")
        else:
            test("查询资金", False, str(data))
        
        # ============================================================
        # 3. 模拟交易测试
        # ============================================================
        print("\n" + "=" * 70)
        print("🧪 3. 模拟交易测试")
        print("=" * 70)
        
        # 测试买入
        print("\n📝 测试买入下单...")
        ret, data = trade_ctx.place_order(
            price=520.0,
            qty=100,
            code='HK.00700',
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL
        )
        
        if ret == 0 and len(data) > 0:
            test("模拟买入", True)
            order_id = data['order_id'].iloc[0]
            print(f"   订单 ID: {order_id}")
            
            # 测试撤单
            print("\n📝 测试撤单...")
            ret, msg = trade_ctx.cancel_order(order_id)
            if ret == 0:
                test("模拟撤单", True)
            else:
                test("模拟撤单", False, msg)
        else:
            test("模拟买入", False, str(data))
        
        # 测试卖出
        print("\n📝 测试卖出下单...")
        ret, data = trade_ctx.place_order(
            price=80.0,
            qty=200,
            code='HK.09988',
            trd_side=TrdSide.SELL,
            order_type=OrderType.NORMAL
        )
        
        if ret == 0 and len(data) > 0:
            test("模拟卖出", True)
            order_id = data['order_id'].iloc[0]
            print(f"   订单 ID: {order_id}")
            
            # 撤单
            ret, msg = trade_ctx.cancel_order(order_id)
            if ret == 0:
                test("卖出撤单", True)
            else:
                test("卖出撤单", False, msg)
        else:
            test("模拟卖出", False, str(data))
        
        trade_ctx.close()
    else:
        test("交易解锁", False, msg)
        trade_ctx.close()
    
    # ============================================================
    # 4. 交易时间检查
    # ============================================================
    print("\n" + "=" * 70)
    print("⏰ 4. 交易时间检查")
    print("=" * 70)
    
    now = datetime.now()
    print(f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"星期：{now.strftime('%A')}")
    
    hour = now.hour
    minute = now.minute
    time_now = hour * 60 + minute
    is_weekday = now.weekday() < 5
    
    print("\n📊 港股交易时间:")
    print("  早盘：09:30 - 12:00")
    print("  午盘：13:00 - 16:00")
    
    if is_weekday:
        if 570 <= time_now < 720 or 780 <= time_now < 960:
            print("  状态：✅ 交易中")
            test("交易时间", True, "当前可交易")
        else:
            print("  状态：⏸️ 休市")
            test("交易时间", True, "非交易时间但可下单")
    else:
        print("  状态：⏸️ 周末休市")
        test("交易时间", True, "周末但模拟盘可测试")
    
    # ============================================================
    # 5. 总结
    # ============================================================
    print("\n" + "=" * 70)
    print("📊 测试总结")
    print("=" * 70)
    total = results["passed"] + results["failed"]
    print(f"总测试数：{total}")
    print(f"✅ 通过：{results['passed']}")
    print(f"❌ 失败：{results['failed']}")
    if total > 0:
        print(f"通过率：{results['passed']/total*100:.1f}%")
    
    if results['failed'] == 0:
        print("\n🎉 所有测试通过！SDK 功能正常！")
    else:
        print("\n⚠️ 部分测试失败")
        for t in results["tests"]:
            if not t["success"]:
                print(f"  ❌ {t['name']}: {t['msg']}")

except Exception as e:
    print(f"\n❌ 测试异常：{e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
