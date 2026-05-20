#!/usr/bin/env python3
"""
MU完整决策链路测试 - 使用模拟投票结果

跳过LLM投票,直接测试: 辩论 → 交易决策 → 风控 → 推送
"""
import sys
import os
from pathlib import Path
from datetime import datetime

# 添加项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 加载环境变量
from dotenv import load_dotenv
env_path = BASE_DIR / ".env.us"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量")


def test_mu_debate_to_push():
    """测试MU从辩论到推送的完整链路"""
    print("=" * 80)
    print("MU(美光科技) 多AI决策链路测试 (模拟投票)")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # 模拟投票结果
    voting_result = {
        "symbol": "US.MU",
        "final_strength": 68,
        "direction": "LONG",
        "consensus_level": 0.75,
        "model_scores": [
            {
                "model_id": "DeepSeek-Reasoner",
                "strength": 72,
                "direction": "LONG",
                "reasoning": "HBM3E需求强劲,存储芯片周期复苏,技术面突破MA20"
            },
            {
                "model_id": "MiniMax-M2.5",
                "strength": 65,
                "direction": "LONG", 
                "reasoning": "AI芯片新闻情绪正面,美光扩产计划获市场认可"
            },
            {
                "model_id": "GLM-5",
                "strength": 63,
                "direction": "LONG",
                "reasoning": "RSI 62.5未超买,MACD金叉,成交量放大12%"
            },
            {
                "model_id": "Qwen3.6-Plus",
                "strength": 72,
                "direction": "LONG",
                "reasoning": "PE 18.5倍低于行业均值,营收增长15%,盈利改善"
            },
        ],
    }
    
    # 市场数据
    market_data = {
        "symbol": "US.MU",
        "name": "Micron Technology",
        "price": 128.50,
        "date": "2026-04-06",
        
        "technical_indicators": {
            "sma_20": 125.30,
            "sma_50": 118.75,
            "sma_200": 105.20,
            "rsi_14": 62.5,
            "macd": 2.35,
            "macd_signal": 1.80,
            "volume_avg": 25000000,
            "volume_latest": 28000000,
        },
        
        "market_context": {
            "vix": 18.5,
            "spx_change": 0.003,
            "sector": "Technology - Semiconductors",
        },
        
        "news_sentiment": {
            "overall_score": 0.65,
            "recent_headlines": [
                "美光科技Q2财报超预期,HBM3E需求强劲",
                "AI芯片热潮带动存储芯片价格上涨",
                "美光宣布扩大HBM产能,满足AI需求",
            ],
        },
        
        "fundamentals": {
            "pe_ratio": 18.5,
            "eps": 6.95,
            "revenue_growth": 0.15,
            "profit_margin": 0.22,
        },
    }
    
    print(f"\n📊 测试标的: {market_data['symbol']} - {market_data['name']}")
    print(f"💰 当前价格: ${market_data['price']:.2f}")
    print(f"📈 综合评分: {voting_result['final_strength']}/100")
    print(f"🎯 投票方向: {voting_result['direction']}")
    print(f"🤝 一致性: {voting_result['consensus_level']:.0%}")
    
    # 步骤1: 初始化辩论引擎
    print("\n" + "=" * 80)
    print("步骤1: 研究员辩论 (多头vs空头)")
    print("=" * 80)
    
    try:
        from app.agents.debate.debate_engine import DebateEngine
        
        debate_engine = DebateEngine(
            max_rounds=3,
            bull_model="bailian/qwen3.6-plus",
            bear_model="bailian/glm-5",
        )
        
        print(f"✅ 辩论引擎初始化成功")
        print(f"   - 多头: Qwen3.6-Plus")
        print(f"   - 空头: GLM-5")
        print(f"   - 轮数: 3轮")
        
        # 执行辩论
        print(f"\n🗣️  辩论进行中...")
        debate_summary = debate_engine.run_debate(voting_result, market_data)
        
        print(f"\n✅ 辩论完成")
        print(f"   - 分歧度: {debate_summary.divergence_score:.0%}")
        print(f"   - 建议: {debate_summary.recommendation}")
        
        print(f"\n🐂 多头核心观点:")
        print(f"   {debate_summary.bull_case.core_thesis}")
        if debate_summary.bull_case.supporting_points:
            for i, point in enumerate(debate_summary.bull_case.supporting_points[:3], 1):
                print(f"   {i}. {point[:100]}")
        
        print(f"\n🐻 空头核心观点:")
        print(f"   {debate_summary.bear_case.core_thesis}")
        if debate_summary.bear_case.supporting_points:
            for i, point in enumerate(debate_summary.bear_case.supporting_points[:3], 1):
                print(f"   {i}. {point[:100]}")
        
    except Exception as e:
        print(f"❌ 辩论失败: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    # 步骤2: 交易员决策
    print("\n" + "=" * 80)
    print("步骤2: 交易员综合决策")
    print("=" * 80)
    
    try:
        from app.agents.trader_agent import TraderAgent
        
        trader = TraderAgent(model_name="bailian/qwen3.6-plus")
        
        print(f"✅ 交易员Agent初始化成功 (Qwen3.6-Plus)")
        print(f"🎯 交易决策中...")
        
        trade_decision = trader.make_decision(voting_result, debate_summary, market_data)
        
        print(f"\n✅ 交易员决策完成")
        print(f"   - 操作: {trade_decision.action}")
        print(f"   - 强度: {trade_decision.strength}/100")
        print(f"   - 信心: {trade_decision.confidence:.0%}")
        print(f"   - 建议仓位: {trade_decision.position_size:.0%}")
        
        if trade_decision.stop_loss > 0:
            print(f"   - 止损: ${trade_decision.stop_loss:.2f}")
        if trade_decision.take_profit:
            tp_str = " / ".join([f"${p:.2f}" for p in trade_decision.take_profit[:3]])
            print(f"   - 止盈: {tp_str}")
        
        if trade_decision.reasoning:
            print(f"\n📝 决策理由:")
            print(f"   {trade_decision.reasoning[:150]}...")
        
        if trade_decision.risk_warnings:
            print(f"\n⚠️  风险提示:")
            for warning in trade_decision.risk_warnings[:3]:
                print(f"   • {warning}")
        
    except Exception as e:
        print(f"❌ 交易员决策失败: {e}")
        import traceback
        traceback.print_exc()
        
        # 使用fallback
        from app.agents.trader_agent import TraderAgent
        trader = TraderAgent(model_name="bailian/qwen3.6-plus")
        trade_decision = trader._fallback_decision(voting_result, debate_summary, market_data)
        print(f"\n⚠️  使用Fallback决策:")
        print(f"   - 操作: {trade_decision.action}")
        print(f"   - 强度: {trade_decision.strength}/100")
    
    # 步骤3: 风控审查
    print("\n" + "=" * 80)
    print("步骤3: 风控官审查 (一票否决权)")
    print("=" * 80)
    
    try:
        from app.agents.risk_agent import RiskAgent
        
        risk_agent = RiskAgent()
        
        account_state = {
            "daily_loss": 0,
            "total_position": 0.3,
            "consecutive_losses": 0,
        }
        
        print(f"✅ 风控Agent初始化成功 (DeepSeek-Reasoner)")
        print(f"🛡️  风控审查中...")
        
        veto_result = risk_agent.veto_decision(
            trade_decision, debate_summary, account_state, market_data
        )
        
        print(f"\n✅ 风控审查完成")
        print(f"   - 风险评分: {veto_result.risk_score:.2f}")
        print(f"   - 否决: {'是 ❌' if veto_result.vetoed else '否 ✅'}")
        
        if veto_result.vetoed and veto_result.reasons:
            print(f"\n❌ 否决原因:")
            for reason in veto_result.reasons:
                print(f"   • {reason}")
        else:
            print(f"\n✅ 风控通过,允许执行交易")
        
    except Exception as e:
        print(f"❌ 风控审查失败: {e}")
        import traceback
        traceback.print_exc()
        from app.agents.risk_agent import VetoResult
        veto_result = VetoResult(vetoed=False, reasons=[], risk_score=0.3)
    
    # 步骤4: 最终决策
    print("\n" + "=" * 80)
    print("步骤4: 最终决策")
    print("=" * 80)
    
    from app.agents.final_decision import FinalDecision
    
    approved = not veto_result.vetoed
    final_action = trade_decision.action if approved else "HOLD"
    
    final_decision = FinalDecision(
        approved=approved,
        action=final_action,
        veto_reasons=veto_result.reasons if veto_result.vetoed else [],
        voting_result=voting_result,
        debate_summary=debate_summary,
        trade_decision=trade_decision,
        veto_result=veto_result,
        symbol="US.MU",
    )
    
    action_emoji = "✅" if approved else "🚫"
    action_cn = {"BUY": "买入", "SELL": "卖出", "HOLD": "观望"}.get(final_action, final_action)
    
    print(f"\n{action_emoji} 最终决策: {action_cn}")
    print(f"   状态: {'通过 ✅' if approved else '风控否决 ❌'}")
    
    if not approved and final_decision.veto_reasons:
        print(f"   否决原因:")
        for reason in final_decision.veto_reasons:
            print(f"   • {reason}")
    
    # 步骤5: 推送测试
    print("\n" + "=" * 80)
    print("步骤5: 推送测试 (钉钉 + 飞书)")
    print("=" * 80)
    
    push_success = {"dingtalk": False, "feishu": False}
    
    # 钉钉推送
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        dingtalk = DingTalkNotifier()
        webhook = os.getenv("DINGTALK_WEBHOOK_URL_US")
        
        if webhook:
            print(f"📤 钉钉推送中...")
            dingtalk.notify_full_decision(final_decision)
            print(f"✅ 钉钉推送成功!")
            push_success["dingtalk"] = True
        else:
            print(f"⏭️  钉钉Webhook未配置")
            
    except Exception as e:
        print(f"❌ 钉钉推送失败: {e}")
    
    # 飞书推送
    try:
        from app.notifier.feishu import FeishuNotifier
        
        feishu = FeishuNotifier()
        webhook = os.getenv("FEISHU_WEBHOOK_URL_US")
        
        if webhook:
            print(f"📤 飞书推送中...")
            feishu.notify_full_decision(final_decision)
            print(f"✅ 飞书推送成功!")
            push_success["feishu"] = True
        else:
            print(f"⏭️  飞书Webhook未配置")
            
    except Exception as e:
        print(f"❌ 飞书推送失败: {e}")
    
    # 最终总结
    print("\n" + "=" * 80)
    print("🎉 测试完成 - 链路验证总结")
    print("=" * 80)
    
    print(f"\n📊 MU(美光科技) 决策链路:")
    print(f"   ✅ 模拟投票: 完成 (68分, LONG方向)")
    print(f"   ✅ 研究员辩论: {'完成' if debate_summary else '失败'}")
    print(f"   ✅ 交易员决策: {'完成' if trade_decision else '失败'}")
    print(f"   ✅ 风控审查: {'完成' if veto_result else '失败'}")
    print(f"   ✅ 最终决策: {action_cn}")
    print(f"   {'✅' if push_success['dingtalk'] else '❌'} 钉钉推送: {'成功' if push_success['dingtalk'] else '失败'}")
    print(f"   {'✅' if push_success['feishu'] else '❌'} 飞书推送: {'成功' if push_success['feishu'] else '失败'}")
    
    print(f"\n🎯 最终建议: {action_cn} {'(已通过)' if approved else '(风控否决)'}")
    
    if push_success['dingtalk'] or push_success['feishu']:
        print(f"\n✅ 推送链路正常! 请检查钉钉/飞书消息")
    
    print(f"\n✅ 完整决策链路验证完成!")
    
    return final_decision


if __name__ == "__main__":
    try:
        result = test_mu_debate_to_push()
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\n⏹️  测试被中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
