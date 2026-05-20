#!/usr/bin/env python3
"""
真实数据源验证测试

验证TwelveData和Finnhub的真实报价,确保AI决策使用准确数据
"""
import sys
from pathlib import Path
import os
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 加载环境变量
from dotenv import load_dotenv
env_path = BASE_DIR / ".env.us"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量: {env_path}")


def test_real_market_data():
    """测试真实市场数据"""
    print("=" * 80)
    print("真实数据源验证测试")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # 测试标的
    test_symbols = ["US.MU", "US.TSLA", "US.NVDA", "US.GOOG", "US.BABA"]
    
    # 检查API Key配置
    print("\n步骤1: 检查API Key配置")
    print("-" * 80)
    
    twelvedata_key = os.getenv("TWELVEDATA_API_KEY", "")
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    alphavantage_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
    
    print(f"TwelveData API Key: {'✅ 已配置' if twelvedata_key else '❌ 未配置'}")
    print(f"Finnhub API Key:    {'✅ 已配置' if finnhub_key else '❌ 未配置'}")
    print(f"Alpha Vantage Key:  {'✅ 已配置' if alphavantage_key else '❌ 未配置'}")
    
    if not twelvedata_key and not finnhub_key:
        print("\n❌ 错误: 至少需要配置TwelveData或Finnhub其中一个API Key")
        print("请编辑 .env.us 文件并添加API Key")
        return False
    
    # 初始化数据源
    print("\n步骤2: 初始化数据提供商")
    print("-" * 80)
    
    from app.data.twelvedata_provider import TwelveDataProvider
    from app.data.finnhub_provider import FinnhubProvider
    from app.data.alphavantage_provider import AlphaVantageProvider
    from app.data.market_data_router import MarketDataRouter
    
    providers = {}
    
    if twelvedata_key:
        try:
            td = TwelveDataProvider(api_key=twelvedata_key, cache_ttl_seconds=60)
            providers['twelvedata'] = td
            print(f"✅ TwelveData 初始化成功")
        except Exception as e:
            print(f"❌ TwelveData 初始化失败: {e}")
    
    if finnhub_key:
        try:
            fh = FinnhubProvider(api_key=finnhub_key, cache_ttl_seconds=60)
            providers['finnhub'] = fh
            print(f"✅ Finnhub 初始化成功")
        except Exception as e:
            print(f"❌ Finnhub 初始化失败: {e}")
    
    if alphavantage_key:
        try:
            av = AlphaVantageProvider(api_key=alphavantage_key, cache_ttl_seconds=60)
            providers['alphavantage'] = av
            print(f"✅ Alpha Vantage 初始化成功")
        except Exception as e:
            print(f"❌ Alpha Vantage 初始化失败: {e}")
    
    if not providers:
        print("\n❌ 错误: 没有成功初始化任何数据源")
        return False
    
    # 测试真实报价
    print("\n步骤3: 获取真实报价")
    print("-" * 80)
    
    results = {}
    
    for symbol in test_symbols:
        print(f"\n📊 {symbol}")
        symbol_results = {}
        
        # 测试TwelveData
        if 'twelvedata' in providers:
            try:
                td = providers['twelvedata']
                quote = td.get_quote(symbol)
                
                if quote and quote.get('price', 0) > 0:
                    print(f"  ✅ TwelveData: ${quote['price']:.2f}")
                    print(f"     涨跌: {quote.get('change_pct', 0):+.2%}")
                    print(f"     成交量: {quote.get('volume', 0):,}")
                    symbol_results['twelvedata'] = quote
                else:
                    print(f"  ⚠️  TwelveData: 无数据")
            except Exception as e:
                print(f"  ❌ TwelveData 失败: {e}")
        
        # 测试Finnhub
        if 'finnhub' in providers:
            try:
                fh = providers['finnhub']
                quote = fh.get_quote(symbol)
                
                if quote and quote.get('price', 0) > 0:
                    print(f"  ✅ Finnhub:    ${quote['price']:.2f}")
                    print(f"     涨跌: {quote.get('change_pct', 0):+.2%}")
                    print(f"     成交量: {quote.get('volume', 0):,}")
                    symbol_results['finnhub'] = quote
                else:
                    print(f"  ⚠️  Finnhub:    无数据")
            except Exception as e:
                print(f"  ❌ Finnhub 失败: {e}")
        
        # 测试Alpha Vantage
        if 'alphavantage' in providers:
            try:
                av = providers['alphavantage']
                quote = av.get_quote(symbol)
                
                if quote and quote.get('price', 0) > 0:
                    print(f"  ✅ AlphaVant:  ${quote['price']:.2f}")
                    symbol_results['alphavantage'] = quote
                else:
                    print(f"  ⚠️  AlphaVant:  无数据")
            except Exception as e:
                print(f"  ❌ Alpha Vantage 失败: {e}")
        
        results[symbol] = symbol_results
    
    # 对比验证
    print("\n" + "=" * 80)
    print("步骤4: 数据对比验证")
    print("=" * 80)
    
    for symbol, symbol_results in results.items():
        if len(symbol_results) >= 2:
            prices = {source: data['price'] for source, data in symbol_results.items()}
            
            print(f"\n📊 {symbol} 报价对比:")
            for source, price in prices.items():
                print(f"  {source:15s}: ${price:.2f}")
            
            # 检查价格差异
            price_list = list(prices.values())
            max_diff = max(price_list) - min(price_list)
            avg_price = sum(price_list) / len(price_list)
            diff_pct = (max_diff / avg_price) * 100
            
            if diff_pct < 1:
                print(f"  ✅ 价格差异: ${max_diff:.2f} ({diff_pct:.2f}%) - 正常")
            elif diff_pct < 5:
                print(f"  ⚠️  价格差异: ${max_diff:.2f} ({diff_pct:.2f}%) - 可接受")
            else:
                print(f"  ❌ 价格差异: ${max_diff:.2f} ({diff_pct:.2f}%) - 异常!")
    
    # 测试技术指标
    print("\n" + "=" * 80)
    print("步骤5: 测试技术指标 (TwelveData)")
    print("=" * 80)
    
    if 'twelvedata' in providers:
        td = providers['twelvedata']
        test_symbol = "US.MU"
        
        print(f"\n📊 {test_symbol} 技术指标:")
        
        try:
            # 获取完整市场上下文
            context = td.get_full_market_context(test_symbol)
            
            if context:
                price = context.get('price', 0)
                print(f"  当前价格: ${price:.2f}")
                
                tech = context.get('technical_indicators', {})
                if tech:
                    print(f"  技术指标:")
                    print(f"    SMA20:  ${tech.get('sma_20', 0):.2f}")
                    print(f"    SMA50:  ${tech.get('sma_50', 0):.2f}")
                    print(f"    SMA200: ${tech.get('sma_200', 0):.2f}")
                    print(f"    RSI14:  {tech.get('rsi_14', 0):.1f}")
                    print(f"    MACD:   {tech.get('macd', 0):.2f}")
                
                # 验证价格合理性
                sma20 = tech.get('sma_20', 0)
                if sma20 > 0:
                    deviation = abs(price - sma20) / sma20 * 100
                    if deviation < 20:
                        print(f"  ✅ 价格与SMA20偏离 {deviation:.1f}% - 合理")
                    else:
                        print(f"  ⚠️  价格与SMA20偏离 {deviation:.1f}% - 可能异常")
            else:
                print(f"  ❌ 无法获取市场上下文")
                
        except Exception as e:
            print(f"  ❌ 技术指标获取失败: {e}")
            import traceback
            traceback.print_exc()
    
    # 测试新闻情绪
    print("\n" + "=" * 80)
    print("步骤6: 测试新闻情绪 (RSS + Finnhub)")
    print("=" * 80)
    
    from app.data.news_provider import USNewsProvider
    
    test_symbol = "US.MU"
    print(f"\n📰 {test_symbol} 新闻情绪:")
    
    try:
        news_provider = USNewsProvider()
        
        # RSS新闻
        news_titles = news_provider.fetch_recent_news(test_symbol, hours=24)
        if news_titles:
            print(f"  ✅ RSS新闻: {len(news_titles)} 条")
            for i, title in enumerate(news_titles[:3], 1):
                print(f"    {i}. {title[:80]}...")
        else:
            print(f"  ⚠️  RSS新闻: 0 条")
        
        # Finnhub新闻
        if 'finnhub' in providers:
            fh = providers['finnhub']
            fh_news = fh.get_company_news(test_symbol)
            if fh_news:
                print(f"  ✅ Finnhub新闻: {len(fh_news)} 条")
            else:
                print(f"  ⚠️  Finnhub新闻: 0 条")
        
        # 情绪打分
        sentiment = news_provider.get_sentiment_score(test_symbol)
        if sentiment != 0:
            sentiment_emoji = "📈" if sentiment > 0 else "📉"
            print(f"  {sentiment_emoji} 情绪打分: {sentiment:+.2f}")
        else:
            print(f"  ⚪ 情绪打分: 0 (无数据)")
            
    except Exception as e:
        print(f"  ❌ 新闻情绪获取失败: {e}")
    
    # 测试MarketDataRouter
    print("\n" + "=" * 80)
    print("步骤7: 测试MarketDataRouter (统一路由)")
    print("=" * 80)
    
    try:
        router = MarketDataRouter(
            twelvedata_api_key=twelvedata_key if twelvedata_key else None,
            finnhub_api_key=finnhub_key if finnhub_key else None,
            alphavantage_api_key=alphavantage_key if alphavantage_key else None,
            cache_ttl_seconds=60,
        )
        
        test_symbol = "US.MU"
        print(f"\n📊 {test_symbol} Router测试:")
        
        # 获取报价
        quote = router.get_quote(test_symbol)
        if quote and quote.get('price', 0) > 0:
            print(f"  ✅ 报价: ${quote['price']:.2f}")
        else:
            print(f"  ❌ 报价获取失败")
        
        # 获取完整上下文
        context = router.get_full_market_context(test_symbol)
        if context:
            price = context.get('price', 0)
            vix = context.get('vix', 0)
            sentiment = context.get('news_sentiment', 0)
            
            print(f"  ✅ 完整上下文:")
            print(f"     价格: ${price:.2f}")
            print(f"     VIX:  {vix:.1f}")
            print(f"     情绪: {sentiment:+.2f}")
        else:
            print(f"  ❌ 完整上下文获取失败")
            
    except Exception as e:
        print(f"  ❌ Router测试失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)
    
    success_count = sum(1 for symbol_results in results.values() if len(symbol_results) > 0)
    total_symbols = len(test_symbols)
    
    print(f"\n✅ 成功获取数据的标的: {success_count}/{total_symbols}")
    print(f"   数据源: {', '.join(providers.keys())}")
    
    if success_count == total_symbols:
        print(f"\n🎉 所有数据源工作正常!")
        print(f"   可以使用真实数据进行AI决策和多空辩论")
    else:
        print(f"\n⚠️  部分数据源有问题,请检查:")
        for symbol, symbol_results in results.items():
            if not symbol_results:
                print(f"   ❌ {symbol}: 所有数据源均失败")
    
    print(f"\n📝 下一步:")
    print(f"   1. 确认报价数据准确")
    print(f"   2. 使用真实数据运行AI决策测试")
    print(f"   3. 验证多空辩论结果")
    
    return success_count > 0


if __name__ == "__main__":
    try:
        success = test_real_market_data()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⏹️  测试被中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
