#!/usr/bin/env python3
"""Test data collector after fix"""
import os
import sys

print("=== Environment Check ===")
print(f"FUTU_OPEND_HOST: {os.environ.get('FUTU_OPEND_HOST', 'NOT SET')}")
print(f"FINNHUB_API_KEY: {'SET' if os.environ.get('FINNHUB_API_KEY') else 'NOT SET'}")

print("\n=== Testing Data Collector ===")
from app.playbook.data_collector import PlaybookDataCollector
from app.playbook.pipeline import _build_data_collector

collector = _build_data_collector()
print("✓ Data collector initialized")

print("\n--- Testing US.AAPL data collection ---")
data = collector.collect("US.AAPL")

print(f"✓ Data collected for {data.symbol}")
print(f"  Technical quality: {data.technical.quality}")
print(f"  Fundamental quality: {data.fundamental.quality}")
print(f"  Insider quality: {data.insider.quality}")
print(f"  News quality: {data.news.quality}")
print(f"  Earnings quality: {data.earnings.quality}")
print(f"  Sentiment quality: {data.sentiment.quality}")

if data.technical.price:
    print(f"\n✓ Technical data:")
    print(f"  Price: ${data.technical.price:.2f}")
    if data.technical.rsi:
        print(f"  RSI: {data.technical.rsi:.2f}")

if data.fundamental.market_cap:
    print(f"\n✓ Fundamental data:")
    print(f"  Market cap: ${data.fundamental.market_cap:,.0f}")
    if data.fundamental.pe:
        print(f"  PE: {data.fundamental.pe:.2f}")

print("\n=== Test Complete ===")
