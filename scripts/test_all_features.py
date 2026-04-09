#!/usr/bin/env python3
"""
FTSeek 全功能验证测试

测试范围:
1. 运维功能 (已实现)
2. 交易功能 (订单追踪/成本计算)
3. 风控功能 (回撤控制)
4. 策略功能 (回测框架)
5. 数据功能 (质量监控/导出 API)
"""
import os
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 设置环境变量
os.environ['API_TOKEN'] = 'test_token'
os.environ['BAILIAN_API_KEY'] = 'sk-8f8a02822c1b46b4bd43b422b9196ec6'

print("="*70)
print(" " * 20 + "FTSeek 全功能验证测试")
print("="*70)
print()

results = {"pass": 0, "fail": 0, "skip": 0}

def test_result(name: str, passed: bool, message: str = ""):
    status = "✅" if passed else "❌"
    print(f"{status} {name}")
    if message:
        print(f"   {message}")
    if passed:
        results["pass"] += 1
    else:
        results["fail"] += 1
    print()

# ============================================================================
# 模块 1: 运维功能 (已验证)
# ============================================================================
print("="*70)
print("模块 1: 运维功能")
print("="*70)
print()

import requests
try:
    response = requests.get("http://localhost:8888/health", timeout=5)
    test_result("HTTP 健康检查", response.status_code == 200)
except:
    test_result("HTTP 健康检查", False)

# 检查 cron 配置
cron_file = Path("/etc/cron.d/ftseek-tasks")
test_result("定时任务配置", cron_file.exists())

# 检查健康检查脚本
healthcheck_script = Path("/opt/ftseek/FTSeek/scripts/healthcheck.sh")
test_result("健康检查脚本", healthcheck_script.exists() and os.access(healthcheck_script, os.X_OK))

# 检查备份目录
backup_dir = Path("/opt/ftseek/FTSeek/backups")
if backup_dir.exists():
    backups = list(backup_dir.glob("*.db"))
    test_result("数据库备份", len(backups) > 0, f"{len(backups)}个备份文件")
else:
    test_result("数据库备份", False)

# ============================================================================
# 模块 2: 交易功能
# ============================================================================
print("="*70)
print("模块 2: 交易功能")
print("="*70)
print()

# 2.1 订单追踪
try:
    from app.trader.order_tracker import OrderTracker, OrderStatus, get_order_tracker
    
    tracker = get_order_tracker()
    
    # 创建测试订单
    order = tracker.create_order("HK.00700", "BUY", 100, 380.0)
    test_result("订单创建", order is not None, f"订单 ID: {order.order_id}")
    
    # 更新订单状态
    tracker.update_order(order.order_id, status=OrderStatus.PARTIALLY_FILLED)
    test_result("订单状态更新", order.status == OrderStatus.PARTIALLY_FILLED)
    
    # 添加成交明细
    tracker.add_fill(order.order_id, 380.5, 50)
    test_result("成交明细添加", order.filled_quantity == 50)
    
    # 获取统计
    stats = tracker.get_statistics()
    test_result("订单统计", stats['total'] > 0, f"总订单数：{stats['total']}")
    
except Exception as e:
    test_result("订单追踪模块", False, str(e))

# 2.2 成本计算
try:
    from app.trader.cost_calculator import CostCalculator, get_cost_calculator
    
    calc = get_cost_calculator()
    
    # 计算买入成本
    buy_cost = calc.calculate_buy_cost(380.0, 100)
    test_result("买入成本计算", buy_cost.total_cost > 0, f"总成本：{buy_cost.total_cost:.2f}港元")
    
    # 计算卖出成本
    sell_cost = calc.calculate_sell_cost(385.0, 100)
    test_result("卖出成本计算", sell_cost.total_cost > 0, f"总成本：{sell_cost.total_cost:.2f}港元")
    
    # 往返成本
    round_trip = calc.calculate_round_trip_cost(380.0, 385.0, 100)
    test_result("往返成本分析", round_trip['net_profit'] > 0, f"净利润：{round_trip['net_profit']:.2f}港元")
    
    # 滑点估算
    slippage = calc.estimate_slippage(380.0, 100, 1000000)
    test_result("滑点估算", 'slippage_rate' in slippage, f"滑点率：{slippage['slippage_rate']*100:.2f}%")
    
    # 费率摘要
    summary = calc.get_cost_summary()
    test_result("费率摘要", len(summary) > 0, f"{len(summary)}项费率配置")
    
except Exception as e:
    test_result("成本计算模块", False, str(e))

# ============================================================================
# 模块 3: 风控功能
# ============================================================================
print("="*70)
print("模块 3: 风控功能")
print("="*70)
print()

# 3.1 回撤控制
try:
    from app.risk.drawdown_control import DrawdownController, get_drawdown_controller, DrawdownConfig
    
    config = DrawdownConfig(
        max_daily_drawdown=0.03,
        max_total_drawdown=0.15,
    )
    controller = get_drawdown_controller({
        'max_daily_drawdown': 0.03,
        'max_total_drawdown': 0.15,
    })
    
    # 更新资产值
    state = controller.update_value(1000000.0)
    test_result("回撤状态更新", state.current_drawdown == 0.0, f"当前回撤：{state.current_drawdown*100:.2f}%")
    
    # 模拟回撤
    state = controller.update_value(950000.0)  # 回撤 5%
    test_result("回撤检测", state.current_drawdown > 0.04, f"当前回撤：{state.current_drawdown*100:.2f}%")
    
    # 检查限制
    limits = controller.check_limits()
    test_result("回撤限制检查", 'allowed' in limits, f"交易允许：{limits['trading_allowed']}")
    
    # 生成报告
    report = controller.get_report()
    test_result("回撤报告", 'current_value' in report, f"当前资产：{report['current_value']:,.0f}")
    
