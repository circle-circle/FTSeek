#!/usr/bin/env python3
"""
FTSeek 跨市场模块 - 完整自查自纠脚本 (v2)

检查项:
1. 文件存在性
2. 配置加载
3. 数据源连通性
4. 跨市场事件缓存
5. 双重上市套利
6. 盘前分析器集成
7. 配置一致性

使用方法:
  cd /opt/ftseek/FTSeek
  python3.8 scripts/self_check_cross_market.py
"""

import sys
import os
import logging
import json
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("self_check")


class SelfCheckResult:
    """自查结果"""

    def __init__(self):
        self.checks = []
        self.warnings = []
        self.errors = []

    def add(self, name: str, passed: bool, message: str = ""):
        self.checks.append({"name": name, "passed": passed, "message": message})
        if not passed:
            self.errors.append(f"{name}: {message}")
        elif message and "存在" not in message:
            self.warnings.append(message)

    def is_healthy(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "healthy": self.is_healthy(),
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c['passed']),
            "errors": self.errors,
            "warnings": self.warnings,
        }


def run_self_check() -> SelfCheckResult:
    """执行完整自查"""
    result = SelfCheckResult()
    base_dir = "/opt/ftseek/FTSeek"

    # ============================================================
    # 1. 文件存在性检查
    # ============================================================
    logger.info("=" * 60)
    logger.info("1. 文件存在性检查")
    logger.info("=" * 60)

    files = {
        "跨市场映射配置": "config/cross_market_mapping.yaml",
        "美股数据配置": "config/us_data_unified_unified_config.yaml",
        "主配置文件": "config/unified_unified_unified_config.yaml",
        "跨市场映射器": "app/utils/cross_market_mapper.py",
        "美股数据获取": "app/data/us_data_fetcher.py",
        "美股数据配置器": "app/data/us_data_config.py",
        "事件缓存": "app/data/cross_market_event_cache.py",
        "套利策略": "app/strategy/arb_dual_listed.py",
        "盘前分析器": "app/pipeline/premarket_analyst.py",
        "事件采集脚本": "scripts/cross_market_event_cron.py",
        "标准策略基类": "app/strategy/standard_strategy.py",
        "回测引擎": "app/backtest/engine.py",
        "示例策略": "app/strategy/example_strategies.py",
        "回测 CLI": "scripts/backtest_cli.py",
        "回测测试": "scripts/test_backtest.py",
        "三重屏障": "app/factors/triple_barrier.py",
        "LLM 防火墙": "app/factors/llm_firewall.py",
        "执行监控": "app/monitoring/execution_quality.py",
    }

    for name, path in files.items():
        exists = os.path.exists(os.path.join(base_dir, path))
        result.add(f"文件: {name}", exists, f"{'存在' if exists else '缺失'}")
        logger.info(f"  {'✅' if exists else '❌'} {name}: {path}")

    # ============================================================
    # 2. 配置加载检查
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("2. 配置加载检查")
    logger.info("=" * 60)

    # 跨市场映射
    try:
        from app.utils.cross_market_mapper import get_cross_market_mapper
        mapper = get_cross_market_mapper()
        result.add(
            "跨市场映射", True,
            f"美股: {len(mapper.get_us_watch_list())}, "
            f"映射: {len(mapper._direct_mapping)}, "
            f"套利: {len(mapper._arb_pairs)}"
        )
        logger.info(f"  ✅ 映射: {len(mapper._direct_mapping)} 条, 套利: {len(mapper._arb_pairs)} 对")
    except Exception as e:
        result.add("跨市场映射", False, str(e))
        logger.error(f"  ❌ 映射加载失败: {e}")

    # 美股数据配置
    try:
        from app.data.us_data_config import get_us_data_config
        us_config = get_us_data_config()
        status = us_config.self_check()
        result.add("美股数据配置", status['healthy'], f"数据源: {len(status['sources'])}")
        for prov, s in status['sources'].items():
            logger.info(f"  {'✅' if s['enabled'] else '⚠️'} {prov}: key={s['key_preview']}")
    except Exception as e:
        result.add("美股数据配置", False, str(e))
        logger.error(f"  ❌ 数据配置失败: {e}")

    # ============================================================
    # 3. 数据源连通性
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("3. 数据源连通性检查")
    logger.info("=" * 60)

    try:
        from app.data.us_data_fetcher import get_us_data_fetcher
        fetcher = get_us_data_fetcher()
        health = fetcher.get_health_status()

        for prov, s in health.items():
            result.add(f"数据源: {prov}", s['enabled'], f"enabled={s['enabled']}")
            logger.info(f"  {'✅' if s['enabled'] else '⚠️'} {prov}")

        # 测试实际获取
        for sym in ["US.SPY", "US.BABA"]:
            quote = fetcher.get_quote(sym)
            if quote and quote.get("price", 0) > 0:
                result.add(f"数据获取: {sym}", True, f"${quote['price']:.2f}")
                logger.info(f"  ✅ {sym}: ${quote['price']:.2f}")
            else:
                result.add(f"数据获取: {sym}", False, "获取失败")
                logger.warning(f"  ⚠️ {sym}: 获取失败")

    except Exception as e:
        result.add("数据源连通性", False, str(e))
        logger.error(f"  ❌ 数据源检查失败: {e}")

    # ============================================================
    # 4. 跨市场事件缓存
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("4. 跨市场事件缓存检查")
    logger.info("=" * 60)

    try:
        from app.data.cross_market_event_cache import get_cross_market_event_cache, CrossMarketEvent
        cache = get_cross_market_event_cache()

        # 测试写入
        test_event = CrossMarketEvent(
            event_id=f"test_{int(datetime.now().timestamp())}",
            timestamp=datetime.now().timestamp(),
            event_type="test",
            us_code="US.TEST",
            hk_codes=["HK.00700"],
            title="测试事件",
            content="这是测试",
            impact_score=0.5,
        )
        cache.write(test_event)
        result.add("事件缓存写入", True, "成功")
        logger.info("  ✅ 事件缓存写入成功")

        # 测试读取
        events = cache.read_for_premarket(hours=1)
        result.add("事件缓存读取", len(events) > 0, f"{len(events)} 个事件")
        logger.info(f"  ✅ 读取 {len(events)} 个事件")

        # 统计
        stats = cache.get_stats()
        result.add("事件缓存统计", True, f"总计: {stats.get('total_events', 0)}")
        logger.info(f"  ✅ 统计: {stats}")

        # 清理测试数据
        cache.cleanup_expired(hours=0.01)

    except Exception as e:
        result.add("跨市场事件缓存", False, str(e))
        logger.error(f"  ❌ 事件缓存检查失败: {e}")

    # ============================================================
    # 5. 双重上市套利
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("5. 双重上市套利检查")
    logger.info("=" * 60)

    try:
        from app.strategy.arb_dual_listed import get_dual_listed_arb, ArbPairConfig
        arb = get_dual_listed_arb()

        # 检查配置
        result.add("套利配对配置", len(arb.pairs) > 0, f"{len(arb.pairs)} 对")
        logger.info(f"  ✅ 套利配对: {len(arb.pairs)} 对")

        for pair in arb.pairs:
            logger.info(f"    {pair.us_code} ↔ {pair.hk_code} ({pair.description}) 阈值: {pair.threshold_pct}%")

        # 测试信号生成 (模拟数据)
        us_prices = {"US.BABA": 126.0, "US.JD": 28.5, "US.BIDU": 111.0}
        hk_prices = {"HK.09988": 980.0, "HK.09618": 220.0, "HK.09888": 865.0}

        signals = arb.generate_signals(us_prices, hk_prices)
        result.add("套利信号生成", True, f"{len(signals)} 个信号")
        logger.info(f"  ✅ 信号生成: {len(signals)} 个")

        for s in signals:
            logger.info(f"    {s.hk_code} {s.direction} 价差: {s.spread_pct:+.2f}% 置信度: {s.confidence:.0%}")

        # 测试报告
        report = arb.format_report(signals)
        result.add("套利报告", len(report) > 0, f"{len(report)} 字符")
        logger.info(f"  ✅ 报告: {len(report)} 字符")

    except Exception as e:
        result.add("双重上市套利", False, str(e))
        logger.error(f"  ❌ 套利检查失败: {e}")

    # ============================================================
    # 6. 盘前分析器集成
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("6. 盘前分析器集成检查")
    logger.info("=" * 60)

    try:
        from app.pipeline.premarket_analyst import PreMarketAnalyst
        analyst = PreMarketAnalyst()

        # 检查方法
        methods = ['_get_us_market_context', '_get_cross_market_events_context', '_get_arb_context']
        for method in methods:
            has_method = hasattr(analyst, method)
            result.add(f"方法: {method}", has_method, f"{'存在' if has_method else '缺失'}")
            logger.info(f"  {'✅' if has_method else '❌'} {method}")

        # 测试获取上下文
        context = analyst._get_us_market_context()
        if context and "数据获取失败" not in context:
            lines = context.split('\n')
            result.add("盘前美股上下文", True, f"{len(lines)} 行")
            logger.info(f"  ✅ 上下文: {len(lines)} 行")
        else:
            result.add("盘前美股上下文", False, "获取失败")
            logger.warning(f"  ⚠️ 上下文获取失败")

    except Exception as e:
        result.add("盘前分析器集成", False, str(e))
        logger.error(f"  ❌ 盘前分析器检查失败: {e}")

    # ============================================================
    # 7. 配置一致性
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("7. 配置一致性检查")
    logger.info("=" * 60)

    try:
        from config.config_center import get_config_center
        cc = get_config_center()
        us_list = cc.get('trading.watch_list_us')

        if us_list:
            result.add("unified_unified_config.yaml watch_list_us", True, f"{len(us_list)} 只")
            logger.info(f"  ✅ watch_list_us: {len(us_list)} 只")

            # 检查核心美股是否在列表中
            from app.utils.cross_market_mapper import get_cross_market_mapper
            mapper = get_cross_market_mapper()
            core_codes = mapper.get_core_us_codes()
            missing = [c for c in core_codes if c not in us_list]

            if missing:
                result.add("核心美股一致性", False, f"缺失: {missing[:5]}")
                logger.warning(f"  ⚠️ {len(missing)} 只核心美股不在 watch_list_us 中")
            else:
                result.add("核心美股一致性", True, "一致")
                logger.info(f"  ✅ 核心美股一致性: 一致")
        else:
            result.add("unified_unified_config.yaml watch_list_us", False, "为空")
            logger.warning(f"  ⚠️ watch_list_us 为空")

    except Exception as e:
        result.add("配置一致性", False, str(e))
        logger.error(f"  ❌ 配置一致性检查失败: {e}")

    # ============================================================
    # 8. 回测系统检查
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("8. 回测系统检查")
    logger.info("=" * 60)

    # StandardStrategy 基类
    try:
        from app.strategy.standard_strategy import StandardStrategy
        from app.strategy.example_strategies import RSIMeanReversion, MACross, MACDMomentum
        
        for cls in [RSIMeanReversion, MACross, MACDMomentum]:
            s = cls()
            result.add(f"策略: {s.name}", True, f"v{s.version} ({s.timeframe})")
            logger.info(f"  ✅ {s.name}: v{s.version} ({s.timeframe})")
    except Exception as e:
        result.add("回测策略", False, str(e))
        logger.error(f"  ❌ 回测策略检查失败: {e}")

    # 回测引擎
    try:
        from app.backtest.engine import get_backtest_engine
        engine = get_backtest_engine()
        result.add("回测引擎", True, f"capital=${engine.initial_capital:,.0f}")
        logger.info(f"  ✅ 回测引擎: capital=${engine.initial_capital:,.0f}")
        
        # 快速测试
        import pandas as pd
        import numpy as np
        np.random.seed(42)
        dates = pd.date_range('2025-01-01', periods=100, freq='1h')
        close = 100 * np.exp(np.cumsum(np.random.normal(0, 0.01, 100)))
        test_df = pd.DataFrame({
            'open': close * 0.999, 'high': close * 1.005,
            'low': close * 0.995, 'close': close, 'volume': 1e6,
        }, index=dates)
        
        strategy = RSIMeanReversion()
        bt_result = engine.run(strategy, test_df, pair='TEST')
        result.add("快速回测", bt_result.final_capital > 0, 
                  f"${bt_result.final_capital:,.0f} ({bt_result.total_trades} trades)")
        logger.info(f"  ✅ 快速回测: ${bt_result.final_capital:,.0f} ({bt_result.total_trades} trades)")
    except Exception as e:
        result.add("回测引擎", False, str(e))
        logger.error(f"  ❌ 回测引擎检查失败: {e}")

    # ============================================================
    # 输出报告
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("自查报告")
    logger.info("=" * 60)

    passed = sum(1 for c in result.checks if c['passed'])
    total = len(result.checks)

    logger.info(f"检查项: {passed}/{total} 通过")
    if result.errors:
        for e in result.errors:
            logger.error(f"  ❌ {e}")
    if result.warnings:
        for w in result.warnings:
            logger.warning(f"  ⚠️ {w}")

    logger.info(f"\n总体状态: {'✅ 健康' if result.is_healthy() else '⚠️ 存在问题'}")

    # 保存报告
    report_path = "/opt/ftseek/FTSeek/logs/cross_market_self_check_v2.json"
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info(f"报告已保存: {report_path}")
    except Exception as e:
        logger.warning(f"报告保存失败: {e}")

    return result


if __name__ == "__main__":
    try:
        result = run_self_check()
        sys.exit(0 if result.is_healthy() else 1)
    except KeyboardInterrupt:
        logger.info("\n自查被用户中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n自查异常: {e}", exc_info=True)
        sys.exit(1)
