#!/usr/bin/env python3
"""
MU真实数据AI决策测试

使用真实报价和技术指标进行AI决策和多空辩论
"""
import sys
from pathlib import Path
import os
import json
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")


def get_real_market_data(symbol: str = "US.MU"):
    """获取真实市场数据"""
    from app.data.market_data_router import MarketDataRouter
    from app.data.news_provider import USNewsProvider
    
    td_key = os.getenv("TWELVEDATA_API_KEY", "")
    fh_key = os.getenv("FINNHUB_API_KEY", "")
    
    print("步骤1: 初始化数据源")
    print("-" * 80)
    
    router = MarketDataRouter(
        twelvedata_api_key=td_key,
        finnhub_api_key=fh_key,
        cache_ttl_seconds=60,
    )
    
    print("\n步骤2: 获取真实报价")
    print("-" * 80)
    
    # 报价
    quote = router.get_quote(symbol)
    if not quote or quote.get('price', 0) == 0:
        print(f"❌ 无法获取{symbol}报价")
        return None
    
    price = quote['price']
    change_pct = quote.get('change_percent', 0)
    volume = quote.get('volume', 0)
    
    print(f"✅ {symbol} 真实报价: ${price:.2f} ({change_pct:+.2%})")
    print(f"   成交量: {volume:,}")
    
    print("\n步骤3: 获取技术指标 (TwelveData)")
    print("-" * 80)
    
    try:
        from app.data.twelvedata_provider import TwelveDataProvider
        td = TwelveDataProvider(api_key=td_key, cache_ttl_seconds=60)
        
        # 获取历史数据和技术指标
        ts = td.get_time_series(symbol)
        
        tech_indicators = {}
        if ts:
            tech_indicators = {
                'sma_20': ts.get('ma20', 0),
                'sma_50': ts.get('ma50', 0),
                'sma_200': ts.get('ma200', 0),
                'rsi_14': ts.get('rsi_14', 0),
                'macd': ts.get('macd', 0),
                'macd_signal': ts.get('macd_signal', 0),
                'atr_14': ts.get('atr_14', 0),
                'bollinger_upper': ts.get('bollinger_upper', 0),
                'bollinger_lower': ts.get('bollinger_lower', 0),
            }
            
            print(f"✅ 技术指标:")
            print(f"   SMA20:  ${tech_indicators['sma_20']:.2f}")
            print(f"   SMA50:  ${tech_indicators['sma_50']:.2f}")
            print(f"   SMA200: ${tech_indicators['sma_200']:.2f}")
            print(f"   RSI14:  {tech_indicators['rsi_14']:.1f}")
            print(f"   MACD:   {tech_indicators['macd']:.2f}")
            print(f"   ATR14:  ${tech_indicators['atr_14']:.2f}")
            
            # 判断价格位置
            sma20 = tech_indicators['sma_20']
            if sma20 > 0:
                if price > sma20:
                    print(f"   ✅ 价格在MA20上方 (+{(price-sma20)/sma20*100:.1f}%)")
                else:
                    print(f"   ❌ 价格在MA20下方 (-{(sma20-price)/sma20*100:.1f}%)")
        else:
            print(f"⚠️  技术指标获取失败,使用默认值")
            
    except Exception as e:
        print(f"❌ 技术指标获取异常: {e}")
        tech_indicators = {}
    
    print("\n步骤4: 获取新闻情绪")
    print("-" * 80)
    
    try:
        news_provider = USNewsProvider()
        
        # RSS新闻
        news_titles = news_provider.fetch_recent_news(symbol, hours=24)
        
        if news_titles:
            print(f"✅ RSS新闻: {len(news_titles)} 条")
            print(f"   最新3条:")
            for i, title in enumerate(news_titles[:3], 1):
                print(f"   {i}. {title[:80]}")
        else:
            print(f"⚠️  RSS新闻: 0 条")
        
        # 情绪打分
        sentiment = news_provider.get_sentiment_score(symbol)
        print(f"{'✅' if sentiment != 0 else '⚠️ '} 情绪打分: {sentiment:+.2f}")
        
    except Exception as e:
        print(f"❌ 新闻获取异常: {e}")
        news_titles = []
        sentiment = 0.0
    
    print("\n步骤5: 获取市场环境 (VIX/SPY)")
    print("-" * 80)
    
    try:
        vix = router.get_vix()
        print(f"✅ VIX: {vix:.1f}")
        
        spy_ctx = router.get_spy_context()
        if spy_ctx:
            print(f"✅ SPY: ${spy_ctx.get('spy_price', 0):.2f}")
            print(f"   涨跌: {spy_ctx.get('spx_change_percent', 0):+.2%}")
    except Exception as e:
        print(f"❌ 市场环境获取异常: {e}")
        vix = 20.0
        spy_ctx = {}
    
    # 组装完整市场数据
    market_data = {
        "symbol": symbol,
        "price": price,
        "change_percent": change_pct,
        "volume": volume,
        "technical_indicators": tech_indicators,
        "news_sentiment": sentiment,
        "news_headlines": news_titles[:5] if news_titles else [],
        "vix": vix,
        "spy_context": spy_ctx,
        "timestamp": datetime.now().isoformat(),
    }
    
    return market_data


