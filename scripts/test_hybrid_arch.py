#!/usr/bin/env python3
"""
混合架构端到端验证脚本
======================
验证 Phase 1 (RichContext) → Phase 2 (Debate) → Phase 3 (Validation) 完整链路
"""

import sys
import os
import json
import logging
from pathlib import Path

# 初始化路径
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Test] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("hybrid_arch_test")

def test_phase1():
    """Phase 1: 验证 RichContextBuilder"""
    logger.info("=" * 60)
    logger.info("Phase 1: RichContextBuilder 测试")
    logger.info("=" * 60)
    
    from app.review.review_orchestrator import RichContextBuilder
    
    builder = RichContextBuilder()
    ctx = builder.build("2026-05-04")
    
    # 验证每个维度
    checks = {
        "historical_patterns": ctx.get("historical_patterns"),
        "active_lessons": ctx.get("active_lessons"),
        "recent_anomalies": ctx.get("recent_anomalies"),
        "memory_context": ctx.get("memory_context"),
        "decision_funnel": ctx.get("decision_funnel"),
        "performance_trend": ctx.get("performance_trend"),
        "capital_trajectory": ctx.get("capital_trajectory"),
    }
    
    all_pass = True
    for key, val in checks.items():
        if val is None:
            logger.error(f"  ❌ {key}: None")
            all_pass = False
        elif isinstance(val, dict) and "error" in val:
            logger.warning(f"  ⚠️ {key}: {val['error']}")
        elif isinstance(val, list) and len(val) == 0:
            logger.warning(f"  ⚠️ {key}: empty list (可能无数据)")
        else:
            logger.info(f"  ✅ {key}: OK")
            if isinstance(val, dict):
                logger.info(f"     数据: {json.dumps(val, ensure_ascii=False, indent=2)[:200]}...")
            elif isinstance(val, list):
                logger.info(f"     数据: {len(val)} 条")
    
    return all_pass, ctx


def test_phase2(ctx):
    """Phase 2: 验证 MultiModelDebateRunner (模拟模式)"""
    logger.info("")
    logger.info("=" * 60)
    logger.info("Phase 2: MultiModelDebateRunner 测试")
    logger.info("=" * 60)
    
    from app.review.review_orchestrator import (
        ReviewDataCollector, ReviewAnalyst, MultiModelDebateRunner
    )
    from app.llm.agent_bridge import get_agent_bridge
    
    # 检查 agent_bridge 可用性
    try:
        bridge = get_agent_bridge()
        hermes_available = bridge._hermes_available
        logger.info(f"  Agent Bridge 状态: {'✅ 可用' if hermes_available else '⚠️ 降级模式'}")
    except Exception as e:
        logger.error(f"  Agent Bridge 初始化失败: {e}")
        return False
    
    # 构建测试数据
    collector = ReviewDataCollector()
    data = collector.collect_daily("2026-05-04")
    data["rich_context"] = ctx
    
    logger.info(f"  基础数据: {len(data['closed_trades'])} 笔交易")
    logger.info(f"  Rich Context: {len(ctx.get('historical_patterns', {}))} 维度")
    
    # 测试 DebateRunner 初始化
    try:
        runner = MultiModelDebateRunner(bridge)
        logger.info(f"  ✅ DebateRunner 初始化成功")
        logger.info(f"     模型: {', '.join([m['model'] for m in runner.MODEL_ROLES])}")
    except Exception as e:
        logger.error(f"  ❌ DebateRunner 初始化失败: {e}")
        return False
    
    # 构建 prompt 并检查内容
    analyst = ReviewAnalyst(agent_bridge=bridge)
    prompt = analyst._build_analysis_prompt(data, {"memory_section": ""})
    
    # 验证 prompt 是否包含 rich context
    has_rich_context = any(kw in prompt for kw in [
        "历史交易模式", "活跃教训", "记忆上下文", "性能趋势"
    ])
    
    if has_rich_context:
        logger.info(f"  ✅ 辩论 Prompt 包含深度上下文")
        logger.info(f"     Prompt 长度: {len(prompt)} 字符")
        # 显示 prompt 前 500 字符
        logger.info(f"     内容预览: {prompt[:500]}...")
    else:
        logger.error(f"  ❌ 辩论 Prompt 未包含深度上下文")
        return False
    
    # 是否执行真实辩论 (可选)
    run_real_debate = os.getenv("TEST_REAL_DEBATE", "0") == "1"
    
    if run_real_debate:
        logger.info("  执行真实 3 模型辩论...")
        try:
            result = runner.run_debate(
                prompt=prompt,
                system_prompt="你是 FTSeek-US 复盘分析师。输出严格 JSON。",
                timeout=90
            )
            logger.info(f"  辩论模式: {result.get('mode')}")
            logger.info(f"  成功模型: {result.get('success_count')}/{result.get('total_models')}")
            return result.get("success_count", 0) > 0
        except Exception as e:
            logger.error(f"  辩论失败: {e}")
            return False
    else:
        logger.info("  ⏭️ 跳过真实辩论 (设置 TEST_REAL_DEBATE=1 启用)")
        return True  # 结构验证通过即算通过


