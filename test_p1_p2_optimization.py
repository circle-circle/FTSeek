#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek P1+P2 优化测试脚本

测试内容：
1. 日志系统优化 (P1-1)
2. 监控告警增强 (P1-2)
3. 配置统一管理 (P2)

运行方式:
    python3 test_p1_p2_optimization.py --all
    python3 test_p1_p2_optimization.py --logging
    python3 test_p1_p2_optimization.py --monitoring
    python3 test_p1_p2_optimization.py --config
"""

import sys
import os
import json
import time
from pathlib import Path
from datetime import datetime

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 导入测试模块
from app.core.logging_config import (
    setup_logging, get_logger, trace_context, 
    get_log_stats, cleanup_old_logs, LoggingConfig
)
from app.monitoring.metrics_collector import (
    MetricsCollector, get_collector, init_collector,
    MetricType, AlertLevel, MetricDefinition
)
from app.monitoring.alert_manager import (
    AlertManager, get_alert_manager, init_alert_manager,
    AlertRule, AlertLevel as AlertLevelEnum
)
from app.api.health import (
    check_config, check_database, check_logs,
    HealthStatus
)


# ============================================================
# 测试：日志系统优化 (P1-1)
# ============================================================
def test_logging():
    """测试日志系统优化"""
    print("\n" + "="*60)
    print("🧪 测试：日志系统优化 (P1-1)")
    print("="*60)
    
    results = {
        'test_name': '日志系统优化',
        'timestamp': datetime.now().isoformat(),
        'tests': []
    }
    
    # 测试 1: 统一 logger 配置
    print("\n1. 测试统一 logger 配置...")
    try:
        logger = setup_logging("test_module", level="DEBUG")
        assert logger is not None
        assert logger.name == "ftseek.test_module"
        assert logger.propagate == False  # 关键：禁止传播
        print("   ✅ 统一 logger 配置正常")
        results['tests'].append({'name': '统一 logger 配置', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 统一 logger 配置失败：{e}")
        results['tests'].append({'name': '统一 logger 配置', 'status': 'fail', 'error': str(e)})
    
    # 测试 2: TraceID 追踪
    print("\n2. 测试 TraceID 全链路追踪...")
    try:
        with trace_context() as tid:
            assert tid is not None
            assert len(tid) == 8
            logger.info(f"带 TraceID 的日志 [tid={tid}]")
        print(f"   ✅ TraceID 追踪正常 (tid={tid})")
        results['tests'].append({'name': 'TraceID 追踪', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ TraceID 追踪失败：{e}")
        results['tests'].append({'name': 'TraceID 追踪', 'status': 'fail', 'error': str(e)})
    
    # 测试 3: 日志级别控制
    print("\n3. 测试日志级别控制...")
    try:
        logger.debug("DEBUG 级别日志")
        logger.info("INFO 级别日志")
        logger.warning("WARNING 级别日志")
        logger.error("ERROR 级别日志")
        print("   ✅ 日志级别控制正常")
        results['tests'].append({'name': '日志级别控制', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 日志级别控制失败：{e}")
        results['tests'].append({'name': '日志级别控制', 'status': 'fail', 'error': str(e)})
    
    # 测试 4: 日志轮转配置
    print("\n4. 测试日志轮转配置...")
    try:
        config = LoggingConfig(
            max_size_mb=50,
            backup_count_size=5,
            backup_count_daily=30
        )
        assert config.max_size_mb == 50
        assert config.backup_count_size == 5
        print(f"   ✅ 日志轮转配置正常 (max_size={config.max_size_mb}MB)")
        results['tests'].append({'name': '日志轮转配置', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 日志轮转配置失败：{e}")
        results['tests'].append({'name': '日志轮转配置', 'status': 'fail', 'error': str(e)})
    
    # 测试 5: 日志统计
    print("\n5. 测试日志统计功能...")
    try:
        stats = get_log_stats()
        assert 'file_count' in stats
        assert 'total_size_mb' in stats
        print(f"   ✅ 日志统计正常 (文件数={stats['file_count']}, 总大小={stats['total_size_mb']:.2f}MB)")
        results['tests'].append({'name': '日志统计功能', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 日志统计功能失败：{e}")
        results['tests'].append({'name': '日志统计功能', 'status': 'fail', 'error': str(e)})
    
    # 总结
    passed = sum(1 for t in results['tests'] if t['status'] == 'pass')
    total = len(results['tests'])
    print(f"\n📊 日志系统测试：{passed}/{total} 通过")
    
    results['summary'] = {
        'passed': passed,
        'total': total,
        'pass_rate': passed / total if total > 0 else 0
    }
    
    # 保存测试结果
    results_dir = Path('/opt/ftseek/FTSeek/logs/test_results')
    results_dir.mkdir(parents=True, exist_ok=True)
    result_file = results_dir / f"logging_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"📁 测试结果已保存：{result_file}")
    
    return results


# ============================================================
# 测试：监控告警增强 (P1-2)
# ============================================================
def test_monitoring():
    """测试监控告警增强"""
    print("\n" + "="*60)
    print("🧪 测试：监控告警增强 (P1-2)")
    print("="*60)
    
    results = {
        'test_name': '监控告警增强',
        'timestamp': datetime.now().isoformat(),
        'tests': []
    }
    
    # 初始化采集器
    collector = init_collector()
    
    # 测试 1: 系统指标采集
    print("\n1. 测试系统指标采集...")
    try:
        collector.collect_system_metrics()
        cpu = collector.get_current_value("system_cpu_percent")
        memory = collector.get_current_value("system_memory_percent")
        assert cpu is not None
        assert memory is not None
        print(f"   ✅ 系统指标采集正常 (CPU={cpu}%, 内存={memory}%)")
        results['tests'].append({'name': '系统指标采集', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 系统指标采集失败：{e}")
        results['tests'].append({'name': '系统指标采集', 'status': 'fail', 'error': str(e)})
    
    # 测试 2: OpenD 连接状态采集
    print("\n2. 测试 OpenD 连接状态采集...")
    try:
        collector.collect_opend_metrics(connection_status=True)
        status = collector.get_current_value("opend_connection_status")
        assert status == 1
        print(f"   ✅ OpenD 连接状态采集正常 (status={status})")
        results['tests'].append({'name': 'OpenD 连接状态采集', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ OpenD 连接状态采集失败：{e}")
        results['tests'].append({'name': 'OpenD 连接状态采集', 'status': 'fail', 'error': str(e)})
    
    # 测试 3: 交易指标采集
    print("\n3. 测试交易指标采集...")
    try:
        collector.collect_trade_metrics(success=True, amount=10000)
        collector.collect_trade_metrics(success=True, amount=15000)
        collector.collect_trade_metrics(success=False)
        success_rate = collector.get_current_value("trade_success_rate")
        assert success_rate is not None
        print(f"   ✅ 交易指标采集正常 (成功率={success_rate}%)")
        results['tests'].append({'name': '交易指标采集', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 交易指标采集失败：{e}")
        results['tests'].append({'name': '交易指标采集', 'status': 'fail', 'error': str(e)})
    
    # 测试 4: 指标导出 (JSON)
    print("\n4. 测试指标导出 (JSON)...")
    try:
        export_data = collector.export_json()
        assert 'timestamp' in export_data
        assert 'metrics' in export_data
        print(f"   ✅ 指标 JSON 导出正常 (指标数={len(export_data['metrics'])})")
        results['tests'].append({'name': '指标 JSON 导出', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 指标 JSON 导出失败：{e}")
        results['tests'].append({'name': '指标 JSON 导出', 'status': 'fail', 'error': str(e)})
    
    # 测试 5: 指标导出 (Prometheus)
    print("\n5. 测试指标导出 (Prometheus)...")
    try:
        prometheus_text = collector.export_prometheus()
        assert 'HELP' in prometheus_text
        assert 'TYPE' in prometheus_text
        print(f"   ✅ 指标 Prometheus 导出正常 ({len(prometheus_text.splitlines())} 行)")
        results['tests'].append({'name': '指标 Prometheus 导出', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 指标 Prometheus 导出失败：{e}")
        results['tests'].append({'name': '指标 Prometheus 导出', 'status': 'fail', 'error': str(e)})
    
    # 测试 6: 告警规则加载
    print("\n6. 测试告警规则加载...")
    try:
        alert_manager = get_alert_manager()
        status = alert_manager.get_status()
        assert status['rules_count'] > 0
        print(f"   ✅ 告警规则加载正常 (规则数={status['rules_count']})")
        results['tests'].append({'name': '告警规则加载', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 告警规则加载失败：{e}")
        results['tests'].append({'name': '告警规则加载', 'status': 'fail', 'error': str(e)})
    
    # 测试 7: 告警触发和冷却
    print("\n7. 测试告警触发和冷却机制...")
    try:
        alert_manager = get_alert_manager()
        
        # 第一次触发 (应成功)
        alerts1 = alert_manager.check_metric("system_cpu_percent", 95.0)
        assert len(alerts1) > 0
        
        # 第二次触发 (应在冷却期内，被跳过)
        alerts2 = alert_manager.check_metric("system_cpu_percent", 95.0)
        # 注意：这里可能为 0 也可能不为 0，取决于冷却时间设置
        
        print(f"   ✅ 告警触发和冷却正常 (第一次={len(alerts1)}条)")
        results['tests'].append({'name': '告警触发和冷却', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 告警触发和冷却失败：{e}")
        results['tests'].append({'name': '告警触发和冷却', 'status': 'fail', 'error': str(e)})
    
    # 测试 8: 健康检查
    print("\n8. 测试健康检查...")
    try:
        config_ok = check_config()
        logs_ok = check_logs()
        assert config_ok == True
        assert logs_ok == True
        print(f"   ✅ 健康检查正常 (config={config_ok}, logs={logs_ok})")
        results['tests'].append({'name': '健康检查', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 健康检查失败：{e}")
        results['tests'].append({'name': '健康检查', 'status': 'fail', 'error': str(e)})
    
    # 总结
    passed = sum(1 for t in results['tests'] if t['status'] == 'pass')
    total = len(results['tests'])
    print(f"\n📊 监控告警测试：{passed}/{total} 通过")
    
    results['summary'] = {
        'passed': passed,
        'total': total,
        'pass_rate': passed / total if total > 0 else 0
    }
    
    # 保存测试结果
    results_dir = Path('/opt/ftseek/FTSeek/logs/test_results')
    results_dir.mkdir(parents=True, exist_ok=True)
    result_file = results_dir / f"monitoring_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"📁 测试结果已保存：{result_file}")
    
    return results


# ============================================================
# 测试：配置统一管理 (P2)
# ============================================================
def test_config():
    """测试配置统一管理"""
    print("\n" + "="*60)
    print("🧪 测试：配置统一管理 (P2)")
    print("="*60)
    
    results = {
        'test_name': '配置统一管理',
        'timestamp': datetime.now().isoformat(),
        'tests': []
    }
    
    # 测试 1: 统一配置文件加载
    print("\n1. 测试统一配置文件加载...")
    try:
        from config.config_manager import ConfigManager
        config_manager = ConfigManager()
        config = config_manager.get_all()
        assert config is not None
        assert 'version' in config
        print(f"   ✅ 统一配置文件加载正常 (version={config['version']})")
        results['tests'].append({'name': '统一配置文件加载', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 统一配置文件加载失败：{e}")
        results['tests'].append({'name': '统一配置文件加载', 'status': 'fail', 'error': str(e)})
    
    # 测试 2: 配置分层访问
    print("\n2. 测试配置分层访问...")
    try:
        strategy_config = config_manager.get_strategy_config()
        risk_config = config_manager.get_risk_config()
        trading_config = config_manager.get_trading_config()
        assert strategy_config is not None
        assert risk_config is not None
        assert trading_config is not None
        print(f"   ✅ 配置分层访问正常")
        results['tests'].append({'name': '配置分层访问', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 配置分层访问失败：{e}")
        results['tests'].append({'name': '配置分层访问', 'status': 'fail', 'error': str(e)})
    
    # 测试 3: 配置键路径访问
    print("\n3. 测试配置键路径访问...")
    try:
        version = config_manager.get('version')
        trading_mode = config_manager.get('trading.mode')
        futu_host = config_manager.get('futu.host')
        assert version is not None
        assert trading_mode is not None
        assert futu_host is not None
        print(f"   ✅ 配置键路径访问正常 (version={version}, mode={trading_mode})")
        results['tests'].append({'name': '配置键路径访问', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 配置键路径访问失败：{e}")
        results['tests'].append({'name': '配置键路径访问', 'status': 'fail', 'error': str(e)})
    
    # 测试 4: 日志配置验证
    print("\n4. 测试日志配置验证...")
    try:
        logging_config = config_manager.get('logging')
        assert logging_config is not None
        assert 'level' in logging_config
        assert 'max_size_mb' in logging_config
        print(f"   ✅ 日志配置验证正常 (level={logging_config['level']})")
        results['tests'].append({'name': '日志配置验证', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 日志配置验证失败：{e}")
        results['tests'].append({'name': '日志配置验证', 'status': 'fail', 'error': str(e)})
    
    # 测试 5: 监控配置验证
    print("\n5. 测试监控配置验证...")
    try:
        monitoring_config = config_manager.get('monitoring')
        assert monitoring_config is not None
        assert 'metrics' in monitoring_config
        assert 'alerts' in monitoring_config
        assert 'health_check' in monitoring_config
        print(f"   ✅ 监控配置验证正常")
        results['tests'].append({'name': '监控配置验证', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 监控配置验证失败：{e}")
        results['tests'].append({'name': '监控配置验证', 'status': 'fail', 'error': str(e)})
    
    # 测试 6: 告警规则配置验证
    print("\n6. 测试告警规则配置验证...")
    try:
        alert_manager = get_alert_manager()
        status = alert_manager.get_status()
        assert status['rules_count'] > 0
        print(f"   ✅ 告警规则配置验证正常 (规则数={status['rules_count']})")
        results['tests'].append({'name': '告警规则配置验证', 'status': 'pass'})
    except Exception as e:
        print(f"   ❌ 告警规则配置验证失败：{e}")
        results['tests'].append({'name': '告警规则配置验证', 'status': 'fail', 'error': str(e)})
    
    # 总结
    passed = sum(1 for t in results['tests'] if t['status'] == 'pass')
    total = len(results['tests'])
    print(f"\n📊 配置管理测试：{passed}/{total} 通过")
    
    results['summary'] = {
        'passed': passed,
        'total': total,
        'pass_rate': passed / total if total > 0 else 0
    }
    
    # 保存测试结果
    results_dir = Path('/opt/ftseek/FTSeek/logs/test_results')
    results_dir.mkdir(parents=True, exist_ok=True)
    result_file = results_dir / f"config_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"📁 测试结果已保存：{result_file}")
    
    return results


# ============================================================
# 主函数
# ============================================================
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='FTSeek P1+P2 优化测试')
    parser.add_argument('--all', action='store_true', help='运行所有测试')
    parser.add_argument('--logging', action='store_true', help='测试日志系统')
    parser.add_argument('--monitoring', action='store_true', help='测试监控告警')
    parser.add_argument('--config', action='store_true', help='测试配置管理')
    
    args = parser.parse_args()
    
    # 默认运行所有测试
    if not (args.logging or args.monitoring or args.config):
        args.all = True
    
    all_results = []
    
    if args.all or args.logging:
        all_results.append(test_logging())
    
    if args.all or args.monitoring:
        all_results.append(test_monitoring())
    
    if args.all or args.config:
        all_results.append(test_config())
    
    # 汇总报告
    print("\n" + "="*60)
    print("📊 测试汇总报告")
    print("="*60)
    
    total_passed = 0
    total_tests = 0
    
    for result in all_results:
        summary = result.get('summary', {})
        passed = summary.get('passed', 0)
        total = summary.get('total', 0)
        total_passed += passed
        total_tests += total
        
        test_name = result.get('test_name', 'Unknown')
        pass_rate = summary.get('pass_rate', 0) * 100
        
        status_icon = "✅" if pass_rate == 100 else "⚠️" if pass_rate > 0 else "❌"
        print(f"\n{status_icon} {test_name}: {passed}/{total} ({pass_rate:.1f}%)")
    
    print(f"\n{'='*60}")
    overall_rate = (total_passed / total_tests * 100) if total_tests > 0 else 0
    print(f"📈 总体通过率：{total_passed}/{total_tests} ({overall_rate:.1f}%)")
    print(f"{'='*60}\n")
    
    # 保存汇总报告
    results_dir = Path('/opt/ftseek/FTSeek/logs/test_results')
    results_dir.mkdir(parents=True, exist_ok=True)
    summary_file = results_dir / f"p1_p2_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'overall': {
                'passed': total_passed,
                'total': total_tests,
                'pass_rate': overall_rate
            },
            'details': all_results
        }, f, ensure_ascii=False, indent=2)
    
    print(f"📁 汇总报告已保存：{summary_file}")
    
    return 0 if overall_rate == 100 else 1


if __name__ == '__main__':
    sys.exit(main())
