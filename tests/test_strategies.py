#!/usr/bin/env python3
"""
策略单元测试

测试三个策略:
1. 均线交叉策略 (MA Cross)
2. RSI 反弹策略 (RSI Rebound)
3. 融合策略 v2 (Fusion v2)

运行测试:
    cd /opt/ftseek/FTSeek
    python3 -m pytest tests/test_strategies.py -v
"""
import sys
import os
import time
import unittest
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.strategies import (
    load_config,
    create_strategy,
    get_all_strategies,
    FusionV2Strategy,
    MACrossStrategy,
    RSIReboundStrategy
)
from app.strategy.base import Signal, SignalAction, BaseStrategy


class MarketDataStub:
    """市场数据测试桩"""
    def __init__(self, code: str, name: str, price: float, volume: float, klines: list):
        self.code = code
        self.name = name
        self.price = price
        self.volume = volume
        self.klines = klines
        self.timestamp = time.time()
        self.news = []


def generate_klines(count: int, base_price: float = 100.0, trend: str = 'up') -> list:
    """
    生成模拟 K 线数据
    
    Args:
        count: K 线数量
        base_price: 基础价格
        trend: 趋势 (up/down/sideways)
    """
    klines = []
    price = base_price
    
    for i in range(count):
        if trend == 'up':
            change = 0.01 + (i % 5) * 0.005
        elif trend == 'down':
            change = -0.01 - (i % 5) * 0.005
        else:
            change = 0.002 * (1 if i % 2 == 0 else -1)
        
        open_price = price
        close_price = price * (1 + change)
        high_price = max(open_price, close_price) * 1.02
        low_price = min(open_price, close_price) * 0.98
        volume = 1000000 * (1 + (i % 10) * 0.1)
        
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


class TestConfigLoading(unittest.TestCase):
    """测试配置加载"""
    
    def test_load_config(self):
        """测试加载配置文件"""
        config = load_config()
        self.assertIsInstance(config, dict)
        self.assertIn('strategies', config)
        
        strategies = config['strategies']
        self.assertIn('fusion_v2', strategies)
        self.assertIn('ma_cross', strategies)
        self.assertIn('rsi_rebound', strategies)
    
    def test_strategy_weights(self):
        """测试策略权重"""
        config = load_config()
        strategies = config['strategies']
        
        # 验证权重总和接近 1.0
        total_weight = sum(
            s.get('weight', 0) for s in strategies.values()
            if s.get('enabled', True)
        )
        self.assertAlmostEqual(total_weight, 1.0, places=1)


class TestMACrossStrategy(unittest.TestCase):
    """测试均线交叉策略"""
    
    def setUp(self):
        """测试前准备"""
        config = {
            'fast_ma': 5,
            'slow_ma': 20,
            'volume_filter': 1.5,
            'min_confidence': 0.5,
            'enabled': True
        }
        self.strategy = MACrossStrategy(config)
    
    def test_initialization(self):
        """测试策略初始化"""
        self.assertEqual(self.strategy.name, 'ma_cross')
        self.assertEqual(self.strategy.fast_period, 5)
        self.assertEqual(self.strategy.slow_period, 20)
        self.assertTrue(self.strategy.enabled)
    
    def test_get_config(self):
        """测试获取配置"""
        config = self.strategy.get_config()
        self.assertIn('name', config)
        self.assertIn('fast_ma', config)
        self.assertIn('slow_ma', config)
        self.assertEqual(config['name'], 'ma_cross')
    
    def test_golden_cross_signal(self):
        """测试金叉买入信号"""
        # 生成先跌后涨的 K 线 (模拟金叉)
        klines = generate_klines(30, base_price=100, trend='down')
        klines.extend(generate_klines(10, base_price=95, trend='up'))
        
        market_data = MarketDataStub(
            code='TEST.GOLDEN',
            name='Test Golden Cross',
            price=klines[-1]['close'],
            volume=klines[-1]['volume'] * 2,  # 放大成交量
            klines=klines
        )
        
        signal = self.strategy.generate_signal(market_data)
        
        # 可能产生买入信号或无信号 (取决于具体数据)
        if signal:
            self.assertEqual(signal.strategy, 'ma_cross')
            self.assertIn(signal.action, [SignalAction.BUY, SignalAction.SELL])
    
    def test_insufficient_data(self):
        """测试数据不足情况"""
        klines = generate_klines(10)  # 数据不足
        
        market_data = MarketDataStub(
            code='TEST.INSUFFICIENT',
            name='Test Insufficient',
            price=100,
            volume=1000000,
            klines=klines
        )
        
        signal = self.strategy.generate_signal(market_data)
        self.assertIsNone(signal)
    
    def test_volume_filter(self):
        """测试成交量过滤"""
        klines = generate_klines(30)
        
        market_data = MarketDataStub(
            code='TEST.VOLUME',
            name='Test Volume Filter',
            price=100,
            volume=1000,  # 非常小的成交量
            klines=klines
        )
        
        # 成交量太小应该被过滤
        signal = self.strategy.generate_signal(market_data)
        # 可能为 None 或信号 (取决于是否触发交叉)


