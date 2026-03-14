#!/usr/bin/env python3
"""
策略信号生成测试脚本

测试三个策略的信号生成能力:
1. 均线交叉策略
2. RSI 反弹策略
3. 融合策略 v2

使用方法:
    cd /opt/ftseek/FTSeek
    export API_TOKEN=test123
    export FTSEEK_DEV_MODE=1
    python3 scripts/test_strategy_signals.py
"""
import sys
import time
import random
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.strategies import create_strategy, load_config
from app.strategies.ma_cross import MarketData as MACrossMarketData
from app.strategies.rsi_rebound import MarketData as RSIMarketData
from app.strategies.fusion_v2 import MarketData as FusionMarketData


def generate_test_klines(count: int, base_price: float = 100.0, pattern: str = 'gold_cross') -> list:
    """
    生成测试 K 线数据
    
    pattern 选项:
    - gold_cross: 金叉形态 (先跌后涨)
    - death_cross: 死叉形态 (先涨后跌)
    - oversold: 超卖形态 (持续下跌)
    - overbought: 超买形态 (持续上涨)
    - sideways: 横盘震荡
    """
    klines = []
    price = base_price
    
    for i in range(count):
        if pattern == 'gold_cross':
            # 前 2/3 下跌，后 1/3 上涨
            if i < count * 2 // 3:
                change = -0.01 - random.random() * 0.01
            else:
                change = 0.015 + random.random() * 0.01
        elif pattern == 'death_cross':
            # 前 2/3 上涨，后 1/3 下跌
            if i < count * 2 // 3:
                change = 0.01 + random.random() * 0.01
            else:
                change = -0.015 - random.random() * 0.01
        elif pattern == 'oversold':
            # 持续下跌
            change = -0.015 - random.random() * 0.01
        elif pattern == 'overbought':
            # 持续上涨
            change = 0.015 + random.random() * 0.01
        else:  # sideways
            # 横盘震荡
            change = 0.002 * (1 if i % 2 == 0 else -1)
        
        open_price = price
        close_price = price * (1 + change)
        high_price = max(open_price, close_price) * (1 + random.random() * 0.02)
        low_price = min(open_price, close_price) * (1 - random.random() * 0.02)
        volume = 1000000 * (1 + random.random() * 0.5)
        
        klines.append({
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
            'timestamp': time.time() - (count - i) * 86400
        })
        
        price = close_price
    
    return klines


