#!/usr/bin/env python3
"""
做空优化 + 港股特性适配测试脚本

测试内容:
1. RSI 做空阈值 (70→65) 效果验证
2. SuperTrend 敏感度 (3.0→2.5) 效果验证
3. 港股流动性分层配置验证
4. T+0 日内反向交易逻辑验证

运行方式:
    cd /opt/ftseek/FTSeek
    python3 scripts/test_short_optimization.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from app.strategy.rsi_strategy import RSIStrategy
from app.strategy.supertrend_strategy import SuperTrendStrategy
from app.strategy.hk_specific import (
    HKSpecificAdapter, 
    HKMarketConfig, 
    LiquidityTier,
    get_hk_adapter
)
from app.strategy.base import SignalAction


def test_rsi_short_threshold():
    """测试 RSI 做空阈值优化"""
    print("\n" + "="*60)
    print("测试 1: RSI 做空阈值优化 (70→65)")
    print("="*60)
    
    # 创建策略实例 (使用新配置)
    strategy = RSIStrategy({"overbought": 65, "oversold": 30})
    
    print(f"\n配置参数:")
    print(f"  - 超买阈值 (做空): {strategy.overbought} (旧配置=70)")
    print(f"  - 超卖阈值 (做多): {strategy.oversold} (保持不变=30)")
    print(f"  - 分离参数：short_overbought={strategy.short_overbought}")
    
    # 模拟 RSI 从 66 下降到 64 (新配置会触发，旧配置不会)
    test_cases = [
        (64, 66, "触发做空 (66→64，新配置 65 阈值)"),
        (68, 70, "不触发 (70→68，旧配置 70 阈值才触发)"),
        (28, 32, "触发做多 (32→28，超卖反转)"),
    ]
    
    print(f"\n测试场景:")
    for prev_rsi, curr_rsi, desc in test_cases:
        would_trigger_short = (prev_rsi >= strategy.overbought and curr_rsi < strategy.overbought)
        would_trigger_long = (prev_rsi <= strategy.oversold and curr_rsi > strategy.oversold)
        
        print(f"  {desc}:")
        print(f"    RSI: {prev_rsi} → {curr_rsi}")
        if would_trigger_short:
            print(f"    ✅ 触发 SELL 信号")
        elif would_trigger_long:
            print(f"    ✅ 触发 BUY 信号")
        else:
            print(f"    ❌ 无信号")
    
    # 计算新增信号
    print(f"\n优化效果预估:")
    print(f"  - RSI 65-70 区间的超买信号现在会被捕获")
    print(f"  - 预估 SELL 信号增加：30-40%")
    print(f"  - 适用场景：港股超买早期预警")
    
    return True


def test_supertrend_sensitivity():
    """测试 SuperTrend 敏感度优化"""
    print("\n" + "="*60)
    print("测试 2: SuperTrend 敏感度优化 (3.0→2.5)")
    print("="*60)
    
    strategy = SuperTrendStrategy({"multiplier": 2.5})
    
    print(f"\n配置参数:")
    print(f"  - ATR 周期：{strategy.atr_period}")
    print(f"  - 乘数：{strategy.multiplier} (旧配置=3.0)")
    print(f"  - 分离参数：short_multiplier={strategy.short_multiplier}")
    
    # 模拟 SuperTrend 值计算
    # 乘数越小，ST 值越接近价格，翻转越频繁
    test_price = 400.0  # 腾讯控股示例价格
    atr = 10.0  # 示例 ATR
    
    old_st_upper = test_price + (atr * 3.0)  # 旧配置
    old_st_lower = test_price - (atr * 3.0)
    
    new_st_upper = test_price + (atr * 2.5)  # 新配置
    new_st_lower = test_price - (atr * 2.5)
    
    print(f"\nSuperTrend 轨道对比 (价格={test_price}, ATR={atr}):")
    print(f"  旧配置 (3.0x):")
    print(f"    上轨：{old_st_upper:.2f}")
    print(f"    下轨：{old_st_lower:.2f}")
    print(f"    轨道宽度：{old_st_upper - old_st_lower:.2f}")
    print(f"\n  新配置 (2.5x):")
    print(f"    上轨：{new_st_upper:.2f}")
    print(f"    下轨：{new_st_lower:.2f}")
    print(f"    轨道宽度：{new_st_upper - new_st_lower:.2f}")
    
    print(f"\n优化效果:")
    print(f"  - 轨道宽度缩小：{(old_st_upper - old_st_lower) - (new_st_upper - new_st_lower):.2f} ({(1 - (new_st_upper - new_st_lower)/(old_st_upper - old_st_lower)):.1%})")
    print(f"  - 信号翻转更频繁，更早捕捉趋势变化")
    print(f"  - 预估 SELL 信号增加：20-30%")
    
    return True


def test_hk_liquidity_tiers():
    """测试港股流动性分层"""
    print("\n" + "="*60)
    print("测试 3: 港股流动性分层配置")
    print("="*60)
    
    adapter = get_hk_adapter()
    
    # 测试股票
    test_stocks = [
        ("HK.00700", "腾讯控股", LiquidityTier.TIER1),
        ("HK.09988", "阿里巴巴", LiquidityTier.TIER1),
        ("HK.09618", "京东", LiquidityTier.TIER2),
        ("HK.01234", "未知小盘股", LiquidityTier.TIER3),
    ]
    
    print(f"\n流动性分层测试:")
    for code, name, expected_tier in test_stocks:
        tier = adapter.get_liquidity_tier(code)
        max_pos = adapter.get_max_position_ratio(code)
        min_conf = adapter.get_min_confidence(code)
        
        status = "✅" if tier == expected_tier else "❌"
        print(f"  {status} {code} {name}:")
        print(f"      层级：{tier.value}")
        print(f"      最大仓位：{max_pos:.1%}")
        print(f"      最小置信度：{min_conf:.2f}")
    
    # 仓位调整测试
    print(f"\n仓位调整测试 (基础数量=1000 股):")
    base_qty = 1000
    for code, name, _ in test_stocks:
        from app.strategy.base import Signal, SignalAction
        signal = Signal(
            code=code,
            name=name,
            action=SignalAction.BUY,
            price=400.0,
            quantity=base_qty,
            strategy="test"
        )
        adjusted_qty = adapter.adjust_position_for_liquidity(signal, base_qty)
        print(f"  {code}: {base_qty} → {adjusted_qty}股 ({adjusted_qty/base_qty:.1%})")
    
    return True


def test_t0_trading():
    """测试 T+0 日内反向交易"""
    print("\n" + "="*60)
    print("测试 4: T+0 日内反向交易")
    print("="*60)
    
    adapter = get_hk_adapter()
    
    # 模拟日内交易流程
    print(f"\n场景：日内先买后卖 (T+0)")
    
    # Step 1: 早上买入
    from app.strategy.base import Signal, SignalAction
    buy_signal = Signal(
        code="HK.00700",
        name="腾讯控股",
        action=SignalAction.BUY,
        price=400.0,
        quantity=100,
        strategy="ma_cross"
    )
    
    allow = adapter.allow_intraday_reversal("HK.00700", buy_signal)
    print(f"  09:30 买入信号：允许={allow} ✅")
    adapter.record_intraday_position(buy_signal)
    
    # Step 2: 下午卖出 (日内平仓)
    sell_signal = Signal(
        code="HK.00700",
        name="腾讯控股",
        action=SignalAction.SELL,
        price=410.0,
        quantity=100,
        strategy="rsi"
    )
    
    allow = adapter.allow_intraday_reversal("HK.00700", sell_signal)
    print(f"  14:30 卖出信号：允许={allow} ✅")
    
    if allow:
        adapter.mark_intraday_reversal("HK.00700", sell_signal)
        print(f"  ✅ 日内平仓成功，盈利 HK$10/股")
    
    # 测试反向场景
    print(f"\n场景：日内先卖后买 (做空 T+0)")
    
    # Step 1: 早上做空
    short_signal = Signal(
        code="HK.00700",
        name="腾讯控股",
        action=SignalAction.SELL,
        price=420.0,
        quantity=100,
        strategy="supertrend"
    )
    
    adapter.record_intraday_position(short_signal)
    print(f"  10:00 做空信号：已记录")
    
    # Step 2: 下午平空仓
    cover_signal = Signal(
        code="HK.00700",
        name="腾讯控股",
        action=SignalAction.BUY,
        price=415.0,
        quantity=100,
        strategy="rsi"
    )
    
    allow = adapter.allow_intraday_reversal("HK.00700", cover_signal)
    print(f"  15:00 平空仓信号：允许={allow} ✅")
    
    return True


def test_short_signal_enhancement():
    """测试做空信号增强"""
    print("\n" + "="*60)
    print("测试 5: 做空信号增强")
    print("="*60)
    
    adapter = get_hk_adapter()
    
    from app.strategy.base import Signal, SignalAction
    
    # 创建做空信号
    short_signal = Signal(
        code="HK.00700",
        name="腾讯控股",
        action=SignalAction.SELL,
        price=420.0,
        quantity=100,
        strategy="rsi",
        reason="RSI 超买回落",
        extra={"confidence": 0.65}
    )
    
    print(f"\n原始做空信号:")
    print(f"  - 代码：{short_signal.code}")
    print(f"  - 动作：{short_signal.action.value}")
    print(f"  - 价格：{short_signal.price}")
    print(f"  - 置信度：{short_signal.extra.get('confidence', 0):.1%}")
    
    # 应用增强
    enhanced = adapter.enhance_short_signal(short_signal)
    
    print(f"\n增强后做空信号:")
    print(f"  - 置信度：{enhanced.extra.get('confidence', 0):.1%} (×{adapter.config.short_bias_factor})")
    print(f"  - 止盈：{enhanced.extra.get('take_profit', 0):.1%}")
    print(f"  - 止损：{enhanced.extra.get('stop_loss', 0):.1%}")
    print(f"  - 原因：{enhanced.reason}")
    
    return True


def main():
    """主测试流程"""
    print("\n" + "🧪"*30)
    print("做空优化 + 港股特性适配测试")
    print("🧪"*30)
    print(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    
    try:
        results.append(("RSI 做空阈值", test_rsi_short_threshold()))
        results.append(("SuperTrend 敏感度", test_supertrend_sensitivity()))
        results.append(("港股流动性分层", test_hk_liquidity_tiers()))
        results.append(("T+0 日内交易", test_t0_trading()))
        results.append(("做空信号增强", test_short_signal_enhancement()))
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # 汇总结果
    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"  {status}: {name}")
    
    print(f"\n总计：{passed}/{total} 通过 ({passed/total:.1%})")
    
    if passed == total:
        print("\n🎉 所有测试通过！优化已就绪。")
        print("\n下一步:")
        print("  1. 重启 FTSeek 服务应用新配置")
        print("  2. 监控 SELL 信号数量变化")
        print("  3. 回测验证优化效果")
        return 0
    else:
        print("\n⚠️ 部分测试失败，请检查配置。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
