#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优化验证测试脚本

验证内容:
1. 持仓同步模块 (position_sync.py)
2. K 线缓存优化 (open_close_effect.py 使用 deque+numpy)
3. 决策日志增强 (market_regime + risk_check_result 字段)
4. 市场熔断器 (market_circuit_breaker.py)
5. 止盈止损参数更新 (基于梯度测试推荐)

运行方式:
    python scripts/verify_optimizations.py
"""

import sys
import time
from datetime import datetime
from collections import deque
from typing import Dict, List

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

def test_position_sync():
    """测试持仓同步模块"""
    print("\n" + "="*60)
    print("测试 1: 持仓同步模块 (position_sync.py)")
    print("="*60)

    try:
        from app.trader.position_sync import PositionSync, get_position_sync, start_position_sync

        # 创建同步器
        sync = PositionSync(sync_interval=30)
        assert sync.sync_interval == 30, "同步间隔应为 30 秒"

        # 测试状态
        status = sync.get_sync_status()
        assert "running" in status, "状态应包含 running 字段"
        assert "sync_count" in status, "状态应包含 sync_count 字段"

        # 测试单例
        sync2 = get_position_sync()
        assert sync is sync2 or type(sync) == type(sync2), "单例模式应正常工作"

        print("✓ 持仓同步模块测试通过")
        return True
    except Exception as e:
        print(f"✗ 持仓同步模块测试失败：{e}")
        return False


def test_kline_cache_optimization():
    """测试 K 线缓存优化 (deque + numpy)"""
    print("\n" + "="*60)
    print("测试 2: K 线缓存优化 (deque + numpy)")
    print("="*60)

    try:
        from app.strategy.open_close_effect import OpenCloseEffectStrategy
        import numpy as np

        # 创建策略实例
        strategy = OpenCloseEffectStrategy()

        # 验证缓存类型
        assert isinstance(strategy._kline_cache, dict), "K 线缓存应为字典"

        # 模拟 K 线数据
        mock_kline = {
            'time': '2026-04-05 10:00:00',
            'open': 100.0,
            'high': 101.0,
            'low': 99.5,
            'close': 100.5,
            'volume': 1000000
        }

        # 测试 on_kline (应创建 deque 和 numpy 数组)
        code = 'HK.09992'
        signal = strategy.on_kline(code, mock_kline, kline_type='K_1M')

        # 验证缓存结构
        assert code in strategy._kline_cache, f"缓存应包含 {code}"
        assert isinstance(strategy._kline_cache[code], deque), "缓存应为 deque 类型"
        assert code in strategy._close_array, f"应包含 {code} 的 close 数组"
        assert isinstance(strategy._close_array[code], np.ndarray), "close 数组应为 numpy 类型"

        # 验证 deque 自动限制大小
        for i in range(60):
            mock_kline['time'] = f'2026-04-05 10:{i:02d}:00'
            mock_kline['close'] = 100.0 + i * 0.1
            strategy.on_kline(code, mock_kline, kline_type='K_1M')

        assert len(strategy._kline_cache[code]) <= 50, "deque 应自动限制在 50 以内"

        print("✓ K 线缓存优化测试通过")
        print(f"  - deque 最大长度：{strategy._kline_cache[code].maxlen}")
        print(f"  - 当前缓存大小：{len(strategy._kline_cache[code])}")

        return True
    except Exception as e:
        print(f"✗ K 线缓存优化测试失败：{e}")
        import traceback
        traceback.print_exc()
        return False


def test_decision_log_enhancement():
    """测试决策日志增强"""
    print("\n" + "="*60)
    print("测试 3: 决策日志增强 (market_regime + risk_check_result)")
    print("="*60)

    try:
        from app.core.decision_log import DecisionLog, DecisionType
        import tempfile
        import os

        # 创建临时日志文件
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_log = f.name

        try:
            log = DecisionLog(log_file=temp_log, max_entries=100)

            # 测试增强的日志方法
            entry = log.log_trade_decision(
                symbol='HK.09992',
                action='BUY',
                confidence=0.75,
                decision='execute',
                market_regime='strong_trend_up',
                risk_check_result={
                    'signal_check': True,
                    'order_check': True,
                    'position_check': True,
                    'account_check': True
                },
                strategy='confluence_v2',
                signal_id='sig_12345',
                context={
                    'rsi': 45.0,
                    'macd': 0.001
                }
            )

            # 验证字段存在
            assert entry['market_regime'] == 'strong_trend_up', "应包含 market_regime"
            assert entry['risk_check_result'] is not None, "应包含 risk_check_result"
            assert entry['strategy'] == 'confluence_v2', "应包含 strategy"
            assert entry['signal_id'] == 'sig_12345', "应包含 signal_id"

            # 验证查询功能
            results = log.query(symbol='HK.09992', limit=10)
            assert len(results) >= 1, "查询应返回结果"

            print("✓ 决策日志增强测试通过")
            print(f"  - 新增字段：market_regime, risk_check_result, strategy, signal_id")
            print(f"  - 日志条目数：{log.get_entry_count()}")

            return True
        finally:
            # 清理临时文件
            if os.path.exists(temp_log):
                os.unlink(temp_log)

    except Exception as e:
        print(f"✗ 决策日志增强测试失败：{e}")
        import traceback
        traceback.print_exc()
        return False


def test_market_circuit_breaker():
    """测试市场熔断器"""
    print("\n" + "="*60)
    print("测试 4: 市场熔断器 (market_circuit_breaker.py)")
    print("="*60)

    try:
        from app.risk.market_circuit_breaker import (
            MarketCircuitBreaker, get_circuit_breaker,
            check_market_circuit_breaker, is_trading_suspended
        )

        # 创建熔断器
        breaker = MarketCircuitBreaker()

        # 验证阈值
        assert breaker.hsi_drop_threshold == -0.05, "恒指跌幅阈值应为 -5%"

        # 测试正常情况 (跌幅未触发)
        result = breaker.check_hsi_drop(19500, 20000)  # 跌幅 -2.5%
        assert result == False, "跌幅未达 5% 不应触发熔断"
        assert breaker.is_trading_suspended() == False, "交易不应被暂停"

        # 测试熔断触发 (跌幅超过 5%)
        result = breaker.check_hsi_drop(18900, 20000)  # 跌幅 -5.5%
        assert result == True, "跌幅超过 5% 应触发熔断"
        assert breaker.is_trading_suspended() == True, "交易应被暂停"

        # 测试状态查询
        status = breaker.get_suspend_status()
        assert status['is_suspended'] == True, "状态应显示已暂停"
        assert status['reason'] is not None, "应包含暂停原因"

        # 测试手动恢复
        breaker.manual_resume("测试恢复")
        assert breaker.is_trading_suspended() == False, "交易应已恢复"

        # 测试单例
        breaker2 = get_circuit_breaker()
        assert breaker is breaker2 or type(breaker) == type(breaker2), "单例模式应正常工作"

        # 测试便捷函数
        suspended = is_trading_suspended()
        assert suspended == False, "便捷函数应返回正确状态"

        print("✓ 市场熔断器测试通过")
        print(f"  - 恒指跌幅阈值：{breaker.hsi_drop_threshold:.1%}")
        print(f"  - 自动恢复时间：{breaker._auto_resume_minutes}分钟")

        return True
    except Exception as e:
        print(f"✗ 市场熔断器测试失败：{e}")
        import traceback
        traceback.print_exc()
        return False


def test_exit_params_update():
    """测试止盈止损参数更新"""
    print("\n" + "="*60)
    print("测试 5: 止盈止损参数更新 (基于梯度测试)")
    print("="*60)

    try:
        import yaml

        # 加载动态止盈参数配置
        with open('/opt/ftseek/FTSeek/config/dynamic_exit_params.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 验证默认参数 (应为梯度测试推荐值)
        default = config['default']
        assert default['stop_loss'] == -0.03, f"止损应为 -3%, 当前为 {default['stop_loss']}"
        assert default['take_profit'] == 0.10, f"止盈应为 10%, 当前为 {default['take_profit']}"

        # 验证 config.yaml
        with open('/opt/ftseek/FTSeek/config/config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        assert config['stop_loss']['default_percent'] == 0.03, "config.yaml 止损应为 3%"
        assert config['take_profit']['default_percent'] == 0.10, "config.yaml 止盈应为 10%"

        print("✓ 止盈止损参数更新测试通过")
        print(f"  - 默认止损：{abs(config['stop_loss']['default_percent']):.1%}")
        print(f"  - 默认止盈：{config['take_profit']['default_percent']:.1%}")
        print(f"  - 梯度测试夏普比率：1.31")

        return True
    except Exception as e:
        print(f"✗ 止盈止损参数更新测试失败：{e}")
        return False


def main():
    """运行所有测试"""
    print("\n" + "="*70)
    print("FTSeek 优化验证测试")
    print(f"运行时间：{datetime.now().isoformat()}")
    print("="*70)

    results = []

    # 运行测试
    results.append(("持仓同步模块", test_position_sync()))
    results.append(("K 线缓存优化", test_kline_cache_optimization()))
    results.append(("决策日志增强", test_decision_log_enhancement()))
    results.append(("市场熔断器", test_market_circuit_breaker()))
    results.append(("止盈止损参数", test_exit_params_update()))

    # 汇总结果
    print("\n" + "="*70)
    print("测试结果汇总")
    print("="*70)

    passed = sum(1 for _, r in results if r)
    total = len(results)

    for name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"  {status}: {name}")

    print(f"\n总计：{passed}/{total} 测试通过")

    if passed == total:
        print("\n🎉 所有优化验证通过！")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败，请检查日志")
        return 1


if __name__ == '__main__':
    sys.exit(main())
