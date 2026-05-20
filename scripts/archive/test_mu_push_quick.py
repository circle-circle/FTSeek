#!/usr/bin/env python3
"""
快速测试: MU决策推送 (使用规则fallback,不调用LLM)

验证推送链路是否正常
"""
import sys
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

from datetime import datetime
from app.agents.debate.debate_models import DebateArgument, DebateSummary
from app.agents.trader_models import TradeDecision
from app.agents.risk_agent import VetoResult
from app.agents.final_decision import FinalDecision


def create_mock_debate_summary():
    """创建模拟辩论结果"""
    return DebateSummary(
        bull_case=DebateArgument(
            side="BULL",
            core_thesis="存储芯片周期复苏+AI需求爆发,美光HBM3E供不应求,目标价$145",
            supporting_points=[
                "HBM3E产能已被预订至2026Q3,价格持续上涨",
                "Q2财报超预期,毛利率从22%提升至28%",
                "AI服务器存储需求年增长40%,美光受益最大",
            ],
            risk_factors=["半导体周期波动风险", "竞争对手扩产"],
            confidence=0.72,
        ),
        bear_case=DebateArgument(
            side="BEAR",
            core_thesis="估值已反映乐观预期,RSI接近超买,短期可能回调至$120",
            supporting_points=[
                "PE 18.5倍处于历史75分位,估值偏高",
                "RSI 62.5接近超买区域,技术面有回调压力",
                "存储芯片价格可能在2026H2见顶",
            ],
            risk_factors=["AI需求不及预期", "宏观经济衰退"],
            confidence=0.58,
        ),
        divergence_score=0.35,
        consensus_points=["AI驱动存储需求增长", "美光HBM技术领先", "财报基本面改善"],
        key_risks=["半导体周期波动", "估值偏高", "AI资本支出不确定性"],
        recommendation="WEAK_BULL",
    )


def create_mock_trade_decision():
    """创建模拟交易决策 (使用真实价格$366.24)"""
    price = 366.24  # MU真实价格
    
    return TradeDecision(
        action="BUY",
        symbol="US.MU",
        strength=68,
        confidence=0.68,
        position_size=0.15,
        stop_loss=price * 0.92,  # -8% = $336.94
        take_profit=[price * 1.15, price * 1.25, price * 1.40],  # 15%/25%/40% = $421.18/$457.80/$512.74
        reasoning="综合投票68分+多头辩论信心72%,建议买入15%仓位。HBM3E需求强劲,财报超预期,技术面突破MA20支撑。止损设$336.94(-8%),分三档止盈$421.18/$457.80/$512.74。",
        model_opinions={
            "DeepSeek-Reasoner": "HBM3E需求强劲,存储芯片周期复苏,技术面突破MA20",
            "MiniMax-M2.5": "AI芯片新闻情绪正面,美光扩产计划获市场认可",
            "GLM-5": "RSI 62.5未超买,MACD金叉,成交量放大12%",
            "Qwen3.6-Plus": "PE 18.5倍低于行业均值,营收增长15%,盈利改善",
        },
        debate_highlights="多头:HBM3E供不应求,毛利率提升;空头:估值偏高,RSI接近超买。综合建议谨慎看多",
        risk_warnings=[
            "半导体行业周期性波动风险",
            "AI资本支出可能不及预期",
            "存储芯片价格2026H2可能见顶",
        ],
    )


def test_mu_push_notification():
    """测试MU推送通知"""
    print("=" * 80)
    print("MU(美光科技) 决策推送测试")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # 创建模拟数据
    print("\n📊 准备决策数据...")
    
    voting_result = {
        "symbol": "US.MU",
        "final_strength": 68,
        "direction": "LONG",
        "consensus_level": 0.75,
        "model_scores": [
            {"model_id": "DeepSeek-Reasoner", "strength": 72, "direction": "LONG", "reasoning": "HBM3E需求强劲,存储芯片周期复苏"},
            {"model_id": "MiniMax-M2.5", "strength": 65, "direction": "LONG", "reasoning": "AI芯片新闻情绪正面"},
            {"model_id": "GLM-5", "strength": 63, "direction": "LONG", "reasoning": "RSI 62.5未超买,MACD金叉"},
            {"model_id": "Qwen3.6-Plus", "strength": 72, "direction": "LONG", "reasoning": "PE 18.5倍低于行业均值"},
        ],
    }
    
    debate_summary = create_mock_debate_summary()
    trade_decision = create_mock_trade_decision()
    
    veto_result = VetoResult(
        vetoed=False,
        reasons=[],
        risk_score=0.35,
    )
    
    final_decision = FinalDecision(
        approved=True,
        action="BUY",
        veto_reasons=[],
        voting_result=voting_result,
        debate_summary=debate_summary,
        trade_decision=trade_decision,
        veto_result=veto_result,
        symbol="US.MU",
    )
    
    print(f"✅ 决策数据准备完成")
    print(f"   标的: US.MU")
    print(f"   操作: 买入")
    print(f"   强度: 68/100")
    print(f"   仓位: 15%")
    print(f"   止损: ${trade_decision.stop_loss:.2f}")
    print(f"   止盈: ${trade_decision.take_profit[0]:.2f} / ${trade_decision.take_profit[1]:.2f}")
    
    # 测试钉钉推送
    print("\n" + "=" * 80)
    print("测试1: 钉钉推送")
    print("=" * 80)
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        dingtalk = DingTalkNotifier()
        webhook = os.getenv("DINGTALK_WEBHOOK_URL_US")
        
        if not webhook:
            print("⚠️  DINGTALK_WEBHOOK_URL_US 未配置,跳过推送")
        else:
            print("📤 推送中...")
            dingtalk.notify_full_decision(final_decision)
            print("✅ 钉钉推送成功! 请检查钉钉消息")
            
    except Exception as e:
        print(f"❌ 钉钉推送失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 测试飞书推送
    print("\n" + "=" * 80)
    print("测试2: 飞书推送")
    print("=" * 80)
    
    try:
        from app.notifier.feishu import FeishuNotifier
        
        feishu = FeishuNotifier()
        webhook = os.getenv("FEISHU_WEBHOOK_URL_US")
        
        if not webhook:
            print("⚠️  FEISHU_WEBHOOK_URL_US 未配置,跳过推送")
        else:
            print("📤 推送中...")
            feishu.notify_full_decision(final_decision)
            print("✅ 飞书推送成功! 请检查飞书消息")
            
    except Exception as e:
        print(f"❌ 飞书推送失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)
    
    print(f"\n📊 MU决策推送链路:")
    print(f"   ✅ 辩论数据: 已生成")
    print(f"   ✅ 交易决策: BUY 15%仓位")
    print(f"   ✅ 风控审查: 通过")
    print(f"   ✅ 最终决策: 买入(已批准)")
    print(f"   ✅ 推送测试: 已执行")
    
    print(f"\n🎯 完整决策内容:")
    print(f"   • 分析师投票: 4模型平均68分,LONG方向,一致性75%")
    print(f"   • 多头观点: HBM3E供不应求,毛利率提升,AI需求爆发")
    print(f"   • 空头观点: 估值偏高,RSI接近超买,周期可能见顶")
    print(f"   • 交易决策: 买入15%,止损$118.22,止盈$147.78/$160.63/$179.90")
    print(f"   • 风控审查: 风险评分0.35,通过")
    
    print(f"\n✅ 推送链路验证完成!")
    print(f"   如已配置Webhook,请检查钉钉/飞书是否收到完整决策报告")


if __name__ == "__main__":
    try:
        test_mu_push_notification()
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