def run_ai_decision_with_real_data(market_data: dict):
    """使用真实数据运行AI决策"""
    from app.ai.multi_agent_voting import MultiAgentVotingCoordinator
    from app.agents.debate.debate_engine import DebateEngine
    from app.agents.trader_agent import TraderAgent
    from app.agents.risk_agent import RiskGatekeeper
    from app.models.decision_models import MarketData, DebateSummary, FinalDecision
    
    symbol = market_data["symbol"]
    price = market_data["price"]
    
    print("\n" + "=" * 80)
    print(f"AI决策系统 - 使用真实数据")
    print(f"标的: {symbol} | 价格: ${price:.2f}")
    print("=" * 80)
    
    # 1. 多AI投票
    print("\n📊 步骤1: 4模型投票决策")
    print("-" * 80)
    
    try:
        voting_coordinator = MultiAgentVotingCoordinator()
        
        voting_result = voting_coordinator.run_voting(
            symbol=symbol,
            market_data=market_data,
        )
        
        if voting_result:
            print(f"✅ 投票完成:")
            print(f"   综合得分: {voting_result.strength}/100")
            print(f"   置信度: {voting_result.confidence:.0%}")
            print(f"   建议: {voting_result.recommendation}")
            print(f"\n   各模型意见:")
            for model, opinion in voting_result.model_opinions.items():
                print(f"   - {model}: {opinion[:80]}...")
        else:
            print(f"⚠️  投票失败,使用默认结果")
            voting_result = None
            
    except Exception as e:
        print(f"❌ 投票异常: {e}")
        import traceback
        traceback.print_exc()
        voting_result = None
    
    # 2. 多空辩论
    print("\n🎭 步骤2: 研究员多空辩论")
    print("-" * 80)
    
    try:
        debate_engine = DebateEngine()
        
        debate_summary = debate_engine.run_debate(
            voting_result=voting_result,
            market_data=market_data,
            max_rounds=2,  # 简化为2轮
        )
        
        if debate_summary:
            print(f"✅ 辩论完成:")
            print(f"   推荐: {debate_summary.recommendation}")
            print(f"   分歧度: {debate_summary.divergence_score:.0%}")
            print(f"\n   多头观点:")
            print(f"   {debate_summary.bull_case[:150]}...")
            print(f"\n   空头观点:")
            print(f"   {debate_summary.bear_case[:150]}...")
            print(f"\n   共识点:")
            for point in debate_summary.consensus_points[:3]:
                print(f"   - {point}")
        else:
            print(f"⚠️  辩论失败,使用默认结果")
            debate_summary = None
            
    except Exception as e:
        print(f"❌ 辩论异常: {e}")
        import traceback
        traceback.print_exc()
        debate_summary = None
    
    # 3. 交易员决策
    print("\n💼 步骤3: 交易员最终决策")
    print("-" * 80)
    
    try:
        trader = TraderAgent()
        
        trade_decision = trader.make_decision(
            voting_result=voting_result,
            debate_summary=debate_summary,
            market_data=market_data,
        )
        
        print(f"✅ 交易决策:")
        print(f"   动作: {trade_decision.action}")
        print(f"   强度: {trade_decision.strength}/100")
        print(f"   置信度: {trade_decision.confidence:.0%}")
        print(f"   仓位: {trade_decision.position_size:.0%}")
        
        if trade_decision.action == "BUY":
            print(f"\n   止损止盈:")
            print(f"   - 止损: ${trade_decision.stop_loss:.2f} (-{(price-trade_decision.stop_loss)/price*100:.1f}%)")
            if trade_decision.take_profit:
                for i, tp in enumerate(trade_decision.take_profit, 1):
                    print(f"   - 止盈{i}: ${tp:.2f} (+{(tp-price)/price*100:.1f}%)")
        
        print(f"\n   决策理由:")
        print(f"   {trade_decision.reasoning[:200]}...")
        
    except Exception as e:
        print(f"❌ 交易决策异常: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    # 4. 风控审查
    print("\n🛡️  步骤4: 风控一票否决")
    print("-" * 80)
    
    try:
        risk_gate = RiskGatekeeper()
        
        final_decision = risk_gate.review(
            trade_decision=trade_decision,
            market_data=market_data,
            voting_result=voting_result,
        )
        
        if final_decision.approved:
            print(f"✅ 风控通过")
            print(f"   最终动作: {final_decision.action}")
            print(f"   综合强度: {final_decision.strength}/100")
        else:
            print(f"❌ 风控否决")
            print(f"   否决原因:")
            for reason in final_decision.veto_reasons:
                print(f"   - {reason}")
        
    except Exception as e:
        print(f"❌ 风控审查异常: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    return {
        "market_data": market_data,
        "voting_result": voting_result,
        "debate_summary": debate_summary,
        "trade_decision": trade_decision,
        "final_decision": final_decision,
    }


def push_result_to_dingtalk(result: dict):
    """推送结果到钉钉"""
    from app.notifier.dingtalk import DingTalkNotifier
    
    print("\n📤 推送到钉钉")
    print("-" * 80)
    
    try:
        notifier = DingTalkNotifier()
        
        symbol = result["market_data"]["symbol"]
        price = result["market_data"]["price"]
        action = result["final_decision"].action
        strength = result["final_decision"].strength
        
        content = f"""
🤖 AI决策报告 - 使用真实数据

📊 标的: {symbol}
💰 价格: ${price:.2f}
🎯 决策: {action}
💪 强度: {strength}/100

📈 技术指标:
- SMA20: ${result['market_data']['technical_indicators'].get('sma_20', 0):.2f}
- RSI14: {result['market_data']['technical_indicators'].get('rsi_14', 0):.1f}
- MACD: {result['market_data']['technical_indicators'].get('macd', 0):.2f}

📰 新闻情绪: {result['market_data']['news_sentiment']:+.2f}
📊 VIX: {result['market_data']['vix']:.1f}

✅ 数据源: TwelveData + Finnhub (真实报价)
⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        
        success = notifier.send_text(content)
        
        if success:
            print(f"✅ 钉钉推送成功")
        else:
            print(f"⚠️  钉钉推送失败")
            
    except Exception as e:
        print(f"❌ 推送异常: {e}")


if __name__ == "__main__":
    try:
        print("=" * 80)
        print("MU真实数据AI决策完整测试")
        print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        
        # 获取真实数据
        market_data = get_real_market_data("US.MU")
        
        if not market_data:
            print("\n❌ 无法获取真实市场数据,测试终止")
            sys.exit(1)
        
        # 运行AI决策
        result = run_ai_decision_with_real_data(market_data)
        
        if not result:
            print("\n❌ AI决策失败,测试终止")
            sys.exit(1)
        
        # 推送到钉钉
        push_result_to_dingtalk(result)
        
        print("\n" + "=" * 80)
        print("✅ 测试完成!")
        print("=" * 80)
        
        sys.exit(0)
        
    except KeyboardInterrupt:
        print("\n\n⏹️  测试被中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
