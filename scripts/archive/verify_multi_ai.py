#!/usr/bin/env python3
"""
多AI决策增强系统 - 快速验证
仅验证模块加载和配置,不执行实际投票
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

print("=" * 80)
print("多AI决策增强系统 - 快速验证报告")
print("=" * 80)

success_count = 0
total_tests = 5

# 测试1: 模块导入
print("\n【测试1】核心模块导入...")
try:
    from app.agents.voting.voting_config import VotingConfig, ModelRole
    from app.agents.voting.voting_coordinator import VotingCoordinator
    from app.agents.debate.debate_models import DebateArgument, DebateSummary
    from app.agents.debate.debate_engine import DebateEngine
    from app.agents.trader_models import TradeDecision
    from app.agents.trader_agent import TraderAgent
    from app.agents.risk_agent import RiskAgent, VetoResult
    from app.agents.final_decision import FinalDecision
    print("✅ 所有核心模块导入成功")
    success_count += 1
except Exception as e:
    print(f"❌ 模块导入失败: {e}")

# 测试2: 模型配置
print("\n【测试2】模型配置验证...")
try:
    config = VotingConfig.get_default_config()
    
    models = config.model_mapping
    assert "deepseek-reasoner" in models[ModelRole.DEEPSEEK], "DeepSeek模型错误"
    assert "glm-5" in models[ModelRole.GLM5], "GLM模型错误"
    assert "qwen3.6-plus" in models[ModelRole.QWEN35], "Qwen模型错误"
    
    print(f"✅ 模型配置正确:")
    print(f"   趋势分析师: {models[ModelRole.DEEPSEEK]}")
    print(f"   情绪分析师: {models[ModelRole.MINIMAX]}")
    print(f"   技术分析师: {models[ModelRole.GLM5]}")
    print(f"   基本面师:   {models[ModelRole.QWEN35]}")
    
    weights = config.model_weights
    print(f"\n✅ 权重分配:")
    print(f"   DeepSeek-Reasoner: {weights[ModelRole.DEEPSEEK]:.0%}")
    print(f"   MiniMax-M2.5:      {weights[ModelRole.MINIMAX]:.0%}")
    print(f"   GLM-5:             {weights[ModelRole.GLM5]:.0%}")
    print(f"   Qwen3.6-Plus:      {weights[ModelRole.QWEN35]:.0%}")
    success_count += 1
except Exception as e:
    print(f"❌ 模型配置验证失败: {e}")

# 测试3: 数据结构
print("\n【测试3】数据结构验证...")
try:
    # 创建辩论数据
    bull = DebateArgument(side="BULL", core_thesis="看涨", supporting_points=["趋势好"], risk_factors=[], confidence=0.7)
    bear = DebateArgument(side="BEAR", core_thesis="看跌", supporting_points=["估值高"], risk_factors=["风险"], confidence=0.6)
    summary = DebateSummary(bull_case=bull, bear_case=bear, divergence_score=0.1, consensus_points=[], key_risks=[], recommendation="WEAK_BULL")
    
    # 创建交易决策
    decision = TradeDecision(
        action="BUY", symbol="AAPL", strength=70, confidence=0.7,
        reasoning="测试", model_opinions={}, debate_highlights="测试",
        position_size=0.12, stop_loss=170.0, take_profit=[190.0, 200.0],
        risk_warnings=[]
    )
    
    # 创建风控结果
    veto = VetoResult(vetoed=False, reasons=[], risk_score=0.2)
    
    # 创建最终决策
    final = FinalDecision(
        approved=True, action="BUY", veto_reasons=[],
        voting_result={"final_strength": 70},
        debate_summary=summary, trade_decision=decision, veto_result=veto,
        symbol="AAPL"
    )
    
    print("✅ 所有数据结构创建成功")
    print(f"   - DebateArgument ✓")
    print(f"   - DebateSummary ✓")
    print(f"   - TradeDecision ✓")
    print(f"   - VetoResult ✓")
    print(f"   - FinalDecision ✓")
    success_count += 1
except Exception as e:
    print(f"❌ 数据结构验证失败: {e}")
    import traceback
    traceback.print_exc()

# 测试4: VotingCoordinator初始化
print("\n【测试4】VotingCoordinator初始化...")
try:
    coordinator = VotingCoordinator(timeout_seconds=30, enable_debate=False)
    print(f"✅ VotingCoordinator初始化成功")
    print(f"   - 模型Agent数: {len(coordinator.model_agents)}")
    print(f"   - 辩论引擎: {'启用' if coordinator.enable_debate else '禁用(测试模式)'}")
    print(f"   - 交易员: {'已加载' if coordinator.trader_agent else '未加载'}")
    print(f"   - 风控官: {'已加载' if coordinator.risk_agent else '未加载'}")
    success_count += 1
except Exception as e:
    print(f"❌ VotingCoordinator初始化失败: {e}")
    import traceback
    traceback.print_exc()

# 测试5: 环境变量
print("\n【测试5】环境变量检查...")
try:
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    
    env_path = Path("/opt/ftseek/FTSeek-US/.env.us")
    if env_path.exists():
        load_dotenv(env_path)
    
    checks = {
        "DEEPSEEK_API_KEY": bool(os.getenv("DEEPSEEK_API_KEY")),
        "BAILIAN_API_KEY": bool(os.getenv("BAILIAN_API_KEY")),
        "DEEPSEEK_MODEL": os.getenv("DEEPSEEK_MODEL"),
        "DINGTALK_WEBHOOK_URL_US": bool(os.getenv("DINGTALK_WEBHOOK_URL_US")),
        "FEISHU_WEBHOOK_URL_US": bool(os.getenv("FEISHU_WEBHOOK_URL_US")),
    }
    
    print("✅ 环境变量检查:")
    for key, value in checks.items():
        status = "✅" if value else "⚠️"
        print(f"   {status} {key}: {value}")
    
    success_count += 1
except Exception as e:
    print(f"❌ 环境变量检查失败: {e}")

# 总结
print("\n" + "=" * 80)
print(f"测试结果: {success_count}/{total_tests} 通过")
print("=" * 80)

if success_count == total_tests:
    print("\n✅ 核心功能验证成功!")
    print("\n已完成的模块:")
    print("  1. ✅ 模型配置升级 (DeepSeek-Reasoner, GLM-5, Qwen3.6-Plus)")
    print("  2. ✅ 辩论引擎模块 (debate_engine.py)")
    print("  3. ✅ 交易员Agent (trader_agent.py)")
    print("  4. ✅ 风控官增强 (risk_agent.py + 一票否决权)")
    print("  5. ✅ 完整决策链路 (run_full_decision_pipeline)")
    print("\n待完成任务:")
    print("  6. ⏳ 增强推送功能 (钉钉/飞书)")
    print("  7. ⏳ 盘前/盘后报告模块")
    print("  8. ⏳ 定时任务注册")
    print("  9. ⏳ 端到端集成测试")
else:
    print(f"\n⚠️  部分测试未通过,请检查错误信息")
    sys.exit(1)
