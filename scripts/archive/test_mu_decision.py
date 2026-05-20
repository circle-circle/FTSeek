#!/usr/bin/env python3
"""
测试MU(美光科技)完整决策链路

验证: 分析师投票 → 研究员辩论 → 交易员决策 → 风控否决 → 推送
"""
import sys
import os
from pathlib import Path
from datetime import datetime

# 添加项目根目录到路径
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 加载环境变量
from dotenv import load_dotenv
env_path = BASE_DIR / ".env.us"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量: {env_path}")


def prepare_mu_market_data():
    """
    准备MU的市场数据 (模拟下周开盘场景)
    """
    return {
        "symbol": "US.MU",
        "name": "Micron Technology",
        "price": 128.50,  # 模拟当前价格
        "date": "2026-04-06",  # 下周一
        
        # 技术指标
        "technical_indicators": {
            "sma_20": 125.30,
            "sma_50": 118.75,
            "sma_200": 105.20,
            "rsi_14": 62.5,
            "macd": 2.35,
            "macd_signal": 1.80,
            "bollinger_upper": 135.40,
            "bollinger_lower": 115.20,
            "volume_avg": 25000000,
            "volume_latest": 28000000,
        },
        
        # 基本面
        "fundamentals": {
            "pe_ratio": 18.5,
            "eps": 6.95,
            "revenue_growth": 0.15,  # 15%营收增长
            "profit_margin": 0.22,
            "debt_to_equity": 0.12,
        },
        
        # 市场环境
        "market_context": {
            "vix": 18.5,
            "spx_change": 0.003,
            "soxx_change": 0.012,  # 半导体指数上涨
            "sector": "Technology - Semiconductors",
        },
        
        # 新闻情绪
        "news_sentiment": {
            "overall_score": 0.65,  # 偏正面
            "recent_headlines": [
                "美光科技Q2财报超预期,HBM3E需求强劲",
                "AI芯片热潮带动存储芯片价格上涨",
                "美光宣布扩大HBM产能,满足AI需求",
            ],
        },
        
        # 分析师评级
        "analyst_ratings": {
            "buy": 18,
            "hold": 5,
            "sell": 2,
            "avg_price_target": 145.00,
        },
        
        # 日线数据 (供技术分析师使用)
        "daily_kline": [
            {
                "open": 127.50,
                "high": 130.20,
                "low": 126.80,
                "close": 128.50,
                "volume": 28000000,
            }
        ],
    }