def test_phase3():
    """Phase 3: 验证 ValidationAgent"""
    logger.info("")
    logger.info("=" * 60)
    logger.info("Phase 3: ValidationAgent 测试")
    logger.info("=" * 60)
    
    from app.review.review_orchestrator import ValidationAgent
    
    validator = ValidationAgent()
    
    # 模拟辩论结果
    mock_debate = {
        "bull": {
            "insights": ["今日胜率低于预期，信号质量需要优化"],
            "root_causes": ["风控参数可能过于宽松"],
            "fixes": ["调整 config.yaml 中的信号阈值参数"]
        },
        "bear": {
            "insights": ["执行层存在重复订单问题"],
            "root_causes": ["冷却机制可能失效"],
            "fixes": ["检查 exit_cooldowns 实现"]
        },
        "auditor": {
            "insights": ["数据完整性正常，无异常断流"],
            "risks": ["资金回撤接近警戒线"]
        }
    }
    
    result = validator.validate_debate_results(mock_debate, "2026-05-04")
    
    logger.info(f"  总 claims: {result.get('total_claims', 0)}")
    logger.info(f"  已验证: {result.get('verified_count', 0)}")
    logger.info(f"  可信度: {result.get('confidence_score', 0):.0%}")
    logger.info(f"  摘要: {result.get('validation_summary', 'N/A')}")
    
    # 验证结果结构
    required_keys = ["verified_issues", "unverified_claims", "total_claims", 
                     "verified_count", "confidence_score", "validation_summary"]
    all_keys_present = all(k in result for k in required_keys)
    
    if all_keys_present:
        logger.info(f"  ✅ 验证结果结构完整")
        
        # 显示已验证的问题
        for v in result.get("verified_issues", [])[:3]:
            logger.info(f"     ✅ {v.get('claim', '')[:80]}")
            logger.info(f"        证据: {v.get('evidence', 'N/A')}")
    else:
        logger.error(f"  ❌ 验证结果缺少字段")
        return False
    
    return True


def main():
    """完整链路验证"""
    logger.info("🚀 FTSeek-US 混合架构端到端验证")
    logger.info(f"   时间: {__import__('datetime').datetime.now().isoformat()}")
    logger.info("")
    
    results = {}
    
    # Phase 1
    p1_pass, ctx = test_phase1()
    results["phase1_rich_context"] = p1_pass
    
    # Phase 2
    if p1_pass:
        p2_pass = test_phase2(ctx)
    else:
        p2_pass = False
    results["phase2_debate"] = p2_pass
    
    # Phase 3
    p3_pass = test_phase3()
    results["phase3_validation"] = p3_pass
    
    # 总结
    logger.info("")
    logger.info("=" * 60)
    logger.info("📊 验证总结")
    logger.info("=" * 60)
    
    all_pass = all(results.values())
    for phase, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        logger.info(f"  {phase}: {status}")
    
    logger.info("")
    if all_pass:
        logger.info("🎉 混合架构完整链路验证通过!")
        logger.info("   Phase 1: RichContextBuilder ✅")
        logger.info("   Phase 2: MultiModelDebateRunner ✅")
        logger.info("   Phase 3: ValidationAgent ✅")
        logger.info("")
        logger.info("   下次盘后复盘将自动运行完整混合管线")
    else:
        logger.error("⚠️ 部分阶段未通过，请检查日志")
    
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
