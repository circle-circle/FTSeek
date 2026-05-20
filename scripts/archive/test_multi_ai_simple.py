#!/usr/bin/env python3
"""
多AI决策增强系统 - 简化测试 (仅验证模块加载和结构)
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

print("=" * 80)
print("多AI决策增强系统 - 模块加载测试")
print("=" * 80)

# 测试1: 导入模块
print("\n【测试1】导入核心模块...")
try:
    from app.agents.voting.voting_config import VotingConfig, ModelRole
    print("✅ voting_config 导入成功")
    
    from app.agents.voting.model_agent import ModelAgent, ModelScore
    print("✅ model_agent 导入成功")
    
    from app.agents.voting.voting_aggregator import VotingAggregator
    print("✅ voting_aggregator 导入成功")
    
    from app.agents.voting.voting_coordinator import VotingCoordinator
    print("✅ voting_coordinator 导入成功")
    
    from app.agents.debate.debate_models import DebateArgument, DebateSummary, DebateRound
    print("✅ debate_models 导入成功")
    
    from app.agents.debate.debate_engine import DebateEngine, ResearcherAgent
    print("✅ debate_engine 导入成功")
    
    from app.agents.trader_models import TradeDecision
    print("✅ trader_models 导入成功")
    
    from app.agents.trader_agent import TraderAgent
    print("✅ trader_agent 导入成功")
    
    from app.agents.risk_agent import RiskAgent, VetoResult
    print("✅ risk_agent 导入成功")
    
    from app.agents.final_decision import FinalDecision
    print("✅ final_decision 导入成功")
    
except Exception as e:
    print(f"❌ 模块导入失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 测试2: 验证模型配置
print("\n【测试2】验证模型配置...")
try:
    config = VotingConfig.get_default_config()
    
    print(f"模型映射:")
    for role, model in config.model_mapping.items():
        print(f"  - {role.value}: {model}")
    
    print(f"\n权重分配:")
    for role, weight in config.model_weights.items():
        print(f"  - {role.value}: {weight:.0%}")
    
    # 验证新模型
    assert "deepseek-reasoner" in config.model_mapping[ModelRole.DEEPSEEK]
    assert "glm-5" in config.model_mapping[ModelRole.GLM5]
    assert "qwen3.6-plus" in config.model_mapping[ModelRole.QWEN35]
    print("\n✅ 模型配置验证通过 (DeepSeek-Reasoner, GLM-5, Qwen3.6-Plus)")
    
except Exception as e:
    print(f"❌ 模型配置验证失败: {e}")
    sys.exit(1)

# 测试3: 验证数据结构
print("\n【测试3】验证数据结构...")
try:
    # DebateArgument
    bull_arg = DebateArgument(
        side="BULL",
        core_thesis="测试多头论点",
        supporting_points=["论据1", "论据2"],
        risk_factors=["风险1"],
        confidence=0.75,
    )
    print(f"✅ DebateArgument 创建成功: {bull_arg.core_thesis}")
    
    # DebateSummary
    bear_arg = DebateArgument(
        side="BEAR",
        core_thesis="测试空头论点",
        supporting_points=["论据1"],
        risk_factors=["风险1", "风险2"],
        confidence=0.6,
    )
    
    summary = DebateSummary(
        bull_case=bull_arg,
        bear_case=bear_arg,
        divergence_score=0.15,
        consensus_points=["共识点1"],
        key_risks=["关键风险1"],
        recommendation="WEAK_BULL",
    )
    print(f"✅ DebateSummary 创建成功: {summary.recommendation}")
    
    # TradeDecision
    decision = TradeDecision(
        action="BUY",
        symbol="AAPL",
        strength=72,
        confidence=0.75,
        reasoning="测试决策理由",
        model_opinions={"deepseek": "趋势向上"},
        debate_highlights="辩论摘要",
        position_size=0.12,
        stop_loss=170.0,
        take_profit=[195.0, 210.0, 230.0],
        risk_warnings=["风险警告"],
    )
    print(f"✅ TradeDecision 创建成功: {decision.action}")
    
    # VetoResult
    veto = VetoResult(
        vetoed=False,
        reasons=[],
        risk_score=0.2,
    )
    print(f"✅ VetoResult 创建成功: vetoed={veto.vetoed}")
    
    # FinalDecision
    final = FinalDecision(
        approved=True,
        action="BUY",
        veto_reasons=[],
        voting_result={"final_strength": 72},
        debate_summary=summary,
        trade_decision=decision,
        veto_result=veto,
        symbol="AAPL",
    )
    print(f"✅ FinalDecision 创建成功: {final.action}, approved={final.approved}")
    
except Exception as e:
    print(f"❌ 数据结构验证失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 测试4: 验证VotingCoordinator初始化
print("\n【测试4】验证VotingCoordinator初始化 (不启用辩论)...")
try:
    coordinator = VotingCoordinator(
        timeout_seconds=30,
        enable_debate=False,  # 简化测试,不启用辩论
    )
    print(f"✅ VotingCoordinator 初始化成功")
    print(f"   - 模型Agent数量: {len(coordinator.model_agents)}")
    print(f"   - 辩论引擎: {'启用' if coordinator.enable_debate else '禁用'}")
    
except Exception as e:
    print(f"❌ VotingCoordinator初始化失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 测试5: 模拟投票 (使用fallback规则)
print("\n【测试5】模拟投票 (规则fallback)...")
try:
    market_data = {
        "symbol": "AAPL",
        "price": 185.50,
        "volume_ratio": 1.5,
        "momentum": 0.05,
        "news_sentiment": 0.3,
        "vix": 18.5,
        "spx_change_percent": 0.003,
        "atr": 3.2,
        "ma20": 182.0,
        "ma50": 178.0,
    }
    
    result = coordinator.run_voting(market_data)
    print(f"✅ 投票完成:")
    print(f"   - 综合评分: {result['final_strength']}/100")
    print(f"   - 方向: {result['direction']}")
    print(f"   - 一致性: {result['consensus_level']:.0%}")
    print(f"   - 模型评分数: {len(result.get('model_scores', []))}")
    
except Exception as e:
    print(f"❌ 投票测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 80)
print("✅ 所有测试通过!")
print("=" * 80)
print("\n核心功能验证成功,可以继续进行:")
print("1. 完整LLM调用测试 (需要API Keys)")
print("2. 辩论引擎测试")
print("3. 推送功能集成")
print("4. 定时任务注册")
