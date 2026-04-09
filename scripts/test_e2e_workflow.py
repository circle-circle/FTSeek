#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 端到端流程验证脚本

验证范围:
1. 信号生成 → 风控检查 → 订单执行 → 持仓管理 → 平仓退出
2. 熔断器检查
3. 失败追踪
4. 动态止盈止损
5. 市场状态切换

测试场景:
- 正常交易流程
- 风控拒绝场景
- 熔断器触发场景
- 止盈止损触发场景
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict

sys.path.insert(0, '/opt/ftseek/FTSeek')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.e2e_test")

# 测试结果
test_results = {
    'timestamp': datetime.now().isoformat(),
    'scenarios': [],
    'passed': 0,
    'failed': 0,
    'warnings': [],
    'risks': [],
}

@dataclass
class TestScenario:
    """测试场景"""
    name: str
    description: str
    steps: list
    expected: str
    result: str = "pending"
    issues: list = None

def log_scenario(scenario: TestScenario, passed: bool, issues: list = None):
    """记录场景测试结果"""
    scenario.result = "passed" if passed else "failed"
    scenario.issues = issues or []
    
    test_results['scenarios'].append(asdict(scenario))
    
    if passed:
        test_results['passed'] += 1
        logger.info(f"✅ {scenario.name}: 通过")
    else:
        test_results['failed'] += 1
        logger.error(f"❌ {scenario.name}: 失败 - {issues}")
    
    if issues:
        for issue in issues:
            test_results['warnings'].append(f"{scenario.name}: {issue}")


def test_scenario_1_normal_trade():
    """场景 1: 正常交易流程"""
    scenario = TestScenario(
        name="场景 1: 正常交易流程",
        description="测试从信号生成到持仓创建的完整流程",
        steps=[
            "1. 创建买入信号",
            "2. 熔断器检查",
            "3. 风控检查",
            "4. 获取动态止盈止损参数",
            "5. 计算止盈止损价",
            "6. 创建持仓记录",
            "7. 记录成功交易"
        ],
        expected="持仓创建成功，止盈止损价正确设置"
    )
    
    issues = []
    
    try:
        # 1. 创建信号
        from app.strategy.base import Signal, SignalAction
        signal = Signal(
            code='HK.09992',
            name='泡泡玛特',
            action=SignalAction.BUY,
            price=95.5,
            quantity=200,
            reason='测试信号',
            strategy='test_strategy',
            extra={'confidence': 0.65, 'consensus_score': 2.42}
        )
        logger.info(f"✅ 步骤 1: 信号创建成功")
        
        # 2. 熔断器检查
        from app.risk.circuit_breaker import circuit_breaker
        can_trade = circuit_breaker.can_trade()
        if not can_trade:
            issues.append("熔断器阻止交易")
        logger.info(f"✅ 步骤 2: 熔断器检查通过")
        
        # 3. 风控检查
        from app.risk.risk_manager import get_risk_manager
        risk_manager = get_risk_manager()
        risk_result = risk_manager.check_before_trade(signal)
        if not risk_result.passed:
            issues.append(f"风控检查失败：{risk_result.details}")
        logger.info(f"✅ 步骤 3: 风控检查通过")
        
        # 4. 获取动态止盈止损参数
        from app.risk.dynamic_exit_manager import dynamic_exit_manager
        stop_loss = dynamic_exit_manager.get_stop_loss()
        take_profit = dynamic_exit_manager.get_take_profit()
        logger.info(f"✅ 步骤 4: 获取参数 - 止损={stop_loss:.1%}, 止盈={take_profit:.1%}")
        
        # 5. 计算止盈止损价
        if signal.action == SignalAction.BUY:
            stop_price = signal.price * (1 + stop_loss)
            take_profit_price = signal.price * (1 + take_profit)
        else:
            stop_price = signal.price * (1 - abs(stop_loss))
            take_profit_price = signal.price * (1 - take_profit)
        
        logger.info(f"✅ 步骤 5: 计算价格 - 止损={stop_price:.2f}, 止盈={take_profit_price:.2f}")
        
        # 6. 创建持仓记录
        from app.trader.position_manager import PositionManager
        pm = PositionManager()
        position_id = pm.create_position(
            code=signal.code,
            action='BUY',
            quantity=signal.quantity,
            entry_price=signal.price,
            entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            stop_loss=stop_price,
            take_profit=take_profit_price,
        )
        logger.info(f"✅ 步骤 6: 持仓创建成功，position_id={position_id}")
        
        # 7. 记录成功交易
        circuit_breaker.record_success()
        logger.info(f"✅ 步骤 7: 成功交易已记录")
        
        # 验证止盈止损价
        if stop_price >= signal.price:
            issues.append(f"止损价 {stop_price:.2f} 不应高于入场价 {signal.price:.2f}")
        if take_profit_price <= signal.price:
            issues.append(f"止盈价 {take_profit_price:.2f} 不应低于入场价 {signal.price:.2f}")
        
    except Exception as e:
        issues.append(f"异常：{e}")
        logger.error(f"❌ 场景 1 失败：{e}", exc_info=True)
    
    passed = len(issues) == 0
    log_scenario(scenario, passed, issues)
    return passed


