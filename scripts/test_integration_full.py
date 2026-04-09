#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 全链路集成测试脚本

测试范围:
1. 模块导入测试
2. 配置加载测试
3. 数据流向测试
4. 功能集成测试
5. 链路完整性验证
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.integration_test")

# 测试结果
test_results = {
    'timestamp': datetime.now().isoformat(),
    'tests': [],
    'passed': 0,
    'failed': 0,
    'warnings': 0,
}

def log_test(name: str, passed: bool, message: str = ""):
    """记录测试结果"""
    test_results['tests'].append({
        'name': name,
        'passed': passed,
        'message': message,
    })
    
    if passed:
        test_results['passed'] += 1
        logger.info(f"✅ {name}: {message or '通过'}")
    else:
        test_results['failed'] += 1
        logger.error(f"❌ {name}: {message or '失败'}")


def test_module_imports():
    """测试 1: 模块导入"""
    logger.info("=" * 80)
    logger.info("测试 1: 模块导入测试")
    logger.info("=" * 80)
    
    modules = [
        ('恒指数据', 'app.data.hsi_data', 'hsi_fetcher'),
        ('参数加载器', 'app.risk.regime_params_loader', 'params_loader'),
        ('信号追踪器', 'app.storage.signal_tracker', 'signal_tracker'),
        ('动态阈值', 'app.risk.dynamic_threshold', 'threshold_manager'),
        ('智能路由', 'app.execution.smart_router', 'smart_router'),
        ('订单重试', 'app.execution.order_retry', 'order_retry_manager'),
        ('熔断器', 'app.risk.circuit_breaker', 'circuit_breaker'),
        ('失败追踪', 'app.logs.failure_tracker', 'failure_tracker'),
    ]
    
    for name, module_path, object_name in modules:
        try:
            module = __import__(module_path, fromlist=[object_name])
            obj = getattr(module, object_name)
            log_test(f"模块导入：{name}", True)
        except Exception as e:
            log_test(f"模块导入：{name}", False, str(e))


