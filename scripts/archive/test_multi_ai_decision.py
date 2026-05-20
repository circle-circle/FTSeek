#!/usr/bin/env python3
"""
多AI决策增强系统测试脚本

测试完整决策链路: 分析师投票 → 研究员辩论 → 交易员决策 → 风控否决
"""
import sys
from pathlib import Path

# 添加项目路径 (FTSeek-US根目录)
BASE_DIR = Path(__file__).resolve().parent.parent  # scripts的父目录
sys.path.insert(0, str(BASE_DIR))

from app.agents.voting.voting_coordinator import VotingCoordinator
from app.agents.final_decision import FinalDecision


def test_full_decision_pipeline():
    """测试完整决策管线"""
    print("=" * 80)
    print("多AI决策增强系统 - 端到端测试")
    print("=" * 80)
    
    # 初始化协调器
    coordinator = VotingCoordinator(
        timeout_seconds=90,
        enable_debate=True,  # 启用辩论
    )
    
    # 模拟市场数据
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
        "trend_direction": "UP",
        "market_regime": "BULL",
    }
    
    print(f"\n【测试标的】 {market_data['symbol']} @ ${market_data['price']:.2f}")
    print(f"【市场环境】 VIX={market_data['vix']}, 趋势={market_data['trend_direction']}")
    
    # 运行完整决策管线
    print("\n" + "=" * 80)
    print("开始执行完整决策管线...")
    print("=" * 80)
    
    final_decision: FinalDecision = coordinator.run_full_decision_pipeline(market_data)
    
    # 输出结果
    print("\n" + "=" * 80)
    print("【最终决策结果】")
    print("=" * 80)
    print(f"是否批准: {'✅ 是' if final_decision.approved else '❌ 否'}")
    print(f"操作建议: {final_decision.action}")
    
    if final_decision.veto_reasons:
        print(f"\n🚫 风控否决理由:")
        for reason in final_decision.veto_reasons:
            print(f"  - {reason}")
    
    # 投票结果
    print(f"\n【分析师团队投票】")
    print(f"综合评分: {final_decision.voting_result['final_strength']}/100")
    print(f"方向: {final_decision.voting_result['direction']}")
    print(f"一致性: {final_decision.voting_result['consensus_level']:.0%}")
    print(f"\n各模型评分:")
    for ms in final_decision.voting_result.get('model_scores', []):
        print(f"  - {ms['model_id']}: {ms['strength']}分 - {ms['reasoning']}")
    
    # 辩论结果
    print(f"\n【研究员辩论】")
    print(f"多头观点: {final_decision.debate_summary.bull_case.core_thesis}")
    print(f"  支撑: {', '.join(final_decision.debate_summary.bull_case.supporting_points)}")
    print(f"\n空头观点: {final_decision.debate_summary.bear_case.core_thesis}")
    print(f"  支撑: {', '.join(final_decision.debate_summary.bear_case.supporting_points)}")
    print(f"\n分歧度: {final_decision.debate_summary.divergence_score:.0%}")
    print(f"建议: {final_decision.debate_summary.recommendation}")
    
    # 交易员决策
    if final_decision.trade_decision:
        print(f"\n【交易员决策】")
        print(f"操作: {final_decision.trade_decision.action}")
        print(f"强度: {final_decision.trade_decision.strength}/100")
        print(f"信心: {final_decision.trade_decision.confidence:.0%}")
        print(f"仓位: {final_decision.trade_decision.position_size:.0%}")
        print(f"止损: ${final_decision.trade_decision.stop_loss:.2f}")
        print(f"止盈: ${', '.join([f'{p:.2f}' for p in final_decision.trade_decision.take_profit])}")
        print(f"\n决策理由: {final_decision.trade_decision.reasoning}")
        
        if final_decision.trade_decision.risk_warnings:
            print(f"\n⚠️ 风险提示:")
            for warning in final_decision.trade_decision.risk_warnings:
                print(f"  - {warning}")
    
    # 风控审查
    if final_decision.veto_result:
        print(f"\n【风控审查】")
        print(f"风险评分: {final_decision.veto_result.risk_score:.2f}")
        print(f"否决: {'是' if final_decision.veto_result.vetoed else '否'}")
    
    print("\n" + "=" * 80)
    print("测试完成!")
    print("=" * 80)
    
    return final_decision


if __name__ == "__main__":
    try:
        result = test_full_decision_pipeline()
        print(f"\n✅ 测试成功: {result.symbol} -> {result.action}")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
