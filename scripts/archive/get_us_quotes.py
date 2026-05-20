#!/usr/bin/env python3
"""
美股行情获取脚本 - FTSeek-US

使用 TwelveData API 获取美股实时行情 (备用数据源)
无需富途美股行情权限

使用方法:
  python3 scripts/get_us_quotes.py [股票代码]
  python3 scripts/get_us_quotes.py AAPL,MSFT,NVDA
"""
import os
import sys
import requests
import json
from datetime import datetime

# 从环境变量获取 API Key
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "c359be1c550f4c2b8240eaf62fcb8536")
TWELVEDATA_URL = "https://api.twelvedata.com/quote"


def get_quote(symbol: str) -> dict:
    """
    获取单个股票行情
    
    Args:
        symbol: 股票代码 (如 AAPL, MSFT)
    
    Returns:
        行情数据字典
    """
    params = {
        "symbol": symbol,
        "exchange": "NASDAQ" if symbol in ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"] else "NYSE",
        "country": "United States",
        "apikey": TWELVEDATA_API_KEY,
    }
    
    try:
        response = requests.get(TWELVEDATA_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "status" in data and data["status"] == "error":
            return {
                "error": True,
                "message": data.get("message", "API 错误"),
            }
        
        return {
            "error": False,
            "symbol": data.get("symbol", symbol),
            "name": data.get("name", ""),
            "price": float(data.get("close", 0)),
            "change": float(data.get("change", 0)),
            "change_percent": float(data.get("change_percent", 0)),
            "high": float(data.get("high", 0)),
            "low": float(data.get("low", 0)),
            "open": float(data.get("open", 0)),
            "previous_close": float(data.get("previous_close", 0)),
            "timestamp": data.get("datetime", ""),
        }
    except requests.exceptions.RequestException as e:
        return {
            "error": True,
            "message": f"请求失败：{e}",
        }
    except (KeyError, ValueError) as e:
        return {
            "error": True,
            "message": f"解析失败：{e}",
        }


def get_multiple_quotes(symbols: list) -> list:
    """
    获取多个股票行情
    
    Args:
        symbols: 股票代码列表
    
    Returns:
        行情数据列表
    """
    results = []
    for symbol in symbols:
        data = get_quote(symbol)
        results.append(data)
    return results


def print_quotes(symbols: list):
    """格式化打印行情"""
    results = get_multiple_quotes(symbols)
    
    print(f"\n{'股票代码':<10} {'名称':<20} {'当前价':>10} {'涨跌额':>10} {'涨跌幅':>10}")
    print("-" * 65)
    
    for data in results:
        if data.get("error"):
            print(f"{data.get('symbol', 'N/A'):<10} {'获取失败':<20} {'-':>10} {'-':>10} {'-':>10}")
            print(f"  错误：{data.get('message', '')}")
        else:
            symbol = data["symbol"]
            name = data["name"][:18] if len(data["name"]) > 18 else data["name"]
            price = data["price"]
            change = data["change"]
            change_pct = data["change_percent"]
            
            # 颜色标记
            if change_pct > 0:
                sign = "+"
            elif change_pct < 0:
                sign = ""
            else:
                sign = ""
            
            print(f"{symbol:<10} {name:<20} ${price:>9.2f} {sign}{change:>9.2f} {sign}{change_pct:>9.2f}%")
    
    print("-" * 65)
    print(f"数据时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print(f"数据源：TwelveData API")
    
    return results


def main():
    """主函数"""
    # 默认监控股票池
    default_symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "BABA", "PDD"]
    
    if len(sys.argv) > 1:
        # 从命令行参数获取股票代码
        symbols = sys.argv[1].upper().split(",")
    else:
        # 使用默认列表
        symbols = default_symbols
    
    print_quotes(symbols)


if __name__ == "__main__":
    main()
