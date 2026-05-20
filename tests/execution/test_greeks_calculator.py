#!/usr/bin/env python3
"""
Greeks 计算器测试脚本

验证内容：
1. Black-Scholes 理论计算准确性
2. Delta 目标范围 (0.4-0.6)
3. Gamma/Theta/Vega 计算
4. 实时监控告警
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from datetime import date, timedelta
from app.execution.greeks_calculator import (
    GreeksCalculator,
    GreeksData,
    GreeksMonitor,
    GreeksMonitorConfig,
    calculate_position_greeks,
)


def test_bs_greeks_calculation():
    """测试 1: Black-Scholes Greeks 计算"""
    print("\n" + "=" * 70)
    print("测试 1: Black-Scholes Greeks 计算")
    print("=" * 70)
    
    calculator = GreeksCalculator(quote_ctx=None)
    
    # 测试用例：ATM Put 期权
    expiry = (date.today() + timedelta(days=16)).strftime("%Y-%m-%d")
    
    greeks = calculator.get_greeks(
        option_code="US.TSLA260417P380000",
        underlying_price=380.0,
        strike=380.0,
        expiry_date=expiry,
        option_type="P",
        option_price=14.32,
    )
    
    print(f"\nATM Put 期权 (S=380, K=380, DTE=16):")
    print(f"  Delta:   {greeks.delta:.4f} (目标：-0.4 ~ -0.6)")
    print(f"  Gamma:   {greeks.gamma:.6f}")
    print(f"  Theta:   {greeks.theta:.4f} (每日衰减)")
    print(f"  Vega:    {greeks.vega:.4f} (1% IV 变动)")
    print(f"  IV:      {greeks.implied_volatility:.2%}")
    
    # 验证 Delta 在目标范围
    delta_valid = -0.6 <= greeks.delta <= -0.4
    print(f"\n  Delta 验证：{'✅ PASS' if delta_valid else '❌ FAIL'}")
    
    # 验证 Gamma 为正
    gamma_valid = greeks.gamma > 0
    print(f"  Gamma 验证：{'✅ PASS' if gamma_valid else '❌ FAIL'} (Gamma 应为正)")
    
    # 验证 Theta 为负 (时间衰减)
    theta_valid = greeks.theta < 0
    print(f"  Theta 验证：{'✅ PASS' if theta_valid else '❌ FAIL'} (Theta 应为负)")
    
    # 验证 Vega 为正
    vega_valid = greeks.vega > 0
    print(f"  Vega 验证：{'✅ PASS' if vega_valid else '❌ FAIL'} (Vega 应为正)")
    
    all_passed = delta_valid and gamma_valid and theta_valid and vega_valid
    return all_passed


def test_delta_target_range():
    """测试 2: Delta 目标范围监控 (0.4-0.6)"""
    print("\n" + "=" * 70)
    print("测试 2: Delta 目标范围监控")
    print("=" * 70)
    
    calculator = GreeksCalculator(quote_ctx=None)
    config = GreeksMonitorConfig(
        delta_target_min=0.4,
        delta_target_max=0.6,
    )
    monitor = GreeksMonitor(calculator, config)
    
    expiry = (date.today() + timedelta(days=16)).strftime("%Y-%m-%d")
    
    # 测试 ATM Put (Delta 应≈-0.5)
    greeks = monitor.update(
        option_code="US.TSLA260417P380000",
        underlying_price=380.0,
        strike=380.0,
        expiry_date=expiry,
        option_type="P",
        option_price=14.32,
    )
    
    delta_abs = abs(greeks.delta)
    in_range = config.delta_target_min <= delta_abs <= config.delta_target_max
    
    print(f"\nATM Put Delta: {greeks.delta:.4f} (绝对值：{delta_abs:.4f})")
    print(f"目标范围：[{config.delta_target_min}, {config.delta_target_max}]")
    print(f"状态：{'✅ 在范围内' if in_range else '❌ 超出范围'}")
    
    # 测试 ITM Put (Delta 应<-0.6)
    greeks_itm = monitor.update(
        option_code="US.TSLA260417P400000",
        underlying_price=380.0,
        strike=400.0,
        expiry_date=expiry,
        option_type="P",
        option_price=25.0,
    )
    
    delta_abs_itm = abs(greeks_itm.delta)
    it_out_of_range = delta_abs_itm > config.delta_target_max
    
    print(f"\nITM Put Delta: {greeks_itm.delta:.4f} (绝对值：{delta_abs_itm:.4f})")
    print(f"状态：{'✅ 正确识别为超出范围' if it_out_of_range else '❌ 未识别'}")
    
    # 测试 OTM Put (Delta 应>-0.4)
    greeks_otm = monitor.update(
        option_code="US.TSLA260417P360000",
        underlying_price=380.0,
        strike=360.0,
        expiry_date=expiry,
        option_type="P",
        option_price=6.0,
    )
    
    delta_abs_otm = abs(greeks_otm.delta)
    otm_out_of_range = delta_abs_otm < config.delta_target_min
    
    print(f"\nOTM Put Delta: {greeks_otm.delta:.4f} (绝对值：{delta_abs_otm:.4f})")
    print(f"状态：{'✅ 正确识别为超出范围' if otm_out_of_range else '❌ 未识别'}")
    
    # 检查告警
    alerts = monitor.get_alerts()
    print(f"\n生成告警数：{len(alerts)}")
    for alert in alerts:
        print(f"  - {alert['type']}: {alert['option_code']}")
    
    return in_range and it_out_of_range and otm_out_of_range


def test_gamma_theta_vega():
    """测试 3: Gamma/Theta/Vega 计算验证"""
    print("\n" + "=" * 70)
    print("测试 3: Gamma/Theta/Vega 计算验证")
    print("=" * 70)
    
    calculator = GreeksCalculator(quote_ctx=None)
    expiry = (date.today() + timedelta(days=16)).strftime("%Y-%m-%d")
    
    greeks = calculator.get_greeks(
        option_code="US.TSLA260417P380000",
        underlying_price=380.0,
        strike=380.0,
        expiry_date=expiry,
        option_type="P",
    )
    
    print(f"\nATM Put Greeks:")
    print(f"  Gamma: {greeks.gamma:.6f}")
    print(f"    说明：标的价格变动$1, Delta 变化{greeks.gamma:.4f}")
    
    print(f"  Theta: {greeks.theta:.4f}")
    print(f"    说明：每日时间衰减${abs(greeks.theta):.4f}")
    
    print(f"  Vega: {greeks.vega:.4f}")
    print(f"    说明：IV 变动 1%, 期权价格变动${greeks.vega:.4f}")
    
    # 验证合理性
    gamma_valid = 0 < greeks.gamma < 0.1  # ATM 期权 Gamma 通常较小
    theta_valid = greeks.theta < 0  # Theta 应为负
    vega_valid = 0 < greeks.vega < 1.0  # Vega 通常为正且小于 1
    
    print(f"\n  Gamma 合理性：{'✅ PASS' if gamma_valid else '❌ FAIL'}")
    print(f"  Theta 合理性：{'✅ PASS' if theta_valid else '❌ FAIL'}")
    print(f"  Vega 合理性：{'✅ PASS' if vega_valid else '❌ FAIL'}")
    
    return gamma_valid and theta_valid and vega_valid


def test_position_greeks():
    """测试 4: 持仓组合 Greeks 计算"""
    print("\n" + "=" * 70)
    print("测试 4: 持仓组合 Greeks 计算")
    print("=" * 70)
    
    calculator = GreeksCalculator(quote_ctx=None)
    expiry = (date.today() + timedelta(days=16)).strftime("%Y-%m-%d")
    
    # 模拟持仓：2 手 ATM Put
    positions = [
        {
            "option_code": "US.TSLA260417P380000",
            "quantity": 2,  # 2 手
            "underlying_price": 380.0,
            "strike": 380.0,
            "expiry_date": expiry,
            "option_type": "P",
        }
    ]
    
    total_greeks = calculate_position_greeks(positions, calculator)
    
    print(f"\n持仓组合 Greeks (2 手 ATM Put):")
    print(f"  Total Delta: {total_greeks['delta']:.2f}")
    print(f"    说明：标的价格变动$1, 持仓价值变动${total_greeks['delta']:.2f}")
    
    print(f"  Total Gamma: {total_greeks['gamma']:.4f}")
    print(f"  Total Theta: {total_greeks['theta']:.2f} (每日衰减)")
    print(f"  Total Vega: {total_greeks['vega']:.2f} (1% IV 变动)")
    
    # 验证 Delta (2 手×100 股×-0.5 ≈ -100)
    delta_expected = 2 * 100 * -0.5
    delta_valid = abs(total_greeks['delta'] - delta_expected) < 20  # 允许误差
    
    print(f"\n  Delta 验证：{'✅ PASS' if delta_valid else '❌ FAIL'} (期望≈{delta_expected:.0f})")
    
    return delta_valid


def test_real_time_update():
    """测试 5: 实时更新 Greeks"""
    print("\n" + "=" * 70)
    print("测试 5: 实时更新 Greeks")
    print("=" * 70)
    
    calculator = GreeksCalculator(quote_ctx=None)
    expiry = (date.today() + timedelta(days=16)).strftime("%Y-%m-%d")
    
    # 初始 Greeks
    greeks1 = calculator.get_greeks(
        option_code="US.TSLA260417P380000",
        underlying_price=380.0,
        strike=380.0,
        expiry_date=expiry,
        option_type="P",
    )
    
    # 标的价格上涨后的 Greeks
    greeks2 = calculator.get_greeks(
        option_code="US.TSLA260417P380000",
        underlying_price=390.0,  # 上涨$10
        strike=380.0,
        expiry_date=expiry,
        option_type="P",
    )
    
    print(f"\n标的价格变动：$380 → $390 (+$10)")
    print(f"\nDelta 变化：{greeks1.delta:.4f} → {greeks2.delta:.4f} ({greeks2.delta - greeks1.delta:+.4f})")
    print(f"  说明：Put 期权 Delta 应增大 (更接近 0)")
    
    print(f"\n期权价格变化估算:")
    print(f"  ΔPrice ≈ Delta × ΔS = {greeks1.delta:.4f} × 10 = ${greeks1.delta * 10:.2f}")
    
    # 验证 Delta 变化方向 (Put 期权，标的上涨，Delta 应增大)
    delta_increased = greeks2.delta > greeks1.delta
    
    print(f"\n  Delta 变化方向：{'✅ PASS' if delta_increased else '❌ FAIL'}")
    
    return delta_increased


def main():
    """运行所有测试"""
    print("=" * 70)
    print("Greeks 计算器验证测试")
    print("=" * 70)
    
    results = []
    
    print("\n[1/5] Black-Scholes 计算...")
    results.append(("BS 计算", test_bs_greeks_calculation()))
    
    print("\n[2/5] Delta 目标范围监控...")
    results.append(("Delta 监控", test_delta_target_range()))
    
    print("\n[3/5] Gamma/Theta/Vega 验证...")
    results.append(("Gamma/Theta/Vega", test_gamma_theta_vega()))
    
    print("\n[4/5] 持仓组合 Greeks...")
    results.append(("组合 Greeks", test_position_greeks()))
    
    print("\n[5/5] 实时更新 Greeks...")
    results.append(("实时更新", test_real_time_update()))
    
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
        print("\n✅ Greeks 计算器验证通过！")
        return 0
    else:
        print(f"\n⚠️ {total - passed} 项未通过")
        return 1


if __name__ == "__main__":
    sys.exit(main())
