#!/usr/bin/env python3.8
"""
数据源完整测试脚本

测试三层数据源架构:
1. TwelveData (Primary - 技术指标)
2. Finnhub (Primary - 实时报价)
3. Alpha Vantage (Fallback - 替换 Yahoo Finance)

验证:
- 单个数据源功能
- 数据源 fallback 机制
- 健康检查
- 限速器状态
"""
import sys
import json
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.market_data_router import MarketDataRouter
from app.data.twelvedata_provider import TwelveDataProvider
from app.data.finnhub_provider import FinnhubProvider
from app.data.alphavantage_provider import AlphaVantageProvider


def load_config():
    """加载配置"""
    import os
    from dotenv import load_dotenv
    
    env_path = Path(__file__).resolve().parent.parent / ".env.us"
    if env_path.exists():
        load_dotenv(env_path)
    
    return {
        "twelvedata_api_key": os.getenv("TWELVEDATA_API_KEY", ""),
        "finnhub_api_key": os.getenv("FINNHUB_API_KEY", ""),
        "alphavantage_api_key": os.getenv("ALPHAVANTAGE_API_KEY", ""),
    }


def test_individual_providers(config):
    """测试单个数据提供商"""
    print("=" * 70)
    print("第一部分：测试单个数据提供商")
    print("=" * 70)
    
    results = {}
    
    # TwelveData
    print("\n1. TwelveData")
    print("-" * 70)
    if config["twelvedata_api_key"]:
        td = TwelveDataProvider(api_key=config["twelvedata_api_key"])
        quote = td.get_quote("US.AAPL")
        if quote and quote.get("price", 0) > 0:
            print(f"   ✅ 成功 - AAPL ${quote['price']:.2f}")
            results["twelvedata"] = True
        else:
            print(f"   ❌ 失败 - 无数据")
            results["twelvedata"] = False
    else:
        print(f"   ⚠️  未配置 API Key")
        results["twelvedata"] = None
    
    # Finnhub
    print("\n2. Finnhub")
    print("-" * 70)
    if config["finnhub_api_key"]:
        fh = FinnhubProvider(api_key=config["finnhub_api_key"])
        quote = fh.get_quote("US.AAPL")
        if quote and quote.get("price", 0) > 0:
            print(f"   ✅ 成功 - AAPL ${quote['price']:.2f}")
            results["finnhub"] = True
        else:
            print(f"   ❌ 失败 - 无数据")
            results["finnhub"] = False
    else:
        print(f"   ⚠️  未配置 API Key")
        results["finnhub"] = None
    
    # Alpha Vantage
    print("\n3. Alpha Vantage")
    print("-" * 70)
    if config["alphavantage_api_key"]:
        av = AlphaVantageProvider(api_key=config["alphavantage_api_key"])
        quote = av.get_quote("US.AAPL")
        if quote and quote.get("price", 0) > 0:
            print(f"   ✅ 成功 - AAPL ${quote['price']:.2f}")
            results["alphavantage"] = True
        else:
            print(f"   ❌ 失败 - 无数据")
            results["alphavantage"] = False
    else:
        print(f"   ⚠️  未配置 API Key")
        results["alphavantage"] = None
    
    return results


