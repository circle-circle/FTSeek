#!/usr/bin/env python3.8
"""
Alpha Vantage 数据源测试脚本

测试功能:
1. 实时报价
2. 市场数据
3. VIX 恐慌指数
4. SPY 大盘上下文
5. ATR 波动率
6. 限速器状态
"""
import sys
import json
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.alphavantage_provider import AlphaVantageProvider


def test_alphavantage():
    """测试 Alpha Vantage 数据提供商"""
    print("=" * 70)
    print("Alpha Vantage 数据源测试")
    print("=" * 70)
    
    provider = AlphaVantageProvider()
    
    if not provider.enabled:
        print("\n❌ Alpha Vantage API Key 未配置")
        print("\n请在 .env.us 中配置:")
        print("  ALPHAVANTAGE_API_KEY=your_api_key_here")
        print("\n获取 API Key: https://www.alphavantage.co/support/#api-key")
        return False
    
    print(f"\n✅ API Key 已配置")
    print(f"   Premium: {provider.premium}")
    print(f"   Cache TTL: {provider.cache_ttl}")
    
    # 测试 1: 单只股票报价
    print("\n" + "-" * 70)
    print("测试 1: 实时报价 (US.AAPL)")
    print("-" * 70)
    quote = provider.get_quote("US.AAPL")
    if quote:
        print(f"   ✅ 成功")
        print(f"   价格：${quote['price']:.2f}")
        print(f"   涨跌：{quote['change_percent']:+.2%}")
        print(f"   成交量：{quote['volume']:,}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 2: 市场数据
    print("\n" + "-" * 70)
    print("测试 2: 市场数据 (US.NVDA)")
    print("-" * 70)
    market = provider.get_market_data("US.NVDA")
    if market:
        print(f"   ✅ 成功")
        print(f"   价格：${market['price']:.2f}")
        print(f"   趋势：{market['trend']}")
        print(f"   动量：{market['momentum']:.2%}")
        print(f"   MA20: ${market['ma20']:.2f}")
        print(f"   MA50: ${market['ma50']:.2f}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 3: VIX 恐慌指数
    print("\n" + "-" * 70)
    print("测试 3: VIX 恐慌指数")
    print("-" * 70)
    vix = provider.get_vix()
    print(f"   VIX: {vix:.2f}")
    if vix > 0:
        print(f"   ✅ 成功")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 4: SPY 大盘上下文
    print("\n" + "-" * 70)
    print("测试 4: SPY 大盘上下文")
    print("-" * 70)
    spy_ctx = provider.get_spy_context()
    if spy_ctx:
        print(f"   ✅ 成功")
        print(f"   SPY 价格：${spy_ctx['spy_price']:.2f}")
        print(f"   SPY MA20: ${spy_ctx['spy_ma20']:.2f}")
        print(f"   SPY MA50: ${spy_ctx['spy_ma50']:.2f}")
        print(f"   今日涨跌：{spy_ctx['spx_change_percent']:+.2%}")
        print(f"   QQQ 涨跌：{spy_ctx['qqq_change_percent']:+.2%}")
        print(f"   5 日涨跌：{spy_ctx['price_change_5d']:+.2%}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 5: ATR 波动率
    print("\n" + "-" * 70)
    print("测试 5: ATR 波动率 (US.TSLA)")
    print("-" * 70)
    atr = provider.compute_atr("US.TSLA")
    if atr and atr.get('atr', 0) > 0:
        print(f"   ✅ 成功")
        print(f"   ATR: {atr['atr']:.2f}")
        print(f"   平均 ATR: {atr['avg_atr']:.2f}")
        print(f"   波动率：{atr['volatility']:.2%}")
        print(f"   平均波动率：{atr['avg_volatility']:.2%}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 6: 完整市场上下文
    print("\n" + "-" * 70)
    print("测试 6: 完整市场上下文 (US.MSFT)")
    print("-" * 70)
    context = provider.get_full_market_context("US.MSFT")
    if context:
        print(f"   ✅ 成功")
        print(f"   价格：${context['price']:.2f}")
        print(f"   趋势：{context['trend_direction']}")
        print(f"   VIX: {context['vix']:.2f}")
        print(f"   波动率：{context['volatility']:.2%}")
        print(f"    ATR: {context['atr']:.2f}")
        print(f"   市场状态：{context['market_regime']}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 7: 限速器状态
    print("\n" + "-" * 70)
    print("测试 7: 限速器状态")
    print("-" * 70)
    status = provider.get_rate_limit_status()
    print(f"   ✅ 成功")
    print(f"   令牌剩余：{status['tokens_remaining']}/{status['capacity']}")
    print(f"   今日剩余：{status['daily_remaining']}/{status['daily_limit']}")
    print(f"   补充间隔：{status['refill_interval_sec']}s/令牌")
    
    # 测试 8: 批量获取
    print("\n" + "-" * 70)
    print("测试 8: 批量获取报价")
    print("-" * 70)
    symbols = ["US.AAPL", "US.MSFT", "US.NVDA"]
    print(f"   测试股票：{symbols}")
    quotes = provider.get_batch_quotes(symbols)
    if quotes:
        print(f"   ✅ 成功获取 {len(quotes)}/{len(symbols)} 只股票")
        for sym, q in quotes.items():
            print(f"   {sym}: ${q['price']:.2f} ({q['change_percent']:+.2%})")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 总结
    print("\n" + "=" * 70)
    print("✅ 所有测试通过!")
    print("=" * 70)
    
    return True


if __name__ == "__main__":
    success = test_alphavantage()
    sys.exit(0 if success else 1)