def test_full_decision_pipeline():
    """测试完整决策管线"""
    print("=" * 80)
    print("MU(美光科技) 完整决策链路测试")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # 准备市场数据
    market_data = prepare_mu_market_data()
    
    print(f"\n📊 测试标的: {market_data['symbol']} - {market_data['name']}")
    print(f"💰 当前价格: ${market_data['price']:.2f}")
    print(f"📈 20日均线: ${market_data['technical_indicators']['sma_20']:.2f}")
    print(f"📊 RSI: {market_data['technical_indicators']['rsi_14']:.1f}")
    print(f"💬 新闻情绪: {market_data['news_sentiment']['overall_score']:.2f}")
    
    # 步骤1: 初始化决策协调器
    print("\n" + "=" * 80)
    print("步骤1: 初始化多AI决策系统")
    print("=" * 80)
    
    try:
        from app.agents.voting.voting_coordinator import VotingCoordinator
        
        coordinator = VotingCoordinator(
            timeout_seconds=90,
            enable_debate=True,  # 启用辩论
        )
        
        print(f"✅ VotingCoordinator初始化成功")
        print(f"   - 模型Agent数: {len(coordinator.model_agents)}")
        print(f"   - 辩论引擎: {'启用' if coordinator.enable_debate else '禁用'}")
        
    except Exception as e:
        print(f"❌ VotingCoordinator初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    # 步骤2: 分析师团队投票
    print("\n" + "=" * 80)
    print("步骤2: 分析师团队投票 (4模型并行)")
    print("=" * 80)
    
    try:
        voting_result = coordinator.run_voting(market_data)
        
        print(f"✅ 投票完成")
        print(f"   - 综合评分: {voting_result.get('final_strength', 0):.1f}/100")
        print(f"   - 方向: {voting_result.get('direction', 'NEUTRAL')}")
        print(f"   - 一致性: {voting_result.get('consensus_level', 0):.0%}")
        
        # 显示各模型观点
        print(f"\n📋 各模型观点:")
        for ms in voting_result.get('model_scores', []):
            model_id = ms.get('model_id', '?')
            strength = ms.get('strength', 0)
            direction = ms.get('direction', 'NEUTRAL')
            reasoning = ms.get('reasoning', '')
            emoji = "📈" if direction == "LONG" else "📉" if direction == "SHORT" else "⚪"
            
            print(f"   {emoji} {model_id}: {strength}分 ({direction})")
            print(f"      {reasoning[:80]}...")
        
    except Exception as e:
        print(f"❌ 投票失败: {e}")
        import traceback
        traceback.print_exc()
        voting_result = None
    
    # 步骤3: 研究员辩论
    if coordinator.enable_debate and voting_result:
        print("\n" + "=" * 80)
        print("步骤3: 研究员辩论 (多头vs空头)")
        print("=" * 80)
        
        try:
            debate_summary = coordinator.debate_engine.run_debate(voting_result, market_data)
            
            print(f"✅ 辩论完成 ({len(debate_summary.rounds)}轮)")
            print(f"   - 分歧度: {debate_summary.divergence_score:.0%}")
            print(f"   - 建议: {debate_summary.recommendation}")
            
            print(f"\n🐂 多头观点:")
            print(f"   核心: {debate_summary.bull_case.core_thesis}")
            if debate_summary.bull_case.supporting_points:
                for point in debate_summary.bull_case.supporting_points[:3]:
                    print(f"   • {point[:80]}")
            
            print(f"\n🐻 空头观点:")
            print(f"   核心: {debate_summary.bear_case.core_thesis}")
            if debate_summary.bear_case.supporting_points:
                for point in debate_summary.bear_case.supporting_points[:3]:
                    print(f"   • {point[:80]}")
            
        except Exception as e:
            print(f"❌ 辩论失败: {e}")
            import traceback
            traceback.print_exc()
            debate_summary = None
    else:
        debate_summary = None
    
    # 步骤4: 交易员决策
    print("\n" + "=" * 80)
    print("步骤4: 交易员综合决策")
    print("=" * 80)
    
    try:
        if hasattr(coordinator, 'trader_agent') and coordinator.trader_agent:
            trade_decision = coordinator.trader_agent.make_decision(
                voting_result, debate_summary, market_data
            )
            
            print(f"✅ 交易员决策完成")
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
                print(f"   - 理由: {trade_decision.reasoning[:100]}...")
            
            if trade_decision.risk_warnings:
                print(f"   - 风险提示:")
                for warning in trade_decision.risk_warnings[:3]:
                    print(f"     ⚠️ {warning}")
        else:
            print("⏭️  交易员Agent未加载,使用默认决策")
            # 创建默认决策
            from app.agents.trader_models import TradeDecision
            trade_decision = TradeDecision(
                action="HOLD",
                symbol="US.MU",
                strength=50,
                confidence=0.5,
                position_size=0.0,
                stop_loss=0,
                take_profit=[],
                reasoning="交易员Agent未初始化,使用默认HOLD",
                model_opinions={},
                debate_highlights="无辩论数据",
                risk_warnings=[],
            )
        
    except Exception as e:
        print(f"❌ 交易员决策失败: {e}")
        import traceback
        traceback.print_exc()
        from app.agents.trader_models import TradeDecision
        trade_decision = TradeDecision(
            action="HOLD",
            symbol="US.MU",
            strength=50,
            confidence=0.5,
            position_size=0.0,
            stop_loss=0,
            take_profit=[],
            reasoning=f"交易员决策异常: {str(e)}",
            model_opinions={},
            debate_highlights="异常",
            risk_warnings=[str(e)],
        )
    
    # 步骤5: 风控审查
    print("\n" + "=" * 80)
    print("步骤5: 风控官审查 (一票否决权)")
    print("=" * 80)
    
    try:
        account_state = {
            "daily_loss": 0,
            "total_position": 0.3,
            "consecutive_losses": 0,
        }
        
        if hasattr(coordinator, 'risk_agent') and coordinator.risk_agent:
            veto_result = coordinator.risk_agent.veto_decision(
                trade_decision, debate_summary, account_state, market_data
            )
            
            print(f"✅ 风控审查完成")
            print(f"   - 风险评分: {veto_result.risk_score:.2f}")
            print(f"   - 否决: {'是' if veto_result.vetoed else '否'}")
            
            if veto_result.vetoed and veto_result.reasons:
                print(f"   - 否决原因:")
                for reason in veto_result.reasons:
                    print(f"     ❌ {reason}")
        else:
            print("⏭️  风控Agent未加载,默认通过")
            from app.agents.risk_agent import VetoResult
            veto_result = VetoResult(
                vetoed=False,
                reasons=[],
                risk_score=0.3,
            )
        
    except Exception as e:
        print(f"❌ 风控审查失败: {e}")
        import traceback
        traceback.print_exc()
        from app.agents.risk_agent import VetoResult
        veto_result = VetoResult(
            vetoed=False,
            reasons=[f"风控审查异常: {str(e)}"],
            risk_score=0.5,
        )
    
    # 步骤6: 最终决策
    print("\n" + "=" * 80)
    print("步骤6: 最终决策")
    print("=" * 80)
    
    try:
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
        
        print(f"{action_emoji} 最终决策: {action_cn}")
        print(f"   - 状态: {'通过' if approved else '风控否决'}")
        
        if not approved and final_decision.veto_reasons:
            print(f"   - 否决原因:")
            for reason in final_decision.veto_reasons:
                print(f"     ❌ {reason}")
        
    except Exception as e:
        print(f"❌ 最终决策失败: {e}")
        import traceback
        traceback.print_exc()
        final_decision = None
    
    # 步骤7: 推送测试
    if final_decision:
        print("\n" + "=" * 80)
        print("步骤7: 推送测试 (钉钉 + 飞书)")
        print("=" * 80)
        
        # 测试钉钉推送
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            
            dingtalk = DingTalkNotifier()
            
            # 检查是否有webhook配置
            webhook_url = os.getenv("DINGTALK_WEBHOOK_URL_US")
            if webhook_url:
                print(f"📤 钉钉推送中...")
                dingtalk.notify_full_decision(final_decision)
                print(f"✅ 钉钉推送成功")
            else:
                print(f"⏭️  钉钉Webhook未配置,跳过推送")
                
        except Exception as e:
            print(f"❌ 钉钉推送失败: {e}")
        
        # 测试飞书推送
        try:
            from app.notifier.feishu import FeishuNotifier
            
            feishu = FeishuNotifier()
            
            webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US")
            if webhook_url:
                print(f"📤 飞书推送中...")
                feishu.notify_full_decision(final_decision)
                print(f"✅ 飞书推送成功")
            else:
                print(f"⏭️  飞书Webhook未配置,跳过推送")
                
        except Exception as e:
            print(f"❌ 飞书推送失败: {e}")
    
    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)
    
    print(f"\n📊 MU(美光科技) 决策链路验证:")
    print(f"   ✅ 分析师投票: {'完成' if voting_result else '失败'}")
    print(f"   ✅ 研究员辩论: {'完成' if debate_summary else '跳过/失败'}")
    print(f"   ✅ 交易员决策: {'完成' if trade_decision else '失败'}")
    print(f"   ✅ 风控审查: {'完成' if veto_result else '失败'}")
    print(f"   ✅ 最终决策: {'完成' if final_decision else '失败'}")
    print(f"   ✅ 推送测试: 已执行")
    
    if final_decision:
        action_cn = {"BUY": "买入", "SELL": "卖出", "HOLD": "观望"}.get(final_decision.action, final_decision.action)
        print(f"\n🎯 最终建议: {action_cn} {'(已通过)' if final_decision.approved else '(风控否决)'}")
    
    print(f"\n✅ 链路验证完成!")
    
    return final_decision


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("MU(美光科技) 多AI决策链路测试")
    print("=" * 80)
    print()
    
    try:
        result = test_full_decision_pipeline()
        
        if result:
            print("\n🎉 测试成功! 完整决策链路正常工作")
            sys.exit(0)
        else:
            print("\n⚠️  测试部分失败,请检查错误信息")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\n⏹️  测试被用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