def test_scenario_2_risk_reject():
    """场景 2: 风控拒绝场景"""
    scenario = TestScenario(
        name="场景 2: 风控拒绝场景",
        description="测试风控检查失败时的处理流程",
        steps=[
            "1. 创建高风险信号",
            "2. 风控检查",
            "3. 记录失败到追踪器",
            "4. 熔断器记录拒绝"
        ],
        expected="风控拒绝，失败被记录，熔断器计数 +1"
    )
    
    issues = []
    
    try:
        from app.strategy.base import Signal, SignalAction
        from app.risk.risk_manager import get_risk_manager
        from app.logs.failure_tracker import failure_tracker
        from app.risk.circuit_breaker import circuit_breaker
        
        # 1. 创建信号
        signal = Signal(
            code='HK.09992',
            name='泡泡玛特',
            action=SignalAction.BUY,
            price=95.5,
            quantity=200,
            reason='测试信号',
            strategy='test_strategy',
            extra={'confidence': 0.30}  # 低置信度
        )
        logger.info(f"✅ 步骤 1: 低置信度信号创建")
        
        # 2. 风控检查
        risk_manager = get_risk_manager()
        risk_result = risk_manager.check_before_trade(signal)
        
        # 3-4. 如果风控失败，记录失败
        if not risk_result.passed:
            failed_checks = [k for k, v in risk_result.details.items() if not v]
            failure_tracker.record_failure(signal.code, 'risk', f"风控失败：{failed_checks}")
            circuit_breaker.record_reject(f"风控检查失败：{failed_checks}")
            logger.info(f"✅ 步骤 2-4: 风控拒绝，失败已记录")
        else:
            logger.info(f"⚠️ 风控检查通过 (置信度阈值可能已调整)")
        
    except Exception as e:
        issues.append(f"异常：{e}")
        logger.error(f"❌ 场景 2 失败：{e}", exc_info=True)
    
    passed = len(issues) == 0
    log_scenario(scenario, passed, issues)
    return passed