except Exception as e:
    test_result("回撤控制模块", False, str(e))

# ============================================================================
# 模块 4: 策略功能
# ============================================================================
print("="*70)
print("模块 4: 策略功能")
print("="*70)
print()

# 4.1 回测框架
try:
    from app.backtest.simple_backtester import SimpleBacktester, get_backtester, BacktestConfig
    from datetime import datetime, timedelta
    
    # 生成测试数据
    test_data = []
    base_date = datetime(2026, 1, 1)
    base_price = 100.0
    
    for i in range(60):  # 60 天数据
        date = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        # 模拟价格波动
        import random
        price = base_price * (1 + random.gauss(0, 0.02))
        base_price = price
        
        test_data.append({
            'date': date,
            'stock_code': 'HK.00700',
            'open': price * 0.99,
            'high': price * 1.02,
            'low': price * 0.98,
            'close': price,
            'volume': 1000000,
        })
    
    # 简单策略：金叉买入，死叉卖出
    def simple_strategy(data, positions, cash):
        signals = []
        if len(data) >= 5:
            closes = [d['close'] for d in data[-5:]]
            avg = sum(closes) / len(closes)
            current = closes[-1]
            
            if current > avg * 1.02 and 'HK.00700' not in positions and cash > 10000:
                signals.append({
                    'stock_code': 'HK.00700',
                    'action': 'BUY',
                    'quantity': 100,
                })
            elif current < avg * 0.98 and 'HK.00700' in positions:
                signals.append({
                    'stock_code': 'HK.00700',
                    'action': 'SELL',
                    'quantity': positions['HK.00700'],
                })
        return signals
    
    # 运行回测
    backtester = get_backtester()
    result = backtester.run(test_data, simple_strategy)
    
    test_result("回测执行", result is not None, f"总收益：{result.total_return*100:.2f}%")
    test_result("回测交易数", result.total_trades >= 0, f"{result.total_trades}笔交易")
    
    # 生成报告
    report = backtester.generate_report(result)
    test_result("回测报告生成", len(report) > 100, f"报告长度：{len(report)}字符")
    
except Exception as e:
    test_result("回测框架", False, str(e))

# ============================================================================
# 模块 5: 数据功能
# ============================================================================
print("="*70)
print("模块 5: 数据功能")
print("="*70)
print()

# 5.1 数据质量监控
try:
    from app.data.data_quality import DataQualityChecker, get_quality_checker
    
    checker = get_quality_checker()
    
    # 生成测试 K 线数据
    test_kline = []
    base_time = datetime(2026, 3, 15, 9, 30)
    base_price = 380.0
    
    for i in range(240):  # 一天 240 根 1 分钟 K 线
        from datetime import timedelta
        time = (base_time + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
        import random
        price = base_price * (1 + random.gauss(0, 0.001))
        
        test_kline.append({
            'time': time,
            'open': price,
            'high': price * 1.001,
            'low': price * 0.999,
            'close': price,
            'volume': 10000,
        })
    
    # 检查完整性
    report = checker.check_kline_completeness("HK.00700", test_kline)
    test_result("K 线完整性检查", report.completeness > 90, f"完整率：{report.completeness:.1f}%")
    test_result("数据质量状态", report.status in ['good', 'warning'], f"状态：{report.status}")
    
    # 新鲜度检查
    freshness = checker.check_data_freshness("HK.00700", datetime.now())
    test_result("数据新鲜度", freshness['status'] in ['fresh', 'ok'], f"状态：{freshness['status']}")
    
except Exception as e:
    test_result("数据质量监控", False, str(e))

# 5.2 导出 API
try:
    from app.api.export_api import export_json, export_csv
    
    # 测试数据
    test_data = [
        {'id': 1, 'name': '测试 1', 'value': 100},
        {'id': 2, 'name': '测试 2', 'value': 200},
    ]
    
    # JSON 导出
    json_response = export_json(test_data, "test")
    test_result("JSON 导出", json_response.media_type == "application/json")
    
    # CSV 导出
    csv_response = export_csv(test_data, "test")
    test_result("CSV 导出", csv_response.media_type == "text/csv")
    
except Exception as e:
    test_result("导出 API", False, str(e))

# ============================================================================
# 测试总结
# ============================================================================
print("="*70)
print(" " * 28 + "测试总结")
print("="*70)
print()
print(f"✅ 通过：{results['pass']}")
print(f"❌ 失败：{results['fail']}")
print(f"⏭️  跳过：{results['skip']}")
print()

total = results['pass'] + results['fail'] + results['skip']
pass_rate = (results['pass'] / total * 100) if total > 0 else 0

print(f"通过率：{pass_rate:.1f}%")
print()

if results['fail'] == 0:
    print("🎉 所有测试通过！FTSeek 全功能验证完成！")
else:
    print(f"⚠️  有 {results['fail']} 个测试失败，请检查！")

print()
print("="*70)

sys.exit(0 if results['fail'] == 0 else 1)
