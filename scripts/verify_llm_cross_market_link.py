#!/usr/bin/env python3
"""
跨市场LLM预分析链路验证脚本

功能:
1. 验证LLM分析师实例是否正确传递
2. 验证共识引擎LLM集成是否初始化
3. 验证缓存键匹配机制
4. 模拟端到端流程测试

使用方法:
  cd /opt/ftseek/FTSeek
  python scripts/verify_llm_cross_market_link.py
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
logger = logging.getLogger("verify_llm_link")


def test_llm_analyst_initialization():
    """测试1: LLM分析师初始化"""
    logger.info("=" * 80)
    logger.info("测试1: LLM分析师初始化")
    logger.info("=" * 80)
    
    try:
        from config.config_center import get_config_center
        config_center = get_config_center()
        
        llm_config = config_center.get('ai.llm_market_analyst', {})
        if hasattr(llm_config, 'to_dict'):
            llm_config = llm_config.to_dict()
        
        logger.info(f"LLM配置: enabled={llm_config.get('enabled')}, model={llm_config.get('model')}")
        logger.info(f"缓存TTL: {llm_config.get('cache_ttl')}秒")
        logger.info(f"时间窗口: {llm_config.get('trading_window', {}).get('start')} - {llm_config.get('trading_window', {}).get('end')}")
        
        if not llm_config.get('enabled'):
            logger.warning("⚠️  LLM分析师未启用,请在config.yaml中设置 llm_market_analyst.enabled: true")
            return False
        
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        analyst = LLMMarketAnalyst(llm_config)
        
        logger.info(f"✅ LLM分析师初始化成功: enabled={analyst.enabled}, model={analyst.model}")
        logger.info(f"   缓存TTL: {analyst.cache_ttl}秒, 最大缓存: {analyst.max_cache_size}")
        return True
        
    except Exception as e:
        logger.error(f"❌ LLM分析师初始化失败: {e}", exc_info=True)
        return False


def test_consensus_engine_llm_integration():
    """测试2: 共识引擎LLM集成"""
    logger.info("=" * 80)
    logger.info("测试2: 共识引擎LLM集成")
    logger.info("=" * 80)
    
    try:
        from config.config_center import get_config_center
        config_center = get_config_center()
        
        llm_config = config_center.get('ai.llm_market_analyst', {})
        if hasattr(llm_config, 'to_dict'):
            llm_config = llm_config.to_dict()
        
        if not llm_config.get('enabled'):
            logger.warning("⚠️  LLM未启用,跳过共识引擎集成测试")
            return True
        
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        from app.strategy.consensus_engine import ConsensusEngine
        
        # 创建LLM分析师
        analyst = LLMMarketAnalyst(llm_config)
        
        # 创建共识引擎
        consensus = ConsensusEngine(
            window_seconds=60,
            threshold=1.5,
            adx_trend=25,
            adx_range=20,
            on_consensus=None,
            allow_short=False,
        )
        
        # 获取LLM权重配置
        llm_cfg = config_center.get('ai.llm_market_analyst.consensus_integration', {})
        if hasattr(llm_cfg, 'to_dict'):
            llm_cfg = llm_cfg.to_dict()
        
        weight = llm_cfg.get('weight', 0.15)
        adaptive = llm_cfg.get('adaptive', {})
        
        # 初始化LLM集成
        consensus.init_llm_integration(analyst, weight, adaptive)
        
        logger.info(f"✅ 共识引擎LLM集成成功:")
        logger.info(f"   _llm_consensus_enabled={consensus._llm_consensus_enabled}")
        logger.info(f"   _llm_consensus_weight={consensus._llm_consensus_weight}")
        logger.info(f"   _llm_analyst={'已注入' if consensus._llm_analyst else '未注入'}")
        
        return consensus._llm_consensus_enabled and consensus._llm_analyst is not None
        
    except Exception as e:
        logger.error(f"❌ 共识引擎LLM集成失败: {e}", exc_info=True)
        return False


def test_cache_key_matching():
    """测试3: 缓存键匹配机制"""
    logger.info("=" * 80)
    logger.info("测试3: 缓存键匹配机制")
    logger.info("=" * 80)
    
    try:
        from config.config_center import get_config_center
        config_center = get_config_center()
        
        llm_config = config_center.get('ai.llm_market_analyst', {})
        if hasattr(llm_config, 'to_dict'):
            llm_config = llm_config.to_dict()
        
        if not llm_config.get('enabled'):
            logger.warning("⚠️  LLM未启用,跳过缓存键测试")
            return True
        
        from app.ai.llm_market_analyst import LLMMarketAnalyst, MarketAssessment, Direction, ConfidenceLevel, PositionTier
        import time
        
        analyst = LLMMarketAnalyst(llm_config)
        
        # 模拟调度器写入缓存(使用SCHEDULED触发类型)
        test_code = "HK.00700"
        mock_assessment = MarketAssessment(
            direction=Direction.BUY,
            confidence_level=ConfidenceLevel.HIGH,
            position_tier=PositionTier.HALF,
            key_factors=["测试因素1", "测试因素2"],
            risk_flags=[],
            recommended_timing="IMMEDIATE",
            reasoning="测试用模拟研判结果",
            ttl_seconds=600,
            timestamp=time.time(),
            trigger_type="SCHEDULED",
        )
        
        # 手动写入缓存(模拟调度器预分析)
        cache_key = analyst._cache_key(test_code, "SCHEDULED", None, None)
        analyst._cache[cache_key] = mock_assessment
        logger.info(f"✅ 模拟调度器写入缓存: key={cache_key}")
        
        # 测试1: 使用SCHEDULED读取(应该命中)
        result_scheduled = analyst.get_cached_assessment(test_code, trigger_type="SCHEDULED")
        if result_scheduled and result_scheduled.direction == "BUY":
            logger.info(f"✅ 使用SCHEDULED读取成功: direction={result_scheduled.direction}")
        else:
            logger.error(f"❌ 使用SCHEDULED读取失败")
            return False
        
        # 测试2: 使用COMBINED读取(旧代码,应该失败)
        result_combined = analyst.get_cached_assessment(test_code, trigger_type="COMBINED")
        if result_combined is None:
            logger.info(f"✅ 使用COMBINED读取 correctly returns None (缓存键不匹配)")
        else:
            logger.warning(f"⚠️  使用COMBINED读取意外命中(这可能表明缓存键逻辑有变化)")
        
        logger.info("✅ 缓存键匹配机制验证通过")
        return True
        
    except Exception as e:
        logger.error(f"❌ 缓存键匹配测试失败: {e}", exc_info=True)
        return False


def test_data_subscriber_signature():
    """测试4: DataSubscriber构造函数签名"""
    logger.info("=" * 80)
    logger.info("测试4: DataSubscriber构造函数签名")
    logger.info("=" * 80)
    
    try:
        import inspect
        from app.data_subscriber import DataSubscriber
        
        sig = inspect.signature(DataSubscriber.__init__)
        params = list(sig.parameters.keys())
        
        logger.info(f"DataSubscriber.__init__ 参数: {params}")
        
        if 'llm_analyst' in params:
            logger.info("✅ DataSubscriber已支持llm_analyst参数")
            return True
        else:
            logger.error("❌ DataSubscriber缺少llm_analyst参数")
            return False
        
    except Exception as e:
        logger.error(f"❌ DataSubscriber签名检查失败: {e}", exc_info=True)
        return False


def main():
    """主测试流程"""
    logger.info("\n")
    logger.info("╔" + "=" * 78 + "╗")
    logger.info("║" + " " * 20 + "跨市场LLM预分析链路验证" + " " * 31 + "║")
    logger.info("╚" + "=" * 78 + "╝")
    logger.info("\n")
    
    results = {
        "LLM分析师初始化": test_llm_analyst_initialization(),
        "共识引擎LLM集成": test_consensus_engine_llm_integration(),
        "缓存键匹配机制": test_cache_key_matching(),
        "DataSubscriber签名": test_data_subscriber_signature(),
    }
    
    logger.info("\n")
    logger.info("=" * 80)
    logger.info("测试结果汇总:")
    logger.info("=" * 80)
    
    all_passed = True
    for test_name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        logger.info(f"  {test_name}: {status}")
        if not passed:
            all_passed = False
    
    logger.info("=" * 80)
    if all_passed:
        logger.info("🎉 所有测试通过! 跨市场LLM预分析链路已正确配置")
        logger.info("\n下一步:")
        logger.info("  1. 重启FTSeek服务: ./restart.sh")
        logger.info("  2. 观察09:25日志: grep 'LLM预分析' logs/ftseek.log")
        logger.info("  3. 观察09:30日志: grep 'LLM置信度调节' logs/ftseek.log")
        logger.info("  4. 检查钉钉是否收到LLM研判推送")
    else:
        logger.error("⚠️  部分测试失败,请检查上述错误信息")
        logger.info("\n常见问题:")
        logger.info("  - LLM未启用: 检查 config.yaml 中 llm_market_analyst.enabled")
        logger.info("  - BAILIAN_API_KEY未配置: 检查 .env 文件")
        logger.info("  - 依赖缺失: 运行 pip install -r requirements.txt")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