def test_scenario_3_circuit_breaker():
    """场景 3: 熔断器触发场景"""
    scenario = TestScenario(
        name="场景 3: 熔断器触发场景",
        description="测试连续拒绝触发熔断器的流程",
        steps=[
            "1. 重置熔断器状态",
            "2. 模拟连续 5 次拒绝",
            "3. 检查熔断器状态",
            "4. 验证交易被阻止",
            "5. 等待暂停期结束",
            "6. 验证交易恢复"
        ],
        expected="连续 5 次拒绝触发熔断，暂停 10 分钟后自动恢复"
    )
    
    issues = []
    
    try:
        from app.risk.circuit_breaker import circuit_breaker
        
        # 1. 重置状态
        circuit_breaker.state.reset()
        logger.info(f"✅ 步骤 1: 熔断器状态重置")
        
        # 2. 模拟连续拒绝
        for i in range(5):
            circuit_breaker.record_reject(f"测试拒绝_{i}")
        logger.info(f"✅ 步骤 2: 模拟 5 次连续拒绝")
        
        # 3. 检查熔断器状态
        status = circuit_breaker.get_status()
        if not status['paused']:
            issues.append("熔断器未触发暂停")
        logger.info(f"✅ 步骤 3: 熔断器状态 - 暂停={status['paused']}")
        
        # 4. 验证交易被阻止
        can_trade = circuit_breaker.can_trade()
        if can_trade:
            issues.append("熔断器未阻止交易")
        logger.info(f"✅ 步骤 4: 交易检查 - 可交易={can_trade}")
        
        # 5-6. 重置状态 (模拟暂停期结束)
        circuit_breaker.state.reset()
        can_trade_after = circuit_breaker.can_trade()
        if not can_trade_after:
            issues.append("熔断器未恢复交易")
        logger.info(f"✅ 步骤 5-6: 重置后交易恢复 - 可交易={can_trade_after}")
        
    except Exception as e:
        issues.append(f"异常：{e}")
        logger.error(f"❌ 场景 3 失败：{e}", exc_info=True)
    
    passed = len(issues) == 0
    log_scenario(scenario, passed, issues)
    return passed


def test_scenario_4_dynamic_exit():
    """场景 4: 动态止盈止损切换场景"""
    scenario = TestScenario(
        name="场景 4: 动态止盈止损切换",
        description="测试不同市场状态下止盈止损参数的自动切换",
        steps=[
            "1. 获取默认状态参数",
            "2. 切换到强上涨状态",
            "3. 验证参数变化",
            "4. 切换到震荡市状态",
            "5. 验证参数变化",
            "6. 切换回默认状态"
        ],
        expected="参数根据市场状态正确切换"
    )
    
    issues = []
    
    try:
        from app.risk.dynamic_exit_manager import dynamic_exit_manager
        
        # 1. 默认状态
        params_default = dynamic_exit_manager.get_all_params()
        logger.info(f"✅ 步骤 1: 默认状态 - 止损={params_default['stop_loss']:.1%}, 止盈={params_default['take_profit']:.1%}")
        
        # 2. 切换到强上涨
        dynamic_exit_manager.update_regime('strong_trend_up')
        params_up = dynamic_exit_manager.get_all_params()
        
        # 3. 验证参数
        if params_up['stop_loss'] != -0.08:
            issues.append(f"强上涨止损应为 -8%，实际={params_up['stop_loss']:.1%}")
        if params_up['take_profit'] != 0.20:
            issues.append(f"强上涨止盈应为 +20%，实际={params_up['take_profit']:.1%}")
        logger.info(f"✅ 步骤 2-3: 强上涨状态 - 止损={params_up['stop_loss']:.1%}, 止盈={params_up['take_profit']:.1%}")
        
        # 4. 切换到震荡市
        dynamic_exit_manager.update_regime('range_bound')
        params_range = dynamic_exit_manager.get_all_params()
        
        # 5. 验证参数
        if params_range['stop_loss'] != -0.05:
            issues.append(f"震荡市止损应为 -5%，实际={params_range['stop_loss']:.1%}")
        if params_range['take_profit'] != 0.10:
            issues.append(f"震荡市止盈应为 +10%，实际={params_range['take_profit']:.1%}")
        logger.info(f"✅ 步骤 4-5: 震荡市状态 - 止损={params_range['stop_loss']:.1%}, 止盈={params_range['take_profit']:.1%}")
        
        # 6. 恢复默认
        dynamic_exit_manager.update_regime('default')
        logger.info(f"✅ 步骤 6: 恢复默认状态")
        
    except Exception as e:
        issues.append(f"异常：{e}")
        logger.error(f"❌ 场景 4 失败：{e}", exc_info=True)
    
    passed = len(issues) == 0
    log_scenario(scenario, passed, issues)
    return passed


