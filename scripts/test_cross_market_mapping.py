#!/usr/bin/env python3
"""
跨市场映射测试脚本

验证:
1. 跨市场映射配置加载
2. 美股 → 港股映射查询
3. 港股 → 美股反向映射查询
4. 美股数据获取 (需要富途 OpenD 运行)
5. 盘前上下文构建

使用方法:
  cd /opt/ftseek/FTSeek
  python scripts/test_cross_market_mapping.py
"""

import sys
import os
import logging

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("test_cross_market")


def test_mapper():
    """测试跨市场映射器"""
    logger.info("=" * 60)
    logger.info("测试 1: 跨市场映射器加载")
    logger.info("=" * 60)

    from app.utils.cross_market_mapper import get_cross_market_mapper

    mapper = get_cross_market_mapper()

    logger.info(f"美股监控名单: {len(mapper.get_us_watch_list())} 只")
    logger.info(f"直接映射: {len(mapper._direct_mapping)} 条")
    logger.info(f"反向映射: {len(mapper._reverse_mapping)} 只港股")
    logger.info(f"套利配对: {len(mapper._arb_pairs)} 对")

    # 测试正向映射
    logger.info("\n--- 正向映射: 美股 → 港股 ---")
    test_us_codes = ["US.NVDA", "US.BABA", "US.TSLA", "US.GS", "US.SPY"]
    for us_code in test_us_codes:
        hk_codes = mapper.get_hk_codes_by_us(us_code)
        info = mapper.get_impact_info(us_code)
        if info:
            logger.info(f"{us_code} → {hk_codes} ({info.impact_type}, corr={info.correlation})")
            logger.info(f"  说明: {info.description}")

    # 测试反向映射
    logger.info("\n--- 反向映射: 港股 → 美股 ---")
    test_hk_codes = ["HK.00981", "HK.09988", "HK.01211", "HK.00388"]
    for hk_code in test_hk_codes:
        us_codes = mapper.get_us_codes_by_hk(hk_code)
        logger.info(f"{hk_code} ← {us_codes}")

    # 测试套利配对
    logger.info("\n--- 套利配对 ---")
    for pair in mapper.get_arb_pairs()[:3]:
        logger.info(f"{pair.us_code} ↔ {pair.hk_code} (阈值: {pair.ratio_threshold*100:.1f}%)")

    return True


def test_config_load():
    """测试配置加载"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 2: 配置加载")
    logger.info("=" * 60)

    # 重新加载配置 (清除缓存)
    from config.config_center import get_config_center, reset_config_center
    reset_config_center()

    from app import config

    logger.info(f"港股名单: {len(config.WATCH_LIST_HK)} 只")
    logger.info(f"美股名单: {len(config.WATCH_LIST_US)} 只")
    logger.info(f"总名单: {len(config.WATCH_LIST)} 只")

    if config.WATCH_LIST_US:
        logger.info(f"美股前 10 只: {config.WATCH_LIST_US[:10]}")
    else:
        logger.warning("美股名单为空!")

    return len(config.WATCH_LIST_US) > 0


def test_us_data_fetcher():
    """测试美股数据获取 (需要富途 OpenD 运行)"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 3: 美股数据获取")
    logger.info("=" * 60)

    try:
        from app.utils.cross_market_mapper import get_cross_market_mapper
        from app.data.us_data_fetcher import get_us_data_fetcher

        mapper = get_cross_market_mapper()
        fetcher = get_us_data_fetcher()

        # 获取核心美股快照
        core_codes = mapper.get_core_us_codes()
        logger.info(f"获取 {len(core_codes)} 只核心美股快照...")

        snapshot = fetcher.get_us_snapshot(core_codes[:10])  # 限制数量测试

        if snapshot:
            logger.info("✅ 美股快照获取成功:")
            for code, data in list(snapshot.items())[:5]:
                change = data.get('change_pct', 0)
                direction = "🟢" if change >= 0 else "🔴"
                logger.info(
                    f"  {direction} {code}: ${data['last_price']:.2f} ({change:+.2f}%)"
                )
        else:
            logger.warning("⚠️ 美股快照为空 (可能 OpenD 未连接或美股非交易时段)")

        # 测试盘前上下文
        logger.info("\n--- 盘前上下文 ---")
        context = fetcher.get_premarket_context(core_codes[:8])
        logger.info(context[:500] + "..." if len(context) > 500 else context)

        fetcher.close()
        return True

    except Exception as e:
        logger.error(f"❌ 美股数据获取失败: {e}")
        return False


def test_premarket_analyst_integration():
    """测试盘前分析器集成"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 4: 盘前分析器美股上下文集成")
    logger.info("=" * 60)

    try:
        from app.pipeline.premarket_analyst import PreMarketAnalyst

        analyst = PreMarketAnalyst()

        # 测试美股上下文获取
        context = analyst._get_us_market_context()

        if context and "美股" in context:
            logger.info("✅ 美股上下文获取成功:")
            logger.info(context[:300] + "..." if len(context) > 300 else context)
            return True
        else:
            logger.warning(f"⚠️ 美股上下文不完整: {context[:100]}")
            return False

    except Exception as e:
        logger.error(f"❌ 盘前分析器集成失败: {e}")
        return False


def main():
    """主测试流程"""
    logger.info("=" * 60)
    logger.info("跨市场映射完整测试")
    logger.info(f"测试时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    results = {}

    # 测试 1: 映射器
    try:
        results['mapper'] = test_mapper()
    except Exception as e:
        logger.error(f"测试 1 失败: {e}")
        results['mapper'] = False

    # 测试 2: 配置加载
    try:
        results['config'] = test_config_load()
    except Exception as e:
        logger.error(f"测试 2 失败: {e}")
        results['config'] = False

    # 测试 3: 美股数据获取 (可选)
    try:
        results['us_data'] = test_us_data_fetcher()
    except Exception as e:
        logger.error(f"测试 3 失败: {e}")
        results['us_data'] = False

    # 测试 4: 盘前分析器集成
    try:
        results['analyst'] = test_premarket_analyst_integration()
    except Exception as e:
        logger.error(f"测试 4 失败: {e}")
        results['analyst'] = False

    # 总结
    logger.info("\n" + "=" * 60)
    logger.info("测试总结")
    logger.info("=" * 60)

    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        logger.info(f"  {name}: {status}")

    all_passed = all(results.values())
    logger.info(f"\n总体结果: {'✅ 全部通过' if all_passed else '⚠️ 部分失败'}")

    return all_passed


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
