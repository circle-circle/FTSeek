#!/usr/bin/env python3
"""
真实数据AI决策测试 (简化版 - 跳过LLM调用)

使用真实报价和技术指标,模拟完整决策流程
"""
import sys
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

from datetime import datetime


def test_real_data_decision():
    """使用真实数据进行决策测试"""
    print("=" * 80)
    print("MU真实数据AI决策测试 (简化版)")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    symbol = "US.MU"
    
    # 步骤1: 获取真实数据
    print("\n📊 步骤1: 获取真实市场数据")
    print("-" * 80)
    
    from app.data.market_data_router import MarketDataRouter
    from app.data.news_provider import USNewsProvider
    
    td_key = os.getenv("TWELVEDATA_API_KEY", "")
    fh_key = os.getenv("FINNHUB_API_KEY", "")
    
    router = MarketDataRouter(
        twelvedata_api_key=td_key,
        finnhub_api_key=fh_key,
        cache_ttl_seconds=60,
    )
    
    # 报价
    quote = router.get_quote(symbol)
    if not quote:
        print("❌ 无法获取报价")
        return False
    
    price = quote['price']
    change_pct = quote.get('change_percent', 0)
    volume = quote.get('volume', 0)
    
    print(f"✅ {symbol} 真实报价: ${price:.2f} ({change_pct:+.2%})")
    print(f"   成交量: {volume:,}")
    
    # 技术指标
    from app.data.twelvedata_provider import TwelveDataProvider
    td = TwelveDataProvider(api_key=td_key, cache_ttl_seconds=60)
    
    ts = td.get_time_series(symbol)
    
    tech = {}
    if ts:
        tech = {
            'sma_20': ts.get('ma20', 0),
            'sma_50': ts.get('ma50', 0),
            'atr_14': ts.get('atr_14', 0),
        }
        
        print(f"\n✅ 技术指标:")
        print(f"   MA20:  ${tech['sma_20']:.2f}")
        print(f"   MA50:  ${tech['sma_50']:.2f}")
        
        # 价格位置分析
        sma20 = tech['sma_20']
        if sma20 > 0:
            deviation = (price - sma20) / sma20 * 100
            if price > sma20:
                print(f"   ✅ 价格在MA20上方 (+{deviation:.1f}%)")
            else:
                print(f"   ⚠️  价格在MA20下方 ({deviation:.1f}%) - 短期弱势")
    else:
        print(f"\n⚠️  技术指标获取失败")
    
    # 新闻情绪 (简化 - 跳过RSS获取)
    print(f"\n⚪ 新闻情绪: 跳过RSS获取 (简化测试)")
    sentiment = 0.0  # 中性
    news_titles = []
    
    # 市场环境
    try:
        vix = router.get_vix()
        print(f"\n✅ VIX: {vix:.1f}")
    except:
        vix = 20.0
    
    # 步骤2: 构建市场数据
    print("\n📊 步骤2: 构建完整市场数据")
    print("-" * 80)
    
    market_data = {
        "symbol": symbol,
        "price": price,
        "change_percent": change_pct,
        "volume": volume,
        "technical_indicators": tech,
        "news_sentiment": sentiment,
        "news_headlines": news_titles[:5] if news_titles else [],
        "vix": vix,
        "timestamp": datetime.now().isoformat(),
    }
    
    print(f"✅ 市场数据构建完成:")
    print(f"   标的: {market_data['symbol']}")
    print(f"   价格: ${market_data['price']:.2f}")
    print(f"   技术指标: {len(tech)} 项")
    print(f"   新闻: {len(news_titles)} 条")
    print(f"   情绪: {sentiment:+.2f}")
    
    # 步骤3: 模拟AI投票 (基于规则)
    print("\n📊 步骤3: AI投票决策 (基于规则模拟)")
    print("-" * 80)
    
    # 基于技术指标的规则评分
    score = 50  # 基础分
    
    # MA趋势
    if tech.get('sma_20', 0) > 0:
        if price > tech['sma_20']:
            score += 10  # 价格在MA20上方
        else:
            score -= 15  # 价格在MA20下方
    
    if tech.get('sma_50', 0) > 0:
        if price > tech['sma_50']:
            score += 10
        else:
            score -= 10
    
    # 新闻情绪
    score += int(sentiment * 20)  # 情绪影响±20分
    
    # VIX
    if vix > 30:
        score -= 10  # 高波动
    elif vix < 15:
        score += 5  # 低波动
    
    # 成交量
    if volume > 40000000:
        score += 5  # 高成交量确认
    
    strength = max(0, min(100, score))
    confidence = 0.5 + abs(sentiment) * 0.3
    confidence = min(0.9, confidence)
    
    if strength >= 65:
        recommendation = "BUY"
    elif strength <= 35:
        recommendation = "SELL"
    else:
        recommendation = "HOLD"
    
    print(f"✅ 投票结果:")
    print(f"   强度: {strength}/100")
    print(f"   置信度: {confidence:.0%}")
    print(f"   建议: {recommendation}")
    print(f"\n   评分因素:")
    print(f"   - MA趋势: {'+' if price > tech.get('sma_20', 0) else '-'}{(abs(price - tech.get('sma_20', 0)) / tech.get('sma_20', 1) * 100):.1f}%")
    print(f"   - 新闻情绪: {sentiment:+.2f}")
    print(f"   - VIX: {vix:.1f}")
    
    # 步骤4: 模拟多空辩论
    print("\n🎭 步骤4: 多空辩论 (规则推导)")
    print("-" * 80)
    
    bull_points = []
    bear_points = []
    
    # 多头观点
    if sentiment > 0.2:
        bull_points.append(f"新闻情绪正面 ({sentiment:+.2f}),市场看好")
    if volume > 40000000:
        bull_points.append(f"成交量活跃 ({volume/1000000:.1f}M),资金关注")
    if vix < 20:
        bull_points.append(f"VIX较低 ({vix:.1f}),市场稳定")
    
    # 空头观点
    if price < tech.get('sma_20', 0):
        bear_points.append(f"价格低于MA20 ({(price-tech['sma_20'])/tech['sma_20']*100:.1f}%),短期弱势")
    if price < tech.get('sma_50', 0):
        bear_points.append(f"价格低于MA50 ({(price-tech['sma_50'])/tech['sma_50']*100:.1f}%),中期趋势向下")
    if sentiment < -0.2:
        bear_points.append(f"新闻情绪负面 ({sentiment:+.2f}),市场担忧")
    
    print(f"📈 多头观点 ({len(bull_points)}):")
    for point in bull_points:
        print(f"   ✅ {point}")
    
    print(f"\n📉 空头观点 ({len(bear_points)}):")
    for point in bear_points:
        print(f"   ❌ {point}")
    
    # 共识点
    consensus = []
    if abs(price - tech.get('sma_20', 0)) / tech.get('sma_20', 1) < 0.1:
        consensus.append("价格接近MA20,可能面临方向选择")
    if volume > 40000000:
        consensus.append("高成交量确认当前趋势")
    
    print(f"\n🤝 共识点 ({len(consensus)}):")
    for point in consensus:
        print(f"   • {point}")
    
    # 辩论推荐
    divergence = min(1.0, abs(len(bull_points) - len(bear_points)) / max(1, len(bull_points) + len(bear_points)))
    
    if len(bull_points) > len(bear_points):
        debate_recommendation = "WEAK_BULL"
    elif len(bear_points) > len(bull_points):
        debate_recommendation = "WEAK_BEAR"
    else:
        debate_recommendation = "NEUTRAL"
    
    print(f"\n✅ 辩论结论:")
    print(f"   推荐: {debate_recommendation}")
    print(f"   分歧度: {divergence:.0%}")
    
    # 步骤5: 交易决策
    print("\n💼 步骤5: 交易决策")
    print("-" * 80)
    
    action = recommendation
    position_size = 0.0
    
    if action == "BUY":
        position_size = 0.15 if strength >= 70 else 0.10
        stop_loss = price * 0.92  # -8%
        take_profit = [
            price * 1.15,  # +15%
            price * 1.25,  # +25%
            price * 1.40,  # +40%
        ]
        
        print(f"✅ 决策: {action}")
        print(f"   仓位: {position_size:.0%}")
        print(f"   止损: ${stop_loss:.2f} (-8%)")
        print(f"   止盈1: ${take_profit[0]:.2f} (+15%) - 平仓40%")
        print(f"   止盈2: ${take_profit[1]:.2f} (+25%) - 平仓35%")
        print(f"   止盈3: ${take_profit[2]:.2f} (+40%) - 平仓25%")
        
    elif action == "SELL":
        position_size = -0.10
        stop_loss = price * 1.08
        take_profit = [price * 0.85]
        
        print(f"✅ 决策: {action}")
        print(f"   仓位: {abs(position_size):.0%} (做空)")
        print(f"   止损: ${stop_loss:.2f} (+8%)")
        print(f"   止盈: ${take_profit[0]:.2f} (-15%)")
        
    else:
        stop_loss = 0
        take_profit = []
        print(f"✅ 决策: {action}")
        print(f"   原因: 强度{strength}分,未达到交易阈值")
    
    # 步骤6: 风控审查
    print("\n🛡️  步骤6: 风控审查")
    print("-" * 80)
    
    veto_reasons = []
    
    # 检查1: 分歧度
    if divergence > 0.7:
        veto_reasons.append(f"多空分歧度过高 ({divergence:.0%})")
    
    # 检查2: VIX
    if vix > 35:
        veto_reasons.append(f"VIX过高 ({vix:.1f}),市场极度不稳定")
    
    # 检查3: 强度
    if strength < 40 and action != "HOLD":
        veto_reasons.append(f"决策强度过低 ({strength}分)")
    
    if veto_reasons:
        approved = False
        print(f"❌ 风控否决")
        for reason in veto_reasons:
            print(f"   • {reason}")
    else:
        approved = True
        print(f"✅ 风控通过")
    
    # 步骤7: 最终决策
    print("\n" + "=" * 80)
    print("最终决策报告")
    print("=" * 80)
    
    print(f"""
📊 标的: {symbol}
💰 真实价格: ${price:.2f} ({change_pct:+.2%})
🎯 决策: {action}
✅ 风控: {'通过' if approved else '否决'}

📈 技术指标:
  MA20:  ${tech.get('sma_20', 0):.2f}
  MA50:  ${tech.get('sma_50', 0):.2f}
  价格位置: {(price/tech.get('sma_20', 1)-1)*100:.1f}% vs MA20

📰 新闻情绪: {sentiment:+.2f}
🌊 VIX: {vix:.1f}
💪 决策强度: {strength}/100

🎯 交易计划:
""")
    
    if action == "BUY" and approved:
        print(f"  买入价格: ${price:.2f}")
        print(f"  仓位大小: {position_size:.0%}")
        print(f"  止损价位: ${stop_loss:.2f} (-{(price-stop_loss)/price*100:.1f}%)")
        print(f"  止盈1: ${take_profit[0]:.2f} (+{(take_profit[0]-price)/price*100:.1f}%) - 40%")
        print(f"  止盈2: ${take_profit[1]:.2f} (+{(take_profit[1]-price)/price*100:.1f}%) - 35%")
        print(f"  止盈3: ${take_profit[2]:.2f} (+{(take_profit[2]-price)/price*100:.1f}%) - 25%")
        
        print(f"\n💡 风险评估:")
        print(f"  • 价格在MA20下方 {(price/tech.get('sma_20', 1)-1)*100:.1f}% - 短期弱势")
        print(f"  • 建议在价格回升至MA20上方后执行")
        print(f"  • 或等待更强信号 (当前强度{strength}分)")
    
    elif action == "HOLD":
        print(f"  当前不适合交易")
        print(f"  原因: 价格低于MA20/MA50,短期趋势向下")
        print(f"  建议: 等待价格突破MA20后再考虑买入")
    
    print(f"\n⏰ 决策时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📡 数据源: TwelveData + Finnhub (真实数据)")
    
    # 步骤8: 推送通知
    print("\n📤 步骤7: 推送通知到钉钉")
    print("-" * 80)
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        notifier = DingTalkNotifier()
        
        content = f"""
🤖 AI决策报告 - 真实数据验证

📊 标的: {symbol}
💰 价格: ${price:.2f} ({change_pct:+.2%})
🎯 决策: {action}
✅ 风控: {'通过' if approved else '否决'}

📈 技术指标:
- MA20: ${tech.get('sma_20', 0):.2f}
- MA50: ${tech.get('sma_50', 0):.2f}
- 位置: {(price/tech.get('sma_20', 1)-1)*100:.1f}% vs MA20

📰 情绪: {sentiment:+.2f} | VIX: {vix:.1f}
💪 强度: {strength}/100

✅ 数据源: TwelveData+Finnhub真实报价
⏰ 时间: {datetime.now().strftime('%H:%M')}
"""
        
        success = notifier.send_text(content)
        
        if success:
            print("✅ 钉钉推送成功")
        else:
            print("⚠️  钉钉推送失败")
            
    except Exception as e:
        print(f"❌ 推送异常: {e}")
    
    print("\n" + "=" * 80)
    print("✅ 测试完成!")
    print("=" * 80)
    print(f"\n📝 总结:")
    print(f"   真实价格: ${price:.2f} (非模拟$128.50)")
    print(f"   数据源: TwelveData + Finnhub (已验证)")
    print(f"   决策: {action} - {'建议执行' if approved and action != 'HOLD' else '观望等待'}")
    print(f"   完整链路: 数据获取 → AI投票 → 多空辩论 → 交易决策 → 风控审查 → 推送通知")
    
    return True


if __name__ == "__main__":
    try:
        success = test_real_data_decision()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⏹️  测试被中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