def test_router(config):
    """测试数据路由器"""
    print("\n" + "=" * 70)
    print("第二部分：测试数据路由器 (Fallback 机制)")
    print("=" * 70)
    
    router = MarketDataRouter(
        twelvedata_api_key=config["twelvedata_api_key"],
        finnhub_api_key=config["finnhub_api_key"],
        alphavantage_api_key=config["alphavantage_api_key"],
    )
    
    # 启用的数据源
    enabled = router.get_enabled_sources()
    print(f"\n启用的数据源：{enabled if enabled else ['无']}")
    
    if not enabled:
        print("\n❌ 未配置任何数据源，跳过测试")
        return False
    
    # 测试 1: 单个报价
    print("\n1. 单个报价 (US.AAPL)")
    print("-" * 70)
    quote = router.get_quote("US.AAPL")
    if quote:
        print(f"   ✅ 成功 - AAPL ${quote['price']:.2f} ({quote['change_percent']:+.2%})")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 2: 批量报价
    print("\n2. 批量报价")
    print("-" * 70)
    symbols = ["US.AAPL", "US.MSFT", "US.NVDA"]
    quotes = router.get_batch_quotes(symbols)
    if quotes:
        print(f"   ✅ 成功 - 获取 {len(quotes)}/{len(symbols)} 只股票")
        for sym, q in quotes.items():
            print(f"      {sym}: ${q['price']:.2f}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 3: 完整市场上下文
    print("\n3. 完整市场上下文 (US.NVDA)")
    print("-" * 70)
    context = router.get_full_market_context("US.NVDA")
    if context:
        print(f"   ✅ 成功")
        print(f"      价格：${context['price']:.2f}")
        print(f"      趋势：{context['trend_direction']}")
        print(f"      VIX: {context['vix']:.2f}")
        print(f"      市场状态：{context['market_regime']}")
        print(f"      新闻情绪：{context['news_sentiment']:.2f}")
    else:
        print(f"   ❌ 失败")
        return False
    
    # 测试 4: 健康检查
    print("\n4. 健康检查")
    print("-" * 70)
    health = router.health_check()
    print(f"   整体健康：{'✅ 健康' if health['healthy'] else '❌ 不健康'}")
    print(f"   健康数据源：{health['sources_healthy']}/{health['sources_total']}")
    for source, status in health["sources"].items():
        icon = "✅" if status.get("healthy") else "❌"
        print(f"      {icon} {source}: {status}")
    
    # 测试 5: 限速器状态
    print("\n5. 限速器状态")
    print("-" * 70)
    status = router.get_rate_limit_status()
    for source, s in status.items():
        if "error" in s:
            print(f"   {source}: ❌ {s['error']}")
        else:
            tokens = s.get("tokens_remaining", "N/A")
            capacity = s.get("capacity", "N/A")
            daily = s.get("daily_remaining", "")
            if daily:
                print(f"   {source}: 令牌 {tokens}/{capacity} | 今日剩余 {daily}")
            else:
                print(f"   {source}: 令牌 {tokens}/{capacity}")
    
    return True


def test_fallback_mechanism():
    """测试 fallback 机制"""
    print("\n" + "=" * 70)
    print("第三部分：测试 Fallback 机制")
    print("=" * 70)
    
    # 仅用 Alpha Vantage 测试 fallback
    import os
    av_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
    
    if not av_key:
        print("\n⚠️  Alpha Vantage 未配置，跳过 fallback 测试")
        return True
    
    print("\n仅使用 Alpha Vantage (模拟 Primary 数据源不可用)")
    print("-" * 70)
    
    router = MarketDataRouter(
        twelvedata_api_key=None,  # 禁用
        finnhub_api_key=None,     # 禁用
        alphavantage_api_key=av_key,
    )
    
    quote = router.get_quote("US.AAPL")
    if quote:
        print(f"   ✅ Fallback 成功 - AAPL ${quote['price']:.2f}")
        return True
    else:
        print(f"   ❌ Fallback 失败")
        return False


def main():
    """主测试函数"""
    print("\n" + "=" * 70)
    print("FTSeek-US 数据源完整测试")
    print("=" * 70)
    
    # 加载配置
    config = load_config()
    
    configured = []
    if config["twelvedata_api_key"]:
        configured.append("TwelveData")
    if config["finnhub_api_key"]:
        configured.append("Finnhub")
    if config["alphavantage_api_key"]:
        configured.append("AlphaVantage")
    
    print(f"\n已配置的数据源：{configured if configured else ['无']}")
    
    if not configured:
        print("\n❌ 未配置任何数据源!")
        print("\n请在 .env.us 中配置至少一个数据源 API Key:")
        print("  TWELVEDATA_API_KEY=...")
        print("  FINNHUB_API_KEY=...")
        print("  ALPHAVANTAGE_API_KEY=...")
        return False
    
    # 测试单个提供商
    individual_results = test_individual_providers(config)
    
    # 测试路由器
    router_success = test_router(config)
    
    # 测试 fallback
    fallback_success = test_fallback_mechanism()
    
    # 总结
    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    
    print("\n单个数据提供商:")
    for name, result in individual_results.items():
        if result is True:
            print(f"   ✅ {name}")
        elif result is False:
            print(f"   ❌ {name}")
        else:
            print(f"   ⚠️  {name} (未配置)")
    
    print(f"\n数据路由器：{'✅ 通过' if router_success else '❌ 失败'}")
    print(f"Fallback 机制：{'✅ 通过' if fallback_success else '❌ 失败'}")
    
    overall = router_success and fallback_success
    print(f"\n{'✅ 所有测试通过!' if overall else '❌ 部分测试失败'}")
    print("=" * 70)
    
    return overall


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
