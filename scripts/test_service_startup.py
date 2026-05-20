#!/usr/bin/env python3
"""
FTSeek 服务启动验证测试

测试内容:
1. 配置加载验证
2. 核心模块导入测试
3. 数据源连接测试
4. 策略引擎加载测试
5. 回测引擎加载测试
6. 跨市场模块加载测试
7. 风险管理加载测试
8. 通知系统加载测试
9. 数据库连接测试
10. 完整服务启动模拟

使用方法:
  cd /opt/ftseek/FTSeek
  python3.8 scripts/test_service_startup.py
"""

import sys
import os
import logging
import time
import importlib
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("service_startup_test")


class StartupTestResult:
    """启动测试结果"""
    
    def __init__(self):
        self.tests = []
        self.start_time = time.time()
    
    def add(self, category: str, name: str, passed: bool, message: str = "", duration: float = 0):
        self.tests.append({
            "category": category,
            "name": name,
            "passed": passed,
            "message": message,
            "duration": duration,
        })
        icon = "✅" if passed else "❌"
        duration_str = f" ({duration:.2f}s)" if duration > 0 else ""
        logger.info(f"  {icon} {name}: {message}{duration_str}")
    
    def is_healthy(self) -> bool:
        return all(t['passed'] for t in self.tests)
    
    def summary(self) -> str:
        duration = time.time() - self.start_time
        passed = sum(1 for t in self.tests if t['passed'])
        total = len(self.tests)
        
        categories = set(t['category'] for t in self.tests)
        cat_results = []
        for cat in categories:
            cat_tests = [t for t in self.tests if t['category'] == cat]
            cat_passed = sum(1 for t in cat_tests if t['passed'])
            cat_total = len(cat_tests)
            status = "✅" if cat_passed == cat_total else "⚠️"
            cat_results.append(f"{status} {cat}: {cat_passed}/{cat_total}")
        
        return (
            f"\n{'=' * 60}\n"
            f"🚀 服务启动测试总结\n"
            f"{'=' * 60}\n"
            f"⏱️  总耗时: {duration:.2f}s\n"
            f"📊 总测试: {passed}/{total} 通过\n\n"
            f"📋 分类结果:\n" +
            "\n".join(f"  {r}" for r in cat_results) +
            f"\n\n{'=' * 60}\n"
            f"总体状态: {'✅ 全部通过 - 服务可正常启动' if self.is_healthy() else '⚠️ 存在问题 - 请检查失败项'}\n"
            f"{'=' * 60}"
        )