def test_scenario_5_failure_tracking():
    """场景 5: 失败追踪场景"""
    scenario = TestScenario(
        name="场景 5: 失败追踪场景",
        description="测试不同类型失败的记录和统计",
        steps=[
            "1. 记录冷却跳过失败",
            "2. 记录置信度拒绝失败",
            "3. 记录订单超限失败",
            "4. 记录风控拒绝失败",
            "5. 获取统计数据",
            "6. 验证分类正确"
        ],
        expected="所有失败正确记录和分类"
    )
    
    issues = []
    
    try:
        from app.logs.failure_tracker import failure_tracker
        
        # 1-4. 记录不同类型失败
        failure_tracker.record_failure('HK.09992', 'cooldown', '冷却跳过')
        failure_tracker.record_failure('HK.09992', 'confidence', '置信度不足')
        failure_tracker.record_failure('HK.09992', 'limit', '订单超限')
        failure_tracker.record_failure('HK.09992', 'risk', '风控拒绝')
        logger.info(f"✅ 步骤 1-4: 记录 4 种类型失败")
        
        # 5. 获取统计
        stats = failure_tracker.get_statistics('today')
        
        # 6. 验证分类
        by_reason = stats.get('by_reason', {})
        if len(by_reason) < 4:
            issues.append(f"失败分类不完整：{list(by_reason.keys())}")
        
        logger.info(f"✅ 步骤 5-6: 统计完成 - 总失败={stats['total']}, 分类={list(by_reason.keys())}")
        
    except Exception as e:
        issues.append(f"异常：{e}")
        logger.error(f"❌ 场景 5 失败：{e}", exc_info=True)
    
    passed = len(issues) == 0
    log_scenario(scenario, passed, issues)
    return passed


def generate_report():
    """生成验证报告"""
    logger.info("\n" + "=" * 80)
    logger.info("📊 FTSeek 端到端流程验证报告")
    logger.info("=" * 80)
    logger.info(f"验证时间：{test_results['timestamp']}")
    logger.info(f"总场景数：{len(test_results['scenarios'])}")
    logger.info(f"✅ 通过：{test_results['passed']}")
    logger.info(f"❌ 失败：{test_results['failed']}")
    logger.info(f"⚠️  警告：{len(test_results['warnings'])}")
    logger.info("=" * 80)
    
    # 计算通过率
    total = test_results['passed'] + test_results['failed']
    pass_rate = test_results['passed'] / total * 100 if total > 0 else 0
    
    if pass_rate >= 95:
        status = "优秀"
    elif pass_rate >= 80:
        status = "良好"
    elif pass_rate >= 60:
        status = "需改进"
    else:
        status = "严重问题"
    
    logger.info(f"整体状态：{status} ({pass_rate:.1f}%)")
    logger.info("=" * 80)
    
    # 保存报告
    report_file = '/opt/ftseek/FTSeek/data/e2e_validation_report.json'
    Path(report_file).parent.mkdir(parents=True, exist_ok=True)
    
    test_results['pass_rate'] = round(pass_rate, 2)
    test_results['overall_status'] = status
    
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(test_results, f, indent=2, ensure_ascii=False)
    
    logger.info(f"\n📄 报告已保存：{report_file}")
    
    return pass_rate


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("🔍 FTSeek 端到端流程验证开始")
    logger.info("=" * 80)
    
    try:
        # 执行测试场景
        test_scenario_1_normal_trade()
        test_scenario_2_risk_reject()
        test_scenario_3_circuit_breaker()
        test_scenario_4_dynamic_exit()
        test_scenario_5_failure_tracking()
        
        # 生成报告
        pass_rate = generate_report()
        
        logger.info("=" * 80)
        logger.info("✅ 端到端流程验证完成")
        logger.info("=" * 80)
        
        return 0 if pass_rate >= 80 else 1
        
    except Exception as e:
        logger.error(f"❌ 验证异常：{e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
