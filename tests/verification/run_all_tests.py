#!/usr/bin/env python3
"""
量化评价与反馈系统 - 完整验证脚本

用法:
    python3 -m tests.verification.run_all_tests

输出:
    - 逻辑验证报告
    - 链路验证报告
    - 性能验证报告
    - 收益验证报告
"""
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any
from dataclasses import dataclass

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("verification")

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')


@dataclass
class TestResult:
    """测试结果"""
    name: str
    passed: bool
    duration_ms: float
    error: str = ""
    details: str = ""

    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'passed': self.passed,
            'duration_ms': round(self.duration_ms, 2),
            'error': self.error,
            'details': self.details,
        }


class VerificationRunner:
    """验证执行器"""

    def __init__(self):
        self.results: List[TestResult] = []
        self.start_time = datetime.now()

    def run_test(self, name: str, test_func, **kwargs) -> TestResult:
        """运行单个测试"""
        logger.info(f"运行测试：{name}")
        start = time.perf_counter()

        try:
            test_func(**kwargs)
            result = TestResult(
                name=name,
                passed=True,
                duration_ms=(time.perf_counter() - start) * 1000,
                details="测试通过"
            )
        except Exception as e:
            result = TestResult(
                name=name,
                passed=False,
                duration_ms=(time.perf_counter() - start) * 1000,
                error=str(e)
            )
            logger.error(f"测试失败：{name} - {e}")

        self.results.append(result)
        return result

    def print_summary(self):
        """打印测试摘要"""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        pass_rate = passed / total * 100 if total > 0 else 0

        total_duration = sum(r.duration_ms for r in self.results)

        print("\n" + "=" * 70)
        print("验证测试摘要")
        print("=" * 70)
        print(f"总测试数：{total}")
        print(f"通过：{passed}")
        print(f"失败：{failed}")
        print(f"通过率：{pass_rate:.1f}%")
        print(f"总耗时：{total_duration:.0f}ms ({total_duration/1000:.1f}s)")
        print()

        # 按类别分组
        categories = {
            '逻辑验证': [],
            '链路验证': [],
            '性能验证': [],
            '收益验证': [],
        }

        for r in self.results:
            for cat in categories:
                if cat in r.name:
                    categories[cat].append(r)
                    break

        for cat, tests in categories.items():
            if tests:
                cat_passed = sum(1 for t in tests if t.passed)
                print(f"{cat}: {cat_passed}/{len(tests)} 通过")

        print("=" * 70)

        # 显示失败的测试
        failed_tests = [r for r in self.results if not r.passed]
        if failed_tests:
            print("\n失败的测试:")
            for r in failed_tests:
                print(f"  ✗ {r.name}: {r.error}")

    def save_report(self, output_path: str = None):
        """保存测试报告"""
        if not output_path:
            output_path = Path('/opt/ftseek/FTSeek/data/verification_report.json')

        report = {
            'executed_at': self.start_time.isoformat(),
            'completed_at': datetime.now().isoformat(),
            'total_tests': len(self.results),
            'passed': sum(1 for r in self.results if r.passed),
            'failed': sum(1 for r in self.results if not r.passed),
            'results': [r.to_dict() for r in self.results],
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.info(f"测试报告已保存到 {output_path}")


# ==================== Level 1: 逻辑验证 ====================

def verify_trade_chain_data_structure():
    """验证 TradeChain 数据结构"""
    from app.analytics.trade_chain import (
        TradeChain, SignalLayer, DecisionLayer,
        ExecutionLayer, ResultLayer
    )

    # 创建完整链路
    chain = TradeChain()
    chain.trade_id = "VERIFY-001"
    chain.code = "00700.HK"

    # 填充 4 层数据
    chain.signal = SignalLayer(
        signal_id="SIG-001",
        signal_score=0.75,
        signal_confidence=0.80,
        consensus_passed=True,
        consensus_weights={'technical': 0.4, 'sentiment': 0.3, 'ai': 0.3},
        market_regime='trending_up',
        volatility_atr=0.025,
        ai_sentiment_score=0.78
    )

    chain.decision = DecisionLayer(
        decision_reason='confluence',
        decision_confidence=8,
        position_planned=0.20,
        position_adjusted=0.20
    )

    chain.execution = ExecutionLayer(
        expected_price=380.0,
        actual_fill_price=380.5,
        slippage_bps=13.16,
        execution_latency_ms=100,
        fill_reason='market',
        total_cost=5.5
    )

    chain.result = ResultLayer(
        exit_price=395.0,
        exit_reason='take_profit',
        pnl=1500.0,
        pnl_percent=3.95,
        holding_period_minutes=120,
        max_profit_during=1600.0,
        max_loss_during=-200.0
    )

    chain.is_closed = True
    chain.is_profitable = True

    # 验证数据完整性
    assert chain.signal.signal_score == 0.75, "信号分数错误"
    assert chain.decision.decision_confidence == 8, "决策信心错误"
    assert chain.execution.slippage_bps == 13.16, "滑点错误"
    assert chain.result.pnl == 1500.0, "盈亏错误"

    # 验证序列化
    data = chain.to_dict()
    assert 'signal' in data
    assert 'decision' in data
    assert 'execution' in data
    assert 'result' in data

    # 验证反序列化
    chain2 = TradeChain.from_dict(data)
    assert chain2.signal.signal_score == 0.75


def verify_trade_chain_recorder():
    """验证 TradeChainRecorder"""
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()

    # 测试 start_chain
    chain = recorder.start_chain(
        trade_id="VERIFY-REC-001",
        code="00700.HK",
        signal_data={
            'signal_id': 'SIG-V-001',
            'signal_score': 0.82,
            'signal_confidence': 0.85,
            'consensus_passed': True,
            'market_regime': 'trending_up',
        },
        decision_data={
            'decision_reason': 'confluence',
            'decision_confidence': 8,
            'position_planned': 0.20,
        }
    )

    assert chain.trade_id == "VERIFY-REC-001"
    assert chain.code == "00700.HK"
    assert chain.signal.signal_score == 0.82

    # 测试 record_execution
    recorder.record_execution("VERIFY-REC-001", {
        'expected_price': 380.0,
        'actual_fill_price': 380.5,
        'slippage_bps': 13.16,
        'execution_latency_ms': 100,
        'total_cost': 5.5,
    })

    chain = recorder.get_chain("VERIFY-REC-001")
    assert chain.execution.actual_fill_price == 380.5

    # 测试 complete_chain
    recorder.complete_chain("VERIFY-REC-001", {
        'exit_price': 395.0,
        'exit_reason': 'take_profit',
        'pnl': 1500.0,
        'pnl_percent': 3.95,
    })

    chain = recorder.get_chain("VERIFY-REC-001")
    assert chain.is_closed == True
    assert chain.result.pnl == 1500.0


def verify_strategy_attribution():
    """验证策略归因分析"""
    from app.analytics.strategy_attribution import get_strategy_attribution

    attribution = get_strategy_attribution()

    # 验证分析方法可用
    by_strategy = attribution.by_strategy()
    assert isinstance(by_strategy, dict)

    by_regime = attribution.by_market_regime()
    assert isinstance(by_regime, dict)

    by_strength = attribution.by_signal_strength()
    assert isinstance(by_strength, dict)

    # 验证噪声信号识别
    noise = attribution.identify_noise_signals()
    assert isinstance(noise, list)

    # 验证过拟合信号识别
    overfit = attribution.identify_overfit_signals()
    assert isinstance(overfit, list)


def verify_signal_correlation():
    """验证信号相关性分析"""
    from app.analytics.signal_correlation import get_signal_quality_correlation

    correlation = get_signal_quality_correlation()

    # 验证相关性分析可用
    corr_result = correlation.score_vs_pnl_correlation()
    assert hasattr(corr_result, 'pearson_r')
    assert hasattr(corr_result, 'p_value')

    # 验证置信度 vs 胜率分析
    conf_result = correlation.confidence_vs_winrate()
    assert 'buckets' in conf_result
    assert 'anomaly_detected' in conf_result


def verify_optimization_advisor():
    """验证优化建议生成器"""
    from app.analytics.optimization_advisor import (
        OptimizationAdvisor, RecommendationType, RecommendationPriority
    )

    advisor = OptimizationAdvisor()

    # 测试建议生成
    recommendations = advisor.generate_recommendations(
        strategy_attribution={
            'by_strategy': {
                'technical': {'total_trades': 20, 'win_rate': 0.30, 'total_pnl': -500},
                'sentiment': {'total_trades': 15, 'win_rate': 0.60, 'total_pnl': 800},
            }
        },
        signal_correlation={
            'confidence_vs_winrate': {
                'anomaly_detected': False,
                'buckets': {}
            },
            'noise_signals': [],
            'overfit_signals': []
        },
        execution_stats={'avg_slippage_bps': 15, 'avg_latency_ms': 100}
    )

    assert isinstance(recommendations, list)


def verify_config_hot_reload():
    """验证配置热加载"""
    from app.core.config_hot_reload import get_config_hot_reloader

    reloader = get_config_hot_reloader()

    # 验证当前配置可获取
    config = reloader.get_current_config()
    assert isinstance(config, dict)

    # 验证版本历史
    history = reloader.get_version_history()
    assert isinstance(history, list)
    assert len(history) > 0

    # 验证回滚功能
    initial_version = reloader.active_version_id
    reloader.apply_recommendation({
        'type': 'upweight',
        'target': 'sentiment',
        'suggestion': '测试加权'
    })

    # 回滚
    success = reloader.rollback(initial_version)
    assert success == True


# ==================== Level 2: 链路验证 ====================

def verify_end_to_end_chain():
    """验证端到端链路"""
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()

    # 完整流程
    # 1. 启动链路
    chain = recorder.start_chain(
        trade_id="E2E-001",
        code="00700.HK",
        signal_data={
            'signal_id': 'SIG-E2E-001',
            'signal_score': 0.75,
            'signal_confidence': 0.80,
            'consensus_passed': True,
            'market_regime': 'trending_up',
        },
        decision_data={
            'decision_reason': 'confluence',
            'decision_confidence': 8,
            'position_planned': 0.20,
        }
    )

    # 2. 记录执行
    recorder.record_execution("E2E-001", {
        'expected_price': 380.0,
        'actual_fill_price': 380.5,
        'slippage_bps': 13.16,
        'total_cost': 5.5,
    })

    # 3. 完成链路
    recorder.complete_chain("E2E-001", {
        'exit_price': 395.0,
        'exit_reason': 'take_profit',
        'pnl': 1500.0,
        'pnl_percent': 3.95,
    })

    # 4. 验证完整链路
    chain = recorder.get_chain("E2E-001")
    assert chain is not None
    assert chain.signal is not None
    assert chain.decision is not None
    assert chain.execution is not None
    assert chain.result is not None
    assert chain.is_closed == True


def verify_statistics_chain():
    """验证统计链路"""
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()

    # 获取统计数据
    stats = recorder.get_statistics()

    assert 'total_trades' in stats
    assert 'closed_trades' in stats
    assert 'win_rate' in stats


# ==================== Level 3: 性能验证 ====================

def verify_performance_start_chain():
    """测试 start_chain 性能"""
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()

    start = time.perf_counter()
    recorder.start_chain(
        trade_id=f"PERF-{int(time.time())}",
        code="00700.HK",
        signal_data={'signal_score': 0.75, 'signal_confidence': 0.80},
        decision_data={'decision_reason': 'confluence', 'decision_confidence': 8}
    )
    duration_ms = (time.perf_counter() - start) * 1000

    assert duration_ms < 100, f"start_chain 耗时{duration_ms:.1f}ms > 100ms"


def verify_performance_full_report():
    """测试完整报告生成性能"""
    from app.analytics.strategy_attribution import get_strategy_attribution

    attribution = get_strategy_attribution()

    start = time.perf_counter()
    report = attribution.get_full_report()
    duration_ms = (time.perf_counter() - start) * 1000

    assert duration_ms < 1000, f"get_full_report 耗时{duration_ms:.1f}ms > 1000ms"


# ==================== 主执行函数 ====================

def run_all_verifications():
    """运行所有验证测试"""
    runner = VerificationRunner()

    # Level 1: 逻辑验证
    runner.run_test("逻辑验证 - TradeChain 数据结构", verify_trade_chain_data_structure)
    runner.run_test("逻辑验证 - TradeChainRecorder", verify_trade_chain_recorder)
    runner.run_test("逻辑验证 - StrategyAttribution", verify_strategy_attribution)
    runner.run_test("逻辑验证 - SignalCorrelation", verify_signal_correlation)
    runner.run_test("逻辑验证 - OptimizationAdvisor", verify_optimization_advisor)
    runner.run_test("逻辑验证 - ConfigHotReload", verify_config_hot_reload)

    # Level 2: 链路验证
    runner.run_test("链路验证 - 端到端链路", verify_end_to_end_chain)
    runner.run_test("链路验证 - 统计链路", verify_statistics_chain)

    # Level 3: 性能验证
    runner.run_test("性能验证 - start_chain", verify_performance_start_chain)
    runner.run_test("性能验证 - full_report", verify_performance_full_report)

    # 打印摘要
    runner.print_summary()

    # 保存报告
    runner.save_report()

    return runner


if __name__ == '__main__':
    runner = run_all_verifications()

    # 返回退出码
    passed = sum(1 for r in runner.results if r.passed)
    failed = len(runner.results) - passed

    sys.exit(0 if failed == 0 else 1)
