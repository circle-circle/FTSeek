#!/usr/bin/env python3
"""
期权风险监控测试脚本

验证内容：
1. 保证金充足性检查
2. 期权价值止损 (-50% 权利金)
3. 到期日监控 (DTE≥7 天)
4. Greeks 风险监控
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from datetime import date, timedelta
from app.risk.option_risk_monitor import OptionRiskMonitor, OptionRiskConfig


def test_margin_sufficiency():
    """测试 1: 保证金充足性检查"""
    print("\n" + "=" * 70)
    print("测试 1: 保证金充足性检查")
    print("=" * 70)
    
    monitor = OptionRiskMonitor()
    
    # 测试用例 1: 余额充足
    result1 = monitor.check_margin_sufficiency(
        option_cost=1500.0,
        account_balance=50000.0,
    )
    
    print(f"\n用例 1: 余额充足")
    print(f"  期权成本：${result1['option_cost']:.2f}")
    print(f"  所需余额：${result1['required_balance']:.2f} (含{result1['buffer_pct']:.0%}缓冲)")
    print(f"  账户余额：${result1['account_balance']:.2f}")
    print(f"  结果：{'✅ PASS' if result1['passed'] else '❌ FAIL'}")
    
    # 测试用例 2: 余额不足
    monitor.clear_alerts()
    result2 = monitor.check_margin_sufficiency(
        option_cost=15000.0,
        account_balance=10000.0,
    )
    
    print(f"\n用例 2: 余额不足")
    print(f"  期权成本：${result2['option_cost']:.2f}")
    print(f"  所需余额：${result2['required_balance']:.2f}")
    print(f"  账户余额：${result2['account_balance']:.2f}")
    print(f"  结果：{'✅ PASS' if not result2['passed'] else '❌ FAIL'} (应失败)")
    
    assert result1['passed'] and not result2['passed'], "test_margin_sufficiency 验证失败 (B1 测试卫生: return True→assert)"

def test_stop_loss():
    """测试 2: 期权价值止损 (-50% 权利金)"""
    print("\n" + "=" * 70)
    print("测试 2: 期权价值止损 (-50% 权利金)")
    print("=" * 70)
    
    monitor = OptionRiskMonitor()
    
    # 测试用例 1: 未触发止损 (亏损<50%)
    result1 = monitor.check_stop_loss(
        entry_premium=15.0,
        current_premium=10.0,
    )
    
    loss_pct1 = result1['loss_pct']
    print(f"\n用例 1: 亏损{loss_pct1:.1%} (阈值 50%)")
    print(f"  开仓权利金：${result1['entry_premium']:.2f}")
    print(f"  当前权利金：${result1['current_premium']:.2f}")
    print(f"  结果：{'✅ PASS' if not result1['triggered'] else '❌ FAIL'} (不应触发)")
    
    # 测试用例 2: 触发止损 (亏损≥50%)
    monitor.clear_alerts()
    result2 = monitor.check_stop_loss(
        entry_premium=15.0,
        current_premium=7.0,
    )
    
    loss_pct2 = result2['loss_pct']
    print(f"\n用例 2: 亏损{loss_pct2:.1%} (阈值 50%)")
    print(f"  开仓权利金：${result2['entry_premium']:.2f}")
    print(f"  当前权利金：${result2['current_premium']:.2f}")
    print(f"  结果：{'✅ PASS' if result2['triggered'] else '❌ FAIL'} (应触发)")
    
    # 测试用例 3: 刚好触发止损 (亏损=50%)
    monitor.clear_alerts()
    result3 = monitor.check_stop_loss(
        entry_premium=15.0,
        current_premium=7.5,
    )
    
    loss_pct3 = result3['loss_pct']
    print(f"\n用例 3: 亏损{loss_pct3:.1%} (阈值 50%)")
    print(f"  结果：{'✅ PASS' if result3['triggered'] else '❌ FAIL'} (应触发)")
    
    assert not result1['triggered'] and result2['triggered'] and result3['triggered'], "test_stop_loss 验证失败 (B1 测试卫生: return True→assert)"

def test_dte_monitoring():
    """测试 3: 到期日监控 (DTE≥7 天)"""
    print("\n" + "=" * 70)
    print("测试 3: 到期日监控 (DTE≥7 天)")
    print("=" * 70)
    
    monitor = OptionRiskMonitor()

    # B1 测试卫生 (2026-08-24): 用固定日期替代 date.today() — check_dte 内部用
    # now_et() 取当前日期 (DEB-fullchain 低优 8/16), 而原测试用服务器本地日期构造
    # 到期日, 北京上午时段 (ET 还在前一天) 产生 12h 偏移 → dte 多算 1 天,
    # 告警用例时过时不过. 显式传 current_date 使测试完全确定, 不依赖运行时刻.

    # 测试用例 1: DTE 充足 (16 天)
    result1 = monitor.check_dte(expiry_date="2026-09-05", current_date="2026-08-20")
    
    print(f"\n用例 1: DTE={result1['dte']}天 (要求≥{result1['min_required']}天)")
    print(f"  结果：{'✅ PASS' if result1['passed'] else '❌ FAIL'}")
    
    # 测试用例 2: DTE 不足 (5 天)
    monitor.clear_alerts()
    result2 = monitor.check_dte(expiry_date="2026-08-25", current_date="2026-08-20")
    
    print(f"\n用例 2: DTE={result2['dte']}天 (要求≥{result2['min_required']}天)")
    print(f"  结果：{'✅ PASS' if not result2['passed'] else '❌ FAIL'} (应失败)")
    
    # 测试用例 3: DTE 告警 (5 天，在告警阈值内)
    monitor.clear_alerts()
    result3 = monitor.check_dte(expiry_date="2026-08-25", current_date="2026-08-20")
    
    print(f"\n用例 3: DTE 告警测试")
    print(f"  告警阈值：≤{result3['alert_threshold']}天")
    print(f"  是否告警：{'✅ PASS' if result3['is_alert'] else '❌ FAIL'}")
    
    assert result1['passed'] and not result2['passed'] and result3['is_alert'], "test_dte_monitoring 验证失败 (B1 测试卫生: return True→assert)"

def test_greeks_risk():
    """测试 4: Greeks 风险监控"""
    print("\n" + "=" * 70)
    print("测试 4: Greeks 风险监控")
    print("=" * 70)
    
    monitor = OptionRiskMonitor()
    
    # 测试用例 1: Greeks 正常 (Delta 在 0.4-0.6 范围)
    result1 = monitor.check_greeks_risk(
        delta=-0.5,
        gamma=0.01,
        theta=-0.3,
        vega=0.25,
    )
    
    print(f"\n用例 1: Greeks 正常")
    print(f"  Delta: {result1['delta']['value']:.4f} (范围：{result1['delta']['target_range']})")
    print(f"  Gamma: {result1['gamma']['value']:.6f} (阈值：{result1['gamma']['threshold']})")
    print(f"  Theta: {result1['theta']['value']:.4f} (限制：{result1['theta']['limit']})")
    print(f"  Vega: {result1['vega']['value']:.4f} (限制：{result1['vega']['limit']})")
    print(f"  结果：{'✅ PASS' if result1['passed'] else '❌ FAIL'}")
    
    # 测试用例 2: Delta 超出范围 (ITM, Delta=-0.7)
    monitor.clear_alerts()
    result2 = monitor.check_greeks_risk(
        delta=-0.7,
        gamma=0.01,
        theta=-0.3,
        vega=0.25,
    )
    
    print(f"\n用例 2: Delta 超出范围 (-0.7)")
    print(f"  Delta 在范围内：{result2['delta']['in_range']}")
    print(f"  结果：{'✅ PASS' if not result2['passed'] else '❌ FAIL'} (应失败)")
    
    # 测试用例 3: Delta 超出范围 (OTM, Delta=-0.3)
    monitor.clear_alerts()
    result3 = monitor.check_greeks_risk(
        delta=-0.3,
        gamma=0.01,
        theta=-0.3,
        vega=0.25,
    )
    
    print(f"\n用例 3: Delta 超出范围 (-0.3)")
    print(f"  Delta 在范围内：{result3['delta']['in_range']}")
    print(f"  结果：{'✅ PASS' if not result3['passed'] else '❌ FAIL'} (应失败)")
    
    # 测试用例 4: Gamma 过高
    monitor.clear_alerts()
    result4 = monitor.check_greeks_risk(
        delta=-0.5,
        gamma=0.10,  # 超过阈值 0.05
        theta=-0.3,
        vega=0.25,
    )
    
    print(f"\n用例 4: Gamma 过高 (0.10 > 0.05)")
    print(f"  Gamma 正常：{result4['gamma']['ok']}")
    print(f"  结果：{'✅ PASS' if not result4['passed'] else '❌ FAIL'} (应失败)")
    
    assert result1['passed'] and not result2['passed'] and not result3['passed'] and not result4['passed'], "test_greeks_risk 验证失败 (B1 测试卫生: return True→assert)"

def test_liquidity_check():
    """测试 5: 流动性检查"""
    print("\n" + "=" * 70)
    print("测试 5: 流动性检查")
    print("=" * 70)
    
    monitor = OptionRiskMonitor()
    
    # 测试用例 1: 流动性充足
    result1 = monitor.check_liquidity(
        volume=500,
        open_interest=1000,
        bid=14.5,
        ask=14.7,
    )
    
    spread1 = result1['bid_ask_spread']['value']
    print(f"\n用例 1: 流动性充足")
    print(f"  成交量：{result1['volume']['value']} (要求≥{result1['volume']['min_required']})")
    print(f"  未平仓：{result1['open_interest']['value']} (要求≥{result1['open_interest']['min_required']})")
    print(f"  买卖价差：{spread1:.1%} (允许≤{result1['bid_ask_spread']['max_allowed']:.0%})")
    print(f"  结果：{'✅ PASS' if result1['passed'] else '❌ FAIL'}")
    
    # 测试用例 2: 流动性不足 (成交量低)
    monitor.clear_alerts()
    result2 = monitor.check_liquidity(
        volume=50,  # 低于要求 100
        open_interest=1000,
        bid=14.5,
        ask=14.7,
    )
    
    print(f"\n用例 2: 流动性不足 (成交量=50)")
    print(f"  成交量正常：{result2['volume']['ok']}")
    print(f"  结果：{'✅ PASS' if not result2['passed'] else '❌ FAIL'} (应失败)")
    
    # 测试用例 3: 买卖价差过大
    monitor.clear_alerts()
    result3 = monitor.check_liquidity(
        volume=500,
        open_interest=1000,
        bid=14.0,
        ask=15.5,  # 价差过大
    )
    
    spread3 = result3['bid_ask_spread']['value']
    print(f"\n用例 3: 买卖价差过大 ({spread3:.1%})")
    print(f"  价差正常：{result3['bid_ask_spread']['ok']}")
    print(f"  结果：{'✅ PASS' if not result3['passed'] else '❌ FAIL'} (应失败)")
    
    assert result1['passed'] and not result2['passed'] and not result3['passed'], "test_liquidity_check 验证失败 (B1 测试卫生: return True→assert)"

def test_custom_config():
    """测试 6: 自定义配置"""
    print("\n" + "=" * 70)
    print("测试 6: 自定义配置")
    print("=" * 70)
    
    # 自定义配置：更严格的止损 (30%)
    custom_config = OptionRiskConfig(
        stop_loss_premium_pct=0.30,
        min_dte=10,
        delta_target_min=0.45,
        delta_target_max=0.55,
    )
    
    monitor = OptionRiskMonitor(config=custom_config)
    
    # 测试止损阈值 (亏损 35% > 30% 阈值)
    result1 = monitor.check_stop_loss(
        entry_premium=15.0,
        current_premium=9.5,  # 亏损 36.7%
    )
    
    loss_pct = result1['loss_pct']
    print(f"\n自定义止损阈值 30%:")
    print(f"  亏损：{loss_pct:.1%}")
    print(f"  结果：{'✅ PASS' if result1['triggered'] else '❌ FAIL'} (应触发)")
    
    # 测试 DTE
    from datetime import timedelta
    expiry = (date.today() + timedelta(days=8)).strftime("%Y-%m-%d")
    result2 = monitor.check_dte(expiry_date=expiry)
    
    print(f"\n自定义 DTE 要求≥10 天:")
    print(f"  实际 DTE: {result2['dte']}天")
    print(f"  结果：{'✅ PASS' if not result2['passed'] else '❌ FAIL'} (应失败)")
    
    assert result1['triggered'] and not result2['passed'], "test_custom_config 验证失败 (B1 测试卫生: return True→assert)"

def main():
    """运行所有测试"""
    print("=" * 70)
    print("期权风险监控验证测试")
    print("=" * 70)
    
    results = []
    
    print("\n[1/6] 保证金充足性检查...")
    try:
        test_margin_sufficiency()
        results.append(("保证金检查", True))
    except AssertionError:
        results.append(("保证金检查", False))
    
    print("\n[2/6] 期权价值止损...")
    try:
        test_stop_loss()
        results.append(("止损检查", True))
    except AssertionError:
        results.append(("止损检查", False))
    
    print("\n[3/6] 到期日监控...")
    try:
        test_dte_monitoring()
        results.append(("DTE 监控", True))
    except AssertionError:
        results.append(("DTE 监控", False))
    
    print("\n[4/6] Greeks 风险监控...")
    try:
        test_greeks_risk()
        results.append(("Greeks 监控", True))
    except AssertionError:
        results.append(("Greeks 监控", False))
    
    print("\n[5/6] 流动性检查...")
    try:
        test_liquidity_check()
        results.append(("流动性检查", True))
    except AssertionError:
        results.append(("流动性检查", False))
    
    print("\n[6/6] 自定义配置...")
    try:
        test_custom_config()
        results.append(("自定义配置", True))
    except AssertionError:
        results.append(("自定义配置", False))
    
    # 汇总结果
    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} {name}")
    
    print("\n" + "=" * 70)
    print(f"总计：{passed}/{total} 通过")
    
    if passed == total:
        print("\n✅ 期权风险监控验证通过！")
        return 0
    else:
        print(f"\n⚠️ {total - passed} 项未通过")
        return 1


if __name__ == "__main__":
    sys.exit(main())