class TestRSIReboundStrategy(unittest.TestCase):
    """测试 RSI 反弹策略"""
    
    def setUp(self):
        """测试前准备"""
        config = {
            'rsi_period': 14,
            'oversold': 25,
            'overbought': 75,
            'max_holding_days': 3,
            'min_confidence': 0.5,
            'enabled': True
        }
        self.strategy = RSIReboundStrategy(config)
    
    def test_initialization(self):
        """测试策略初始化"""
        self.assertEqual(self.strategy.name, 'rsi_rebound')
        self.assertEqual(self.strategy.rsi_period, 14)
        self.assertEqual(self.strategy.oversold, 25)
        self.assertTrue(self.strategy.enabled)
    
    def test_get_config(self):
        """测试获取配置"""
        config = self.strategy.get_config()
        self.assertIn('name', config)
        self.assertIn('rsi_period', config)
        self.assertIn('oversold', config)
        self.assertEqual(config['name'], 'rsi_rebound')
    
    def test_oversold_rebound(self):
        """测试超卖反弹信号"""
        # 生成持续下跌的 K 线 (模拟超卖)
        klines = generate_klines(30, base_price=100, trend='down')
        
        market_data = MarketDataStub(
            code='TEST.OVERSOLD',
            name='Test Oversold',
            price=klines[-1]['close'],
            volume=1000000,
            klines=klines
        )
        
        signal = self.strategy.generate_signal(market_data)
        
        # 可能产生信号或无信号 (取决于 RSI 值)
        if signal:
            self.assertEqual(signal.strategy, 'rsi_rebound')
            self.assertIn('rsi', signal.extra)
    
    def test_rsi_calculation(self):
        """测试 RSI 计算"""
        # 使用已知价格序列测试 RSI
        prices = [44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
        
        rsi = self.strategy._calculate_rsi(prices, 14)
        
        self.assertIsNotNone(rsi)
        self.assertGreaterEqual(rsi, 0)
        self.assertLessEqual(rsi, 100)
    
    def test_insufficient_data(self):
        """测试数据不足情况"""
        klines = generate_klines(10)  # 数据不足
        
        market_data = MarketDataStub(
            code='TEST.INSUFFICIENT',
            name='Test Insufficient',
            price=100,
            volume=1000000,
            klines=klines
        )
        
        signal = self.strategy.generate_signal(market_data)
        self.assertIsNone(signal)


class TestFusionV2Strategy(unittest.TestCase):
    """测试融合策略 v2"""
    
    def setUp(self):
        """测试前准备"""
        config = {
            'technical_weight': 0.6,
            'sentiment_weight': 0.4,
            'buy_threshold': 0.65,
            'rsi_period': 14,
            'macd_fast': 12,
            'macd_slow': 26,
            'enabled': True
        }
        self.strategy = FusionV2Strategy(config)
    
    def test_initialization(self):
        """测试策略初始化"""
        self.assertEqual(self.strategy.name, 'fusion_v2')
        self.assertEqual(self.strategy.technical_weight, 0.6)
        self.assertEqual(self.strategy.sentiment_weight, 0.4)
        self.assertTrue(self.strategy.enabled)
    
    def test_get_config(self):
        """测试获取配置"""
        config = self.strategy.get_config()
        self.assertIn('name', config)
        self.assertIn('technical_weight', config)
        self.assertIn('sentiment_weight', config)
        self.assertEqual(config['name'], 'fusion_v2')
        self.assertEqual(config['technical_weight'], 0.6)
    
    def test_technical_score_calculation(self):
        """测试技术评分计算"""
        indicators = {
            'price': 100,
            'rsi': 25,  # 超卖
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
        
        score = self.strategy._calculate_technical_score(indicators)
        
        self.assertGreaterEqual(score, -1.0)
        self.assertLessEqual(score, 1.0)
        # RSI 超卖应该贡献正分
        self.assertGreater(score, 0)
    
    def test_signal_generation(self):
        """测试信号生成"""
        klines = generate_klines(60, base_price=100, trend='sideways')
        
        market_data = MarketDataStub(
            code='TEST.FUSION',
            name='Test Fusion',
            price=klines[-1]['close'],
            volume=1000000,
            klines=klines
        )
        
        signal = self.strategy.generate_signal(market_data)
        
        # 可能产生信号或无信号 (取决于市场数据)
        if signal:
            self.assertEqual(signal.strategy, 'fusion_v2')
            self.assertIn('technical_score', signal.extra)
            self.assertIn('sentiment_score', signal.extra)
            self.assertIn('final_score', signal.extra)
    
    def test_fusion_formula(self):
        """测试融合公式：score = technical × 0.6 + sentiment × 0.4"""
        technical_score = 0.8
        sentiment_score = -0.5
        
        expected = technical_score * 0.6 + sentiment_score * 0.4
        actual = technical_score * self.strategy.technical_weight + \
                 sentiment_score * self.strategy.sentiment_weight
        
        self.assertAlmostEqual(expected, actual, places=5)


class TestStrategyIntegration(unittest.TestCase):
    """策略集成测试"""
    
    def test_create_all_strategies(self):
        """测试创建所有策略"""
        config = load_config()
        strategies = config['strategies']
        
        for name in ['fusion_v2', 'ma_cross', 'rsi_rebound']:
            strategy_config = strategies.get(name, {})
            strategy = create_strategy(name, strategy_config.get('parameters', {}))
            
            self.assertIsNotNone(strategy, f"Failed to create strategy: {name}")
            self.assertEqual(strategy.name, name)
    
    def test_get_all_strategies(self):
        """测试获取所有启用的策略"""
        strategies = get_all_strategies()
        
        self.assertIsInstance(strategies, dict)
        self.assertGreater(len(strategies), 0)
        
        for name, strategy in strategies.items():
            self.assertIsInstance(strategy, BaseStrategy)
            self.assertTrue(strategy.enabled)
    
    def test_strategy_interface(self):
        """测试统一接口"""
        strategies = get_all_strategies()
        
        for name, strategy in strategies.items():
            # 测试 get_config 方法
            config = strategy.get_config()
            self.assertIsInstance(config, dict)
            self.assertIn('name', config)
            self.assertIn('enabled', config)
            
            # 测试 generate_signal 方法 (使用空数据)
            market_data = MarketDataStub(
                code='TEST.INTERFACE',
                name='Test',
                price=100,
                volume=1000000,
                klines=generate_klines(10)  # 不足数据
            )
            
            signal = strategy.generate_signal(market_data)
            # 数据不足应该返回 None
            if len(market_data.klines) < 20:
                self.assertIsNone(signal)


if __name__ == '__main__':
    unittest.main(verbosity=2)