def test_ma_cross_strategy():
    """测试均线交叉策略"""
    print("\n" + "="*60)
    print("测试 1: 均线交叉策略 (MA Cross)")
    print("="*60)
    
    config = load_config()
    strategy_config = config['strategies'].get('ma_cross', {})
    strategy = create_strategy('ma_cross', strategy_config.get('parameters', {}))
    
    # 测试金叉形态
    print("\n[场景 1] 金叉买入信号")
    klines = generate_test_klines(40, base_price=100, pattern='gold_cross')
    market_data = MACrossMarketData(
        code='TEST.GOLDEN',
        name='Test Golden Cross',
        price=klines[-1]['close'],
        volume=klines[-1]['volume'] * 2,
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    if signal:
        print(f"✓ 生成信号：{signal.action.value} {signal.code}")
        print(f"  价格：{signal.price:.2f}")
        print(f"  原因：{signal.reason}")
    else:
        print("○ 未生成信号 (可能未触发交叉)")
    
    # 测试死叉形态
    print("\n[场景 2] 死叉卖出信号")
    strategy.clear_cache()  # 清除缓存
    klines = generate_test_klines(40, base_price=100, pattern='death_cross')
    market_data = MACrossMarketData(
        code='TEST.DEATH',
        name='Test Death Cross',
        price=klines[-1]['close'],
        volume=klines[-1]['volume'] * 2,
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    if signal:
        print(f"✓ 生成信号：{signal.action.value} {signal.code}")
        print(f"  价格：{signal.price:.2f}")
        print(f"  原因：{signal.reason}")
    else:
        print("○ 未生成信号 (可能未触发交叉)")
    
    # 显示配置
    print("\n[策略配置]")
    print(f"  {strategy.get_config()}")


def test_rsi_rebound_strategy():
    """测试 RSI 反弹策略"""
    print("\n" + "="*60)
    print("测试 2: RSI 反弹策略 (RSI Rebound)")
    print("="*60)
    
    config = load_config()
    strategy_config = config['strategies'].get('rsi_rebound', {})
    strategy = create_strategy('rsi_rebound', strategy_config.get('parameters', {}))
    
    # 测试超卖反弹
    print("\n[场景 1] 超卖反弹买入信号")
    klines = generate_test_klines(40, base_price=100, pattern='oversold')
    market_data = RSIMarketData(
        code='TEST.OVERSOLD',
        name='Test Oversold',
        price=klines[-1]['close'],
        volume=1000000,
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    if signal:
        print(f"✓ 生成信号：{signal.action.value} {signal.code}")
        print(f"  价格：{signal.price:.2f}")
        print(f"  原因：{signal.reason}")
        if 'rsi' in signal.extra:
            print(f"  RSI: {signal.extra['rsi']}")
    else:
        print("○ 未生成信号 (RSI 可能未进入超卖区)")
    
    # 测试超买回落
    print("\n[场景 2] 超买回落卖出信号")
    strategy.clear_cache()
    klines = generate_test_klines(40, base_price=100, pattern='overbought')
    market_data = RSIMarketData(
        code='TEST.OVERBOUGHT',
        name='Test Overbought',
        price=klines[-1]['close'],
        volume=1000000,
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    if signal:
        print(f"✓ 生成信号：{signal.action.value} {signal.code}")
        print(f"  价格：{signal.price:.2f}")
        print(f"  原因：{signal.reason}")
        if 'rsi' in signal.extra:
            print(f"  RSI: {signal.extra['rsi']}")
    else:
        print("○ 未生成信号 (RSI 可能未进入超买区)")
    
    # 显示配置
    print("\n[策略配置]")
    print(f"  {strategy.get_config()}")


def test_fusion_v2_strategy():
    """测试融合策略 v2"""
    print("\n" + "="*60)
    print("测试 3: 融合策略 v2 (Fusion)")
    print("="*60)
    
    config = load_config()
    strategy_config = config['strategies'].get('fusion_v2', {})
    strategy = create_strategy('fusion_v2', strategy_config.get('parameters', {}))
    
    # 测试横盘震荡
    print("\n[场景 1] 横盘震荡市场")
    klines = generate_test_klines(60, base_price=100, pattern='sideways')
    market_data = FusionMarketData(
        code='TEST.SIDEWAYS',
        name='Test Sideways',
        price=klines[-1]['close'],
        volume=1000000,
        klines=klines,
        timestamp=time.time()
    )
    
    signal = strategy.generate_signal(market_data)
    if signal:
        print(f"✓ 生成信号：{signal.action.value} {signal.code}")
        print(f"  价格：{signal.price:.2f}")
        print(f"  原因：{signal.reason}")
        if 'final_score' in signal.extra:
            print(f"  融合评分：{signal.extra['final_score']:.2f}")
            print(f"  技术分：{signal.extra.get('technical_score', 0):.2f}")
            print(f"  情绪分：{signal.extra.get('sentiment_score', 0):.2f}")
    else:
        print("○ 未生成信号 (信号太弱)")
    
    # 显示配置
    print("\n[策略配置]")
    print(f"  {strategy.get_config()}")
    print(f"\n  融合公式：score = technical × 0.6 + sentiment × 0.4")


def test_all_strategies_parallel():
    """并行测试所有策略"""
    print("\n" + "="*60)
    print("综合测试：多策略并行")
    print("="*60)
    
    from app.strategies import get_all_strategies
    
    strategies = get_all_strategies()
    print(f"\n启用策略数量：{len(strategies)}")
    
    # 使用同一组数据测试所有策略
    klines = generate_test_klines(60, base_price=100, pattern='gold_cross')
    
    for name, strategy in strategies.items():
        print(f"\n[{name}]")
        
        # 根据策略类型创建对应的 MarketData
        if name == 'ma_cross':
            market_data = MACrossMarketData(
                code='TEST.PARALLEL',
                name='Test Parallel',
                price=klines[-1]['close'],
                volume=klines[-1]['volume'],
                klines=klines,
                timestamp=time.time()
            )
        elif name == 'rsi_rebound':
            market_data = RSIMarketData(
                code='TEST.PARALLEL',
                name='Test Parallel',
                price=klines[-1]['close'],
                volume=klines[-1]['volume'],
                klines=klines,
                timestamp=time.time()
            )
        else:
            market_data = FusionMarketData(
                code='TEST.PARALLEL',
                name='Test Parallel',
                price=klines[-1]['close'],
                volume=klines[-1]['volume'],
                klines=klines,
                timestamp=time.time()
            )
        
        signal = strategy.generate_signal(market_data)
        if signal:
            print(f"  ✓ 信号：{signal.action.value}")
            print(f"  原因：{signal.reason[:80]}...")
        else:
            print(f"  ○ 无信号")


if __name__ == '__main__':
    print("\n" + "="*60)
    print("FTSeek 策略信号生成测试")
    print("="*60)
    
    test_ma_cross_strategy()
    test_rsi_rebound_strategy()
    test_fusion_v2_strategy()
    test_all_strategies_parallel()
    
    print("\n" + "="*60)
    print("测试完成!")
    print("="*60 + "\n")
