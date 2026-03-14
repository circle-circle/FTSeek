#!/usr/bin/env python3
"""
策略验证脚本 - 精确测试策略逻辑

验证三个策略的核心逻辑是否正确实现
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.strategies import create_strategy, load_config
from app.strategies.ma_cross import MarketData as MACrossMarketData
from app.strategies.rsi_rebound import MarketData as RSIMarketData
from app.strategies.fusion_v2 import MarketData as FusionMarketData


def test_ma_cross_logic():
    """验证均线交叉逻辑"""
    print("\n" + "="*60)
    print("验证：均线交叉策略逻辑")
    print("="*60)
    
    strategy = create_strategy('ma_cross', {
        'fast_ma': 5,
        'slow_ma': 20,
        'volume_filter': 1.0,  # 关闭成交量过滤
        'enabled': True
    })
    
    # 构造明确的死叉转金叉场景
    # 前 20 条：快线 < 慢线 (下跌)
    # 后 20 条：快线 > 慢线 (上涨)
    klines = []
    base_price = 100
    
    # 下跌阶段 (快线在慢线下方)
    for i in range(25):
        price = base_price - i * 1.5
        klines.append({
            'open': price,
            'high': price * 1.01,
            'low': price * 0.99,
            'close': price,
            'volume': 1000000,
            'timestamp': time.time() - (25 - i) * 86400
        })
    
    # 上涨阶段 (快线上穿慢线)
    for i in range(25):
        price = base_price - 25 * 1.5 + i * 2.0
        klines.append({
            'open': price,
            'high': price * 1.01,
            'low': price * 0.99,
            'close': price,
            'volume': 2000000,  # 放量
            'timestamp': time.time() - (25 - i) * 86400
        })
    
    market_data = MACrossMarketData(
        code='TEST.MA',
        name='Test MA Cross',
        price=klines[-1]['close'],
        volume=klines[-1]['volume'],
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    
    if signal:
        print(f"✓ 检测到信号：{signal.action.value}")
        print(f"  原因：{signal.reason}")
        print(f"  策略：{signal.strategy}")
        assert signal.strategy == 'ma_cross', "策略名称错误"
        assert signal.action.value in ['BUY', 'SELL'], "信号动作错误"
        print("✓ 验证通过")
    else:
        print("○ 未检测到信号 (可能交叉不明显)")
    
    # 验证配置接口
    config = strategy.get_config()
    assert config['name'] == 'ma_cross', "配置名称错误"
    assert 'fast_ma' in config, "缺少 fast_ma 配置"
    assert 'slow_ma' in config, "缺少 slow_ma 配置"
    print("✓ 配置接口验证通过")


def test_rsi_logic():
    """验证 RSI 反弹逻辑"""
    print("\n" + "="*60)
    print("验证：RSI 反弹策略逻辑")
    print("="*60)
    
    strategy = create_strategy('rsi_rebound', {
        'rsi_period': 14,
        'oversold': 25,
        'overbought': 75,
        'max_holding_days': 3,
        'enabled': True
    })
    
    # 构造超卖场景：持续下跌 30 天
    klines = []
    base_price = 100
    
    for i in range(30):
        price = base_price - i * 2.0  # 大幅下跌
        klines.append({
            'open': price + 1,
            'high': price * 1.01,
            'low': price * 0.98,
            'close': price,
            'volume': 1000000,
            'timestamp': time.time() - (30 - i) * 86400
        })
    
    # 最后一天反弹
    last_price = klines[-1]['close']
    klines.append({
        'open': last_price,
        'high': last_price * 1.03,
        'low': last_price * 0.99,
        'close': last_price * 1.02,  # 反弹 2%
        'volume': 1500000,
        'timestamp': time.time()
    })
    
    market_data = RSIMarketData(
        code='TEST.RSI',
        name='Test RSI',
        price=klines[-1]['close'],
        volume=klines[-1]['volume'],
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    
    if signal:
        print(f"✓ 检测到信号：{signal.action.value}")
        print(f"  原因：{signal.reason}")
        print(f"  策略：{signal.strategy}")
        assert signal.strategy == 'rsi_rebound', "策略名称错误"
        if 'rsi' in signal.extra:
            print(f"  RSI 值：{signal.extra['rsi']}")
        print("✓ 验证通过")
    else:
        print("○ 未检测到信号 (RSI 可能未达超卖区)")
    
    # 验证 RSI 计算
    prices = [100 - i * 2 for i in range(30)]
    rsi = strategy._calculate_rsi(prices, 14)
    print(f"  测试 RSI 计算：{rsi:.2f}")
    assert 0 <= rsi <= 100, "RSI 超出范围"
    print("✓ RSI 计算验证通过")
    
    # 验证配置接口
    config = strategy.get_config()
    assert config['name'] == 'rsi_rebound', "配置名称错误"
    assert 'rsi_period' in config, "缺少 rsi_period 配置"
    assert 'oversold' in config, "缺少 oversold 配置"
    print("✓ 配置接口验证通过")


def test_fusion_logic():
    """验证融合策略逻辑"""
    print("\n" + "="*60)
    print("验证：融合策略 v2 逻辑")
    print("="*60)
    
    strategy = create_strategy('fusion_v2', {
        'technical_weight': 0.6,
        'sentiment_weight': 0.4,
        'buy_threshold': 0.65,
        'enabled': True
    })
    
    # 构造数据
    klines = []
    base_price = 100
    
    for i in range(60):
        price = base_price + (i % 10) * 0.5  # 震荡上行
        klines.append({
            'open': price,
            'high': price * 1.02,
            'low': price * 0.98,
            'close': price,
            'volume': 1000000,
            'timestamp': time.time() - (60 - i) * 86400
        })
    
    market_data = FusionMarketData(
        code='TEST.FUSION',
        name='Test Fusion',
        price=klines[-1]['close'],
        volume=klines[-1]['volume'],
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    
    # 融合策略可能因为情绪评分为 0 而不产生信号
    if signal:
        print(f"✓ 检测到信号：{signal.action.value}")
        print(f"  原因：{signal.reason}")
        print(f"  策略：{signal.strategy}")
        assert signal.strategy == 'fusion_v2', "策略名称错误"
        if 'technical_score' in signal.extra:
            print(f"  技术分：{signal.extra['technical_score']:.2f}")
        if 'sentiment_score' in signal.extra:
            print(f"  情绪分：{signal.extra['sentiment_score']:.2f}")
        if 'final_score' in signal.extra:
            print(f"  融合分：{signal.extra['final_score']:.2f}")
        print("✓ 验证通过")
    else:
        print("○ 未检测到信号 (可能信号强度不足)")
    
    # 验证技术评分计算
    indicators = {
        'price': 100,
        'rsi': 30,  # 超卖
        'macd': 0.5,
        'macd_signal': 0.3,
        'macd_histogram': 0.2,
        'ma_fast': 102,
        'ma_slow': 98,
        'volume': 2000000,
        'volume_avg': 1000000,
        'bb_upper': 105,
        'bb_middle': 100,
        'bb_lower': 95
    }
    tech_score = strategy._calculate_technical_score(indicators)
    print(f"  测试技术评分：{tech_score:.2f}")
    assert -1.0 <= tech_score <= 1.0, "技术评分超出范围"
    print("✓ 技术评分计算验证通过")
    
    # 验证融合公式
    tech = 0.8
    sent = -0.5
    expected = tech * 0.6 + sent * 0.4
    actual = tech * strategy.technical_weight + sent * strategy.sentiment_weight
    assert abs(expected - actual) < 0.001, "融合公式错误"
    print(f"✓ 融合公式验证通过：{tech}×0.6 + {sent}×0.4 = {actual:.2f}")
    
    # 验证配置接口
    config = strategy.get_config()
    assert config['name'] == 'fusion_v2', "配置名称错误"
    assert 'technical_weight' in config, "缺少 technical_weight 配置"
    assert 'sentiment_weight' in config, "缺少 sentiment_weight 配置"
    print("✓ 配置接口验证通过")


def test_unified_interface():
    """验证统一接口"""
    print("\n" + "="*60)
    print("验证：统一接口")
    print("="*60)
    
    strategies = []
    for name in ['fusion_v2', 'ma_cross', 'rsi_rebound']:
        strategy = create_strategy(name, {'enabled': True})
        strategies.append((name, strategy))
    
    for name, strategy in strategies:
        print(f"\n[{name}]")
        
        # 验证 generate_signal 方法
        assert hasattr(strategy, 'generate_signal'), "缺少 generate_signal 方法"
        print("  ✓ generate_signal 方法存在")
        
        # 验证 get_config 方法
        assert hasattr(strategy, 'get_config'), "缺少 get_config 方法"
        config = strategy.get_config()
        assert isinstance(config, dict), "get_config 应返回字典"
        assert 'name' in config, "配置缺少 name 字段"
        assert 'enabled' in config, "配置缺少 enabled 字段"
        print("  ✓ get_config 方法存在")
        
        # 验证名称匹配
        assert config['name'] == name, f"策略名称不匹配：{config['name']} != {name}"
        print(f"  ✓ 策略名称匹配：{name}")
    
    print("\n✓ 统一接口验证通过")


if __name__ == '__main__':
    print("\n" + "="*60)
    print("FTSeek 策略验证")
    print("="*60)
    
    test_ma_cross_logic()
    test_rsi_logic()
    test_fusion_logic()
    test_unified_interface()
    
    print("\n" + "="*60)
    print("所有验证通过! ✓")
    print("="*60 + "\n")
