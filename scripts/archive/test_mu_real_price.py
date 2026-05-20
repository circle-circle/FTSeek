#!/usr/bin/env python3
"""
快速真实数据验证 - 仅测试MU报价
"""
import sys
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env.us")

from datetime import datetime

print("=" * 80)
print("MU真实报价快速验证")
print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 80)

# 测试MU真实报价
symbol = "US.MU"

print(f"\n📊 {symbol} 真实数据获取...")

# 1. TwelveData
try:
    from app.data.twelvedata_provider import TwelveDataProvider
    
    td_key = os.getenv("TWELVEDATA_API_KEY", "")
    td = TwelveDataProvider(api_key=td_key, cache_ttl_seconds=30)
    
    quote_td = td.get_quote(symbol)
    if quote_td:
        print(f"\n✅ TwelveData:")
        print(f"   价格: ${quote_td.get('price', 0):.2f}")
        print(f"   涨跌: {quote_td.get('change_pct', 0):+.2%}")
        print(f"   成交量: {quote_td.get('volume', 0):,}")
        print(f"   时间: {quote_td.get('timestamp', 'N/A')}")
except Exception as e:
    print(f"\n❌ TwelveData失败: {e}")

# 2. Finnhub
try:
    from app.data.finnhub_provider import FinnhubProvider
    
    fh_key = os.getenv("FINNHUB_API_KEY", "")
    fh = FinnhubProvider(api_key=fh_key, cache_ttl_seconds=30)
    
    quote_fh = fh.get_quote(symbol)
    if quote_fh:
        print(f"\n✅ Finnhub:")
        print(f"   价格: ${quote_fh.get('price', 0):.2f}")
        print(f"   涨跌: {quote_fh.get('change_pct', 0):+.2%}")
        print(f"   成交量: {quote_fh.get('volume', 0):,}")
except Exception as e:
    print(f"\n❌ Finnhub失败: {e}")

# 3. MarketDataRouter (统一)
try:
    from app.data.market_data_router import MarketDataRouter
    
    router = MarketDataRouter(
        twelvedata_api_key=td_key,
        finnhub_api_key=fh_key,
        cache_ttl_seconds=30,
    )
    
    quote = router.get_quote(symbol)
    if quote:
        print(f"\n✅ Router统一报价:")
        print(f"   价格: ${quote.get('price', 0):.2f}")
        print(f"   来源: {quote.get('source', 'unknown')}")
except Exception as e:
    print(f"\n❌ Router失败: {e}")

# 4. 技术指标
print(f"\n📈 技术指标 (TwelveData):")
try:
    context = td.get_full_market_context(symbol)
    if context:
        tech = context.get('technical_indicators', {})
        print(f"   SMA20:  ${tech.get('sma_20', 0):.2f}")
        print(f"   SMA50:  ${tech.get('sma_50', 0):.2f}")
        print(f"   RSI14:  {tech.get('rsi_14', 0):.1f}")
        print(f"   MACD:   {tech.get('macd', 0):.2f}")
except Exception as e:
    print(f"   ❌ 获取失败: {e}")

print("\n" + "=" * 80)
print("验证完成")
print("=" * 80)