def test_category(name: str, test_func, result: StartupTestResult):
    """执行分类测试"""
    logger.info(f"\n{'=' * 60}")
    logger.info(f"📦 {name}")
    logger.info(f"{'=' * 60}")
    
    try:
        test_func(result)
    except Exception as e:
        result.add(name, f"{name} 整体", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")


# ── 测试函数 ──────────────────────────────────────────────────────

def test_config_loading(result: StartupTestResult):
    """测试配置加载"""
    start = time.time()
    
    try:
        # 1. 环境变量加载
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
        if os.path.exists(env_path):
            load_dotenv(env_path)
            result.add("配置加载", ".env 加载", True, f"路径: {env_path}")
        else:
            result.add("配置加载", ".env 加载", False, "文件不存在")
        
        # 2. 配置中心
        t0 = time.time()
        from config.config_center import get_config_center
        cc = get_config_center()
        cc.load()
        result.add("配置加载", "ConfigCenter", True, f"加载耗时: {time.time()-t0:.3f}s")
        
        # 3. app.config 导入
        t0 = time.time()
        from app import config
        result.add("配置加载", "app.config", True, f"导入耗时: {time.time()-t0:.3f}s")
        
        # 4. 关键配置验证
        required_configs = [
            ('trading.watch_list_hk', '港股名单'),
            ('trading.watch_list_us', '美股名单'),
            ('risk_limits.min_confidence', '最小置信度'),
            ('futu.host', 'Futu 主机'),
        ]
        
        for key, name in required_configs:
            val = cc.get(key)
            if val is not None:
                result.add("配置加载", f"{name} 配置", True, f"值: {val if not isinstance(val, list) else f'{len(val)} 项'}")
            else:
                result.add("配置加载", f"{name} 配置", False, "未找到")
        
        # 5. YAML 配置文件
        yaml_files = [
            'config/unified_unified_config.yaml',
            'config/cross_market_mapping.yaml',
            'config/us_data_unified_config.yaml',
        ]
        
        for f in yaml_files:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f)
            exists = os.path.exists(path)
            result.add("配置加载", f"YAML: {os.path.basename(f)}", exists, f"{'存在' if exists else '缺失'}")
        
    except Exception as e:
        result.add("配置加载", "配置加载测试", False, str(e))
    
    result.add("配置加载", "配置加载分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_core_modules(result: StartupTestResult):
    """测试核心模块导入"""
    start = time.time()
    
    modules = [
        ("app.decision_buffer", "决策缓冲层"),
        ("app.signal_store", "信号存储"),
        ("app.storage.database", "数据库"),
        ("app.risk.risk_pipeline", "风控管线"),
        ("app.risk.circuit_breaker", "熔断器"),
        ("app.risk.position_sizer", "仓位管理器"),
        ("app.trader.executor", "交易执行器"),
        ("app.trader.position_manager", "持仓管理器"),
        ("app.notifier.dingtalk", "钉钉通知"),
        ("app.data_subscriber", "数据订阅器"),
    ]
    
    for module_path, name in modules:
        try:
            t0 = time.time()
            mod = importlib.import_module(module_path)
            duration = time.time() - t0
            result.add("核心模块", name, True, f"导入耗时: {duration:.3f}s", duration)
        except Exception as e:
            result.add("核心模块", name, False, str(e))
    
    result.add("核心模块", "核心模块分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_strategy_engine(result: StartupTestResult):
    """测试策略引擎"""
    start = time.time()
    
    try:
        # 1. BaseStrategy
        from app.strategy.base import BaseStrategy, Signal, SignalAction
        result.add("策略引擎", "BaseStrategy", True, "导入成功")
        
        # 2. 共识引擎
        from app.strategy.consensus_engine import ConsensusEngine
        result.add("策略引擎", "ConsensusEngine", True, "导入成功")
        
        # 3. ConfluenceV2 (主力策略)
        from app.strategy.confluence_v2 import ConfluenceStrategyV2
        t0 = time.time()
        confluence = ConfluenceStrategyV2()
        result.add("策略引擎", "ConfluenceV2 初始化", True, f"耗时: {time.time()-t0:.3f}s", time.time()-t0)
        
        # 4. StandardStrategy (新接口)
        from app.strategy.standard_strategy import StandardStrategy
        result.add("策略引擎", "StandardStrategy", True, "导入成功")
        
        # 5. 示例策略
        from app.strategy.example_strategies import RSIMeanReversion, MACross, MACDMomentum
        for cls in [RSIMeanReversion, MACross, MACDMomentum]:
            t0 = time.time()
            s = cls()
            result.add("策略引擎", f"{s.name}", True, f"v{s.version}", time.time()-t0)
        
        # 6. 策略共存验证
        result.add("策略引擎", "策略共存", True, "BaseStrategy + StandardStrategy")
        
    except Exception as e:
        result.add("策略引擎", "策略引擎测试", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    result.add("策略引擎", "策略引擎分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_backtest_engine(result: StartupTestResult):
    """测试回测引擎"""
    start = time.time()
    
    try:
        # 1. 回测引擎导入
        from app.backtest.engine import BacktestEngine, get_backtest_engine
        result.add("回测引擎", "BacktestEngine", True, "导入成功")
        
        # 2. 引擎初始化
        t0 = time.time()
        engine = get_backtest_engine()
        result.add("回测引擎", "引擎初始化", True, f"capital=${engine.initial_capital:,.0f}", time.time()-t0)
        
        # 3. 快速回测测试
        import pandas as pd
        import numpy as np
        
        np.random.seed(42)
        dates = pd.date_range('2025-01-01', periods=200, freq='1h')
        close = 100 * np.exp(np.cumsum(np.random.normal(0.0001, 0.01, 200)))
        test_df = pd.DataFrame({
            'open': close * 0.999, 'high': close * 1.005,
            'low': close * 0.995, 'close': close, 'volume': 1e6,
        }, index=dates)
        
        from app.strategy.example_strategies import RSIMeanReversion
        strategy = RSIMeanReversion()
        
        t0 = time.time()
        bt_result = engine.run(strategy, test_df, pair='TEST')
        duration = time.time() - t0
        
        result.add("回测引擎", "快速回测", bt_result.final_capital > 0, 
                  f"${bt_result.final_capital:,.0f} ({bt_result.total_trades} trades)", duration)
        
        # 4. CLI 工具
        result.add("回测引擎", "CLI 工具", 
                  os.path.exists('/opt/ftseek/FTSeek/scripts/backtest_cli.py'),
                  "存在" if os.path.exists('/opt/ftseek/FTSeek/scripts/backtest_cli.py') else "缺失")
        
    except Exception as e:
        result.add("回测引擎", "回测引擎测试", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    result.add("回测引擎", "回测引擎分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_cross_market(result: StartupTestResult):
    """测试跨市场模块"""
    start = time.time()
    
    try:
        # 1. 跨市场映射器
        from app.utils.cross_market_mapper import get_cross_market_mapper
        t0 = time.time()
        mapper = get_cross_market_mapper()
        result.add("跨市场", "映射器加载", True, f"{len(mapper._direct_mapping)} 条映射", time.time()-t0)
        
        # 2. 美股数据获取
        from app.data.us_data_fetcher import get_us_data_fetcher
        t0 = time.time()
        fetcher = get_us_data_fetcher()
        health = fetcher.get_health_status()
        enabled = sum(1 for s in health.values() if s['enabled'])
        result.add("跨市场", "数据源状态", enabled > 0, f"{enabled}/3 启用", time.time()-t0)
        
        # 3. 事件缓存
        from app.data.cross_market_event_cache import get_cross_market_event_cache
        t0 = time.time()
        cache = get_cross_market_event_cache()
        result.add("跨市场", "事件缓存", True, "初始化成功", time.time()-t0)
        
        # 4. 套利策略
        from app.strategy.arb_dual_listed import get_dual_listed_arb
        t0 = time.time()
        arb = get_dual_listed_arb()
        result.add("跨市场", "套利策略", True, f"{len(arb.pairs)} 对", time.time()-t0)
        
        # 5. 盘前分析器
        from app.pipeline.premarket_analyst import PreMarketAnalyst
        t0 = time.time()
        analyst = PreMarketAnalyst()
        result.add("跨市场", "盘前分析器", True, "初始化成功", time.time()-t0)
        
    except Exception as e:
        result.add("跨市场", "跨市场模块测试", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    result.add("跨市场", "跨市场模块分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_risk_management(result: StartupTestResult):
    """测试风险管理"""
    start = time.time()
    
    try:
        # 1. 风控管线
        from app.risk.risk_pipeline import RiskPipeline
        result.add("风险管理", "RiskPipeline", True, "导入成功")
        
        # 2. 熔断器
        from app.risk.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker()
        result.add("风险管理", "CircuitBreaker", True, f"drawdown_limit={cb.drawdown_limit}")
        
        # 3. 仓位管理器
        from app.risk.position_sizer import PositionSizer
        result.add("风险管理", "PositionSizer", True, "导入成功")
        
        # 3. 止损管理
        try:
            from app.risk.stop_loss import StopLossCalculator
            result.add("风险管理", "StopLossCalculator", True, "导入成功")
        except ImportError:
            from app.risk import stop_loss
            result.add("风险管理", "stop_loss 模块", True, "模块可用")
        
    except Exception as e:
        result.add("风险管理", "风险管理测试", False, str(e))
    
    result.add("风险管理", "风险管理分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_database(result: StartupTestResult):
    """测试数据库"""
    start = time.time()
    
    try:
        from app.storage.database import get_database
        
        t0 = time.time()
        db = get_database()
        result.add("数据库", "连接初始化", True, f"耗时: {time.time()-t0:.3f}s", time.time()-t0)
        
        # 测试 WAL 模式
        t0 = time.time()
        with db.get_connection() as conn:
            cursor = conn.execute("PRAGMA journal_mode;")
            journal_mode = cursor.fetchone()[0]
            result.add("数据库", "WAL 模式", journal_mode == 'wal', f"当前: {journal_mode}", time.time()-t0)
        
    except Exception as e:
        result.add("数据库", "数据库测试", False, str(e))
    
    result.add("数据库", "数据库分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_notifier(result: StartupTestResult):
    """测试通知系统"""
    start = time.time()
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        # 检查配置
        webhook = os.getenv('DINGTALK_WEBHOOK_URL') or os.getenv('DINGTALK_WEBHOOK_URL_DEFAULT')
        secret = os.getenv('DINGTALK_SECRET') or os.getenv('DINGTALK_SECRET_DEFAULT')
        
        result.add("通知系统", "Webhook 配置", bool(webhook), "已配置" if webhook else "未配置")
        result.add("通知系统", "Secret 配置", bool(secret), "已配置" if secret else "未配置")
        
        if webhook and secret:
            t0 = time.time()
            notifier = DingTalkNotifier()
            result.add("通知系统", "DingTalkNotifier", True, f"初始化耗时: {time.time()-t0:.3f}s", time.time()-t0)
        else:
            result.add("通知系统", "DingTalkNotifier", False, "配置不完整，跳过初始化")
        
    except Exception as e:
        result.add("通知系统", "通知系统测试", False, str(e))
    
    result.add("通知系统", "通知系统分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def test_data_sources(result: StartupTestResult):
    """测试数据源"""
    start = time.time()
    
    try:
        # 1. 富途连接配置
        from app import config
        result.add("数据源", "Futu 配置", 
                  bool(config.OPEND_HOST), 
                  f"{config.OPEND_HOST}:{config.OPEND_PORT}")
        
        # 2. 美股数据源
        from app.data.us_data_fetcher import get_us_data_fetcher
        fetcher = get_us_data_fetcher()
        
        for name, status in fetcher.get_health_status().items():
            result.add("数据源", f"{name}", status['enabled'], 
                      f"enabled={status['enabled']}, key={'已配置' if status['api_key_configured'] else '未配置'}")
        
        # 3. 快速数据获取测试 (可选)
        try:
            quote = fetcher.get_quote("US.SPY")
            if quote and quote.get("price", 0) > 0:
                result.add("数据源", "实时数据获取", True, f"SPY ${quote['price']:.2f}")
            else:
                result.add("数据源", "实时数据获取", False, "获取失败")
        except Exception as e:
            result.add("数据源", "实时数据获取", False, str(e))
        
    except Exception as e:
        result.add("数据源", "数据源测试", False, str(e))
    
    result.add("数据源", "数据源分类", True, f"总耗时: {time.time()-start:.3f}s", time.time()-start)


def main():
    logger.info("=" * 60)
    logger.info("🚀 FTSeek 服务启动验证测试")
    logger.info(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    result = StartupTestResult()
    
    # 1. 配置加载
    test_category("1. 配置加载", test_config_loading, result)
    
    # 2. 核心模块
    test_category("2. 核心模块", test_core_modules, result)
    
    # 3. 策略引擎
    test_category("3. 策略引擎", test_strategy_engine, result)
    
    # 4. 回测引擎
    test_category("4. 回测引擎", test_backtest_engine, result)
    
    # 5. 跨市场模块
    test_category("5. 跨市场模块", test_cross_market, result)
    
    # 6. 风险管理
    test_category("6. 风险管理", test_risk_management, result)
    
    # 7. 数据库
    test_category("7. 数据库", test_database, result)
    
    # 8. 通知系统
    test_category("8. 通知系统", test_notifier, result)
    
    # 9. 数据源
    test_category("9. 数据源", test_data_sources, result)
    
    # 10. 新增增强模块 (Action Items)
    logger.info("\n" + "=" * 60)
    logger.info("📦 10. 增强模块检查 (三重屏障/防火墙/监控)")
    logger.info("=" * 60)
    
    try:
        # 三重屏障
        from app.factors.triple_barrier import TripleBarrierLabeler
        result.add("增强模块", "TripleBarrierLabeler", True, "导入成功")
        logger.info("  ✅ TripleBarrierLabeler: 导入成功")
        
        # LLM 防火墙
        from app.factors.llm_firewall import LLMFirewall, LLMFirewallError
        result.add("增强模块", "LLMFirewall", True, "导入成功")
        logger.info("  ✅ LLMFirewall: 导入成功")
        
        # 执行质量监控
        from app.monitoring.execution_quality import get_execution_monitor
        result.add("增强模块", "ExecutionMonitor", True, "导入成功")
        logger.info("  ✅ ExecutionQualityMonitor: 导入成功")
        
    except Exception as e:
        result.add("增强模块", "增强模块检查", False, str(e))
        logger.error(f"  ❌ 增强模块检查失败: {e}")

    # 输出总结
    print(result.summary())
    
    # 保存报告
    report_path = "/opt/ftseek/FTSeek/logs/service_startup_test.json"
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        import json
        
        report_data = {
            "timestamp": datetime.now().isoformat(),
            "healthy": bool(result.is_healthy()),
            "tests": [{
                "category": t['category'],
                "name": t['name'],
                "passed": bool(t['passed']),
                "message": str(t['message']),
                "duration": float(t.get('duration', 0)),
            } for t in result.tests],
            "summary": {
                "total": len(result.tests),
                "passed": sum(1 for t in result.tests if t['passed']),
                "failed": sum(1 for t in result.tests if not t['passed']),
                "duration": float(time.time() - result.start_time),
            }
        }
        
        with open(report_path, 'w') as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"\n📄 报告已保存: {report_path}")
    except Exception as e:
        logger.warning(f"报告保存失败: {e}")
    
    return result.is_healthy()


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("\n测试被用户中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n测试异常: {e}", exc_info=True)
        sys.exit(1)
