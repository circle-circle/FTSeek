#!/usr/bin/env python3.11
"""
测试双数据源 API 连接
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv(".env.us")

print("=" * 80)
print("  测试双数据源 API 连接")
print("=" * 80)
print()

# 获取 API Keys
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

print(f"TwelveData API Key: {TWELVEDATA_API_KEY[:20]}...")
print(f"Finnhub API Key: {FINNHUB_API_KEY[:20]}...")
print()

# 测试股票
test_stocks = ["AAPL", "MSFT", "NVDA"]

# ============================================================
# 测试 TwelveData
# ============================================================
print("1. 测试 TwelveData API")
print("-" * 40)

if not TWELVEDATA_API_KEY:
    print("❌ TwelveData API Key 未配置")
else:
    import requests
    
    for symbol in test_stocks:
        try:
            url = "https://api.twelvedata.com/price"
            params = {
                "symbol": symbol,
                "apikey": TWELVEDATA_API_KEY,
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if "price" in data:
                    print(f"✅ {symbol}: ${data['price']}")
                else:
                    print(f"⚠️  {symbol}: 无数据")
            else:
                print(f"❌ {symbol}: HTTP {response.status_code}")
        except Exception as e:
            print(f"❌ {symbol}: {str(e)[:50]}")

print()

# ============================================================
# 测试 Finnhub
# ============================================================
print("2. 测试 Finnhub API")
print("-" * 40)

if not FINNHUB_API_KEY:
    print("❌ Finnhub API Key 未配置")
else:
    import requests
    
    for symbol in test_stocks:
        try:
            url = "https://finnhub.io/api/v1/quote"
            params = {
                "symbol": symbol,
                "token": FINNHUB_API_KEY,
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data and "c" in data and data["c"] > 0:
                    print(f"✅ {symbol}: ${data['c']:.2f} (涨跌：{data['d']:+.2f}, {data['dp']:+.2f}%)")
                else:
                    print(f"⚠️  {symbol}: 无数据")
            else:
                print(f"❌ {symbol}: HTTP {response.status_code}")
        except Exception as e:
            print(f"❌ {symbol}: {str(e)[:50]}")

print()
print("=" * 80)
print("  测试完成")
print("=" * 80)