def test_config_loading():
    """测试 2: 配置加载"""
    logger.info("=" * 80)
    logger.info("测试 2: 配置加载测试")
    logger.info("=" * 80)
    
    # 测试市场状态参数配置
    try:
        import yaml
        with open('/opt/ftseek/FTSeek/config/market_regime_params.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        required_states = ['default', 'strong_trend_up', 'range_bound', 'strong_trend_down']
        missing = [s for s in required_states if s not in config]
        
        if not missing:
            log_test("市场状态参数配置", True, f"加载{len(config)}个状态")
        else:
            log_test("市场状态参数配置", False, f"缺少状态：{missing}")
    except Exception as e:
        log_test("市场状态参数配置", False, str(e))
    
    # 测试告警阈值配置
    try:
        with open('/opt/ftseek/FTSeek/config/alert_thresholds.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        if 'alert_thresholds' in config:
            log_test("告警阈值配置", True, f"加载{len(config['alert_thresholds'])}个阈值")
        else:
            log_test("告警阈值配置", False, "缺少 alert_thresholds 配置")
    except Exception as e:
        log_test("告警阈值配置", False, str(e))
    
    # 测试动态阈值配置
    try:
        if os.path.exists('/opt/ftseek/FTSeek/config/dynamic_threshold.json'):
            with open('/opt/ftseek/FTSeek/config/dynamic_threshold.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            log_test("动态阈值配置", True, f"当前阈值：{config.get('current_threshold', 0)}")
        else:
            log_test("动态阈值配置", True, "文件不存在 (首次运行会创建)")
    except Exception as e:
        log_test("动态阈值配置", False, str(e))


def test_data_flow():
    """测试 3: 数据流向测试"""
    logger.info("=" * 80)
    logger.info("测试 3: 数据流向测试")
    logger.info("=" * 80)
    
    # 测试信号追踪器
    try:
        from app.storage.signal_tracker import signal_tracker
        
        # 模拟记录信号
        signal_id = signal_tracker.record_signal(
            code='HK.09992',
            strategy='test_strategy',
            action='BUY',
            price=95.5,
            quantity=200,
            confidence=0.65,
            consensus_score=2.42,
            strategies=['macd', 'confluence'],
            market_regime='weak_trend_up',
            adx=22.5,
        )
        
        # 记录执行
        signal_tracker.record_execution(
            signal_id,
            executed=True,
            execution_price=95.6,
        )
        
        # 获取统计
        stats = signal_tracker.get_statistics()
        
        if stats and stats.get('total_signals', 0) > 0:
            log_test("信号追踪链路", True, f"记录{stats['total_signals']}个信号")
        else:
            log_test("信号追踪链路", False, "统计异常")
    except Exception as e:
        log_test("信号追踪链路", False, str(e))
    
    # 测试智能路由器
    try:
        from app.execution.smart_router import smart_router
        
        # 测试正常订单
        order1 = smart_router.route_order('HK.09992', 'BUY', 95.5, 200)
        
        # 测试大额订单 (应触发限价单)
        order2 = smart_router.route_order('HK.09992', 'BUY', 550, 200)  # HK$110,000
        
        if order1.order_type.value == 'market' and order2.order_type.value == 'limit':
            log_test("智能订单路由", True, f"正常→市价单，大额→限价单")
        else:
            log_test("智能订单路由", False, f"路由逻辑异常")
    except Exception as e:
        log_test("智能订单路由", False, str(e))
    
    # 测试熔断器
    try:
        from app.risk.circuit_breaker import circuit_breaker
        
        # 检查初始状态
        can_trade_before = circuit_breaker.can_trade()
        
        # 模拟连续拒绝
        for i in range(5):
            circuit_breaker.record_reject(f"test_reject_{i}")
        
        # 检查是否触发熔断
        can_trade_after = circuit_breaker.can_trade()
        
        # 重置状态
        circuit_breaker.state.reset()
        
        if can_trade_before and not can_trade_after:
            log_test("熔断器链路", True, "连续拒绝触发熔断")
        else:
            log_test("熔断器链路", False, "熔断逻辑异常")
    except Exception as e:
        log_test("熔断器链路", False, str(e))
    
    # 测试失败追踪器
    try:
        from app.logs.failure_tracker import failure_tracker
        
        # 记录失败
        failure_tracker.record_failure('HK.09992', 'cooldown', '测试冷却跳过')
        failure_tracker.record_failure('HK.09992', 'confidence', '测试置信度不足')
        
        # 获取统计
        stats = failure_tracker.get_statistics('today')
        
        if stats and stats.get('total', 0) > 0:
            log_test("失败追踪链路", True, f"记录{stats['total']}次失败")
        else:
            log_test("失败追踪链路", False, "统计异常")
    except Exception as e:
        log_test("失败追踪链路", False, str(e))


def test_integration():
    """测试 4: 功能集成测试"""
    logger.info("=" * 80)
    logger.info("测试 4: 功能集成测试")
    logger.info("=" * 80)
    
    # 测试参数加载器
    try:
        from app.risk.regime_params_loader import params_loader
        
        # 获取不同状态的参数
        params_uptrend = params_loader.get_params('strong_trend_up')
        params_downtrend = params_loader.get_params('strong_trend_down')
        
        if (params_uptrend['signal_cooldown'] == 60 and 
            params_downtrend['signal_cooldown'] == 300):
            log_test("动态参数加载", True, "状态参数正确")
        else:
            log_test("动态参数加载", False, "参数值异常")
    except Exception as e:
        log_test("动态参数加载", False, str(e))
    
    # 测试动态阈值
    try:
        from app.risk.dynamic_threshold import threshold_manager
        
        current = threshold_manager.get_current_threshold()
        
        if 0.40 <= current <= 0.80:
            log_test("动态阈值管理", True, f"当前阈值：{current:.2f}")
        else:
            log_test("动态阈值管理", False, f"阈值异常：{current}")
    except Exception as e:
        log_test("动态阈值管理", False, str(e))
    
    # 测试数据目录
    try:
        data_dirs = [
            '/opt/ftseek/FTSeek/data/signal_records',
            '/opt/ftseek/FTSeek/data/failure_stats',
            '/opt/ftseek/FTSeek/data/backtest_output',
            '/opt/ftseek/FTSeek/data/analysis_output',
        ]
        
        missing = [d for d in data_dirs if not os.path.exists(d)]
        
        if not missing:
            log_test("数据目录结构", True, "所有目录存在")
        else:
            log_test("数据目录结构", False, f"缺少目录：{missing}")
    except Exception as e:
        log_test("数据目录结构", False, str(e))


def test_service_health():
    """测试 5: 服务健康检查"""
    logger.info("=" * 80)
    logger.info("测试 5: 服务健康检查")
    logger.info("=" * 80)
    
    # 检查服务进程
    try:
        import subprocess
        # Python 3.8 兼容：使用 stdout/stderr PIPE 替代 capture_output
        result = subprocess.run(['pgrep', '-f', 'app.main'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            log_test("FTSeek 服务进程", True, f"PID: {', '.join(pids)}")
        else:
            log_test("FTSeek 服务进程", False, "服务未运行")
    except Exception as e:
        log_test("FTSeek 服务进程", False, str(e))
    
    # 检查健康端点
    try:
        import urllib.request
        response = urllib.request.urlopen('http://localhost:8888/health', timeout=5)
        data = json.loads(response.read().decode())
        
        if data.get('status') == 'ok':
            log_test("健康检查端点", True, "/health → 200 OK")
        else:
            log_test("健康检查端点", False, f"状态异常：{data}")
    except Exception as e:
        log_test("健康检查端点", False, str(e))
    
    # 检查日志文件
    try:
        log_file = '/opt/ftseek/FTSeek/logs/ftseek.log'
        if os.path.exists(log_file):
            file_size = os.path.getsize(log_file)
            log_test("日志文件", True, f"大小：{file_size/1024:.1f}KB")
        else:
            log_test("日志文件", False, "文件不存在")
    except Exception as e:
        log_test("日志文件", False, str(e))


def generate_report():
    """生成测试报告"""
    logger.info("\n" + "=" * 80)
    logger.info("📊 FTSeek 全链路集成测试报告")
    logger.info("=" * 80)
    logger.info(f"测试时间：{test_results['timestamp']}")
    logger.info(f"总测试数：{len(test_results['tests'])}")
    logger.info(f"✅ 通过：{test_results['passed']}")
    logger.info(f"❌ 失败：{test_results['failed']}")
    logger.info("=" * 80)
    
    # 计算通过率
    total = test_results['passed'] + test_results['failed']
    pass_rate = test_results['passed'] / total * 100 if total > 0 else 0
    
    if pass_rate >= 95:
        logger.info("🎉 整体状态：优秀")
    elif pass_rate >= 80:
        logger.info("✅ 整体状态：良好")
    elif pass_rate >= 60:
        logger.info("⚠️  整体状态：需改进")
    else:
        logger.info("❌ 整体状态：严重问题")
    
    logger.info("=" * 80)
    
    # 保存报告
    report_file = '/opt/ftseek/FTSeek/data/integration_test_report.json'
    Path(report_file).parent.mkdir(parents=True, exist_ok=True)
    
    test_results['pass_rate'] = round(pass_rate, 2)
    test_results['overall_status'] = '优秀' if pass_rate >= 95 else '良好' if pass_rate >= 80 else '需改进' if pass_rate >= 60 else '严重问题'
    
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(test_results, f, indent=2, ensure_ascii=False)
    
    logger.info(f"\n📄 报告已保存：{report_file}")
    
    return pass_rate


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("🔍 FTSeek 全链路集成测试开始")
    logger.info("=" * 80)
    
    try:
        # 执行测试
        test_module_imports()
        test_config_loading()
        test_data_flow()
        test_integration()
        test_service_health()
        
        # 生成报告
        pass_rate = generate_report()
        
        logger.info("=" * 80)
        logger.info("✅ 全链路集成测试完成")
        logger.info("=" * 80)
        
        return 0 if pass_rate >= 80 else 1
        
    except Exception as e:
        logger.error(f"❌ 测试异常：{e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
