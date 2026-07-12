#!/usr/bin/env python3.11
"""
FTSeek-US 状态落盘保存脚本

保存内容:
1. API Keys 配置
2. 监控股票池
3. 持仓配置
4. 当前价格快照
5. 系统状态
"""
import os
import json
from datetime import datetime
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(".env.us")

# 保存目录
SAVE_DIR = "data/snapshots"
os.makedirs(SAVE_DIR, exist_ok=True)

# 时间戳
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
save_file = f"{SAVE_DIR}/state_{timestamp}.json"

print("=" * 80)
print("  FTSeek-US 状态落盘保存")
print("=" * 80)
print()

# ============================================================
# 1. 保存 API Keys 配置
# ============================================================
api_config = {
    "TWELVEDATA_API_KEY": os.getenv("TWELVEDATA_API_KEY", ""),
    "FINNHUB_API_KEY": os.getenv("FINNHUB_API_KEY", ""),
    "DINGTALK_WEBHOOK_URL_US": os.getenv("DINGTALK_WEBHOOK_URL_US", ""),
    "DINGTALK_SECRET_US": os.getenv("DINGTALK_SECRET_US", ""),
}

print("1. API Keys 配置")
print("-" * 40)
for key, value in api_config.items():
    masked = f"{value[:10]}...{value[-5:]}" if len(value) > 15 else "***"
    print(f"   {key}: {masked}")
print()

# ============================================================
# 2. 保存监控股票池
# ============================================================
watchlist = [
    "AAPL",
    "MSFT",
    "GOOG",
    "AMZN",
    "NVDA",
    "META",
    "BABA",
    "PDD",
]

print("2. 监控股票池")
print("-" * 40)
for symbol in watchlist:
    print(f"   {symbol}")
print()

# ============================================================
# 3. 保存持仓配置
# ============================================================
positions = {
    "AAPL": {"cost": 170.0, "qty": 100, "add_times": 0, "last_add_price": 170.0},
    "MSFT": {"cost": 400.0, "qty": 50, "add_times": 0, "last_add_price": 400.0},
    "NVDA": {"cost": 800.0, "qty": 30, "add_times": 0, "last_add_price": 800.0},
}

print("3. 持仓配置")
print("-" * 40)
for symbol, pos in positions.items():
    print(f"   {symbol}: cost=${pos['cost']}, qty={pos['qty']}")
print()

# ============================================================
# 4. 获取当前价格快照
# ============================================================
import requests

print("4. 当前价格快照")
print("-" * 40)

prices = {}
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

for symbol in watchlist:
    try:
        url = "https://finnhub.io/api/v1/quote"
        params = {"symbol": symbol, "token": FINNHUB_API_KEY}
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data and "c" in data:
                prices[symbol] = {
                    "price": data["c"],
                    "change": data["d"],
                    "change_percent": data["dp"],
                    "timestamp": datetime.now().isoformat(),
                }
                print(f"   {symbol}: ${data['c']:.2f} ({data['dp']:+.2f}%)")
    except Exception as e:
        print(f"   {symbol}: 获取失败 - {str(e)[:30]}")

print()

# ============================================================
# 5. 保存系统状态
# ============================================================
system_state = {
    "timestamp": datetime.now().isoformat(),
    "api_config": api_config,
    "watchlist": watchlist,
    "positions": positions,
    "prices": prices,
    "config": {
        "STOP_LOSS_RATIO": float(os.getenv("DEFAULT_STOP_LOSS", "0.10")),
        "TAKE_PROFIT_1_RATIO": float(os.getenv("DEFAULT_TAKE_PROFIT_1", "0.15")),
        "TAKE_PROFIT_2_RATIO": float(os.getenv("DEFAULT_TAKE_PROFIT_2", "0.25")),
        "TAKE_PROFIT_3_RATIO": float(os.getenv("DEFAULT_TAKE_PROFIT_3", "0.40")),
        "ADD_POSITION_RATIO": float(os.getenv("ADD_POSITION_RATIO", "0.05")),
        "MAX_ADD_POSITION_TIMES": int(os.getenv("MAX_ADD_POSITION_TIMES", "3")),
        "QUERY_INTERVAL": 60,
    }
}

# 保存到文件
with open(save_file, 'w', encoding='utf-8') as f:
    json.dump(system_state, f, indent=2, ensure_ascii=False)

print("5. 系统状态")
print("-" * 40)
print(f"   保存文件：{save_file}")
print(f"   保存时间：{system_state['timestamp']}")
print(f"   监控股票：{len(watchlist)}只")
print(f"   持仓股票：{len(positions)}只")
print(f"   价格快照：{len(prices)}只")
print()

print("=" * 80)
print(f"  ✅ 状态保存成功：{save_file}")
print("=" * 80)
