#!/usr/bin/env python3.8
"""
FTSeek-US 数据源测试脚本

测试富途 OpenD 实时行情获取
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

print("=" * 80)
print("  FTSeek-US 数据源测试")
print(f"  数据源：{os.getenv('DATA_SOURCE', 'futu_opend')}")
print("=" * 80)
print()

# ============================================================
# 测试 1: 富途 OpenD 连接
# ============================================================
print("1. 富途 OpenD 连接测试")
print("-" * 40)

try:
    from futu import OpenQuoteContext, RET_OK
    
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    
    print(f"   连接：{host}:{port}")
    
    quote_ctx = OpenQuoteContext(host=host, port=port)
    print(f"   ✅ 连接成功")
    
    # 测试获取行情
    test_symbols = ['AAPL', 'MSFT', 'NVDA', 'GOOG', 'AMZN']
    
    print()
    print("2. 实时行情测试")
    print("-" * 40)
    
    # 先获取股票列表
    ret_code, ret_data = quote_ctx.get_stock_list(market='US')
    
    if ret_code == RET_OK:
        # 过滤出测试股票
        test_codes = [code for code in ret_data if code.split('.')[1] in test_symbols]
        
        for symbol in test_codes[:5]:  # 最多 5 个
            ret_code, ret_data = quote_ctx.get_stock_quote(stock_list=[symbol])
            
            if ret_code == RET_OK and ret_data is not None and len(ret_data) > 0:
                row = ret_data[0]
                price = row.get('last_price', 0)
                change = row.get('change', 0)
                change_percent = row.get('change_percent', 0)
                
                print(f"   ✅ {symbol}: ${price:.2f}, 涨跌：{change:+.2f} ({change_percent:+.2%})")
            else:
                print(f"   ⚠️  {symbol}: 数据获取失败")
    else:
        print(f"   ⚠️  获取股票列表失败")
    
    quote_ctx.close()
    print()
    print("   ✅ 富途 OpenD 测试完成")
    
except ImportError as e:
    print(f"   ❌ 模块导入失败：{e}")
    print(f"   解决方案：pip install futu-api")
except Exception as e:
    print(f"   ❌ 测试失败：{str(e)[:100]}")

print()

# ============================================================
# 测试 2: LLM API 连接
# ============================================================
print("3. LLM API 配置测试")
print("-" * 40)

try:
    import requests
    
    api_key = os.getenv("BAILIAN_API_KEY")
    base_url = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com")
    
    # 测试 Bailian API
    url = f"{base_url}/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": "Hello"}]
    }
    
    response = requests.post(url, headers=headers, json=data, timeout=10)
    
    if response.status_code == 200:
        print(f"   ✅ Bailian API 正常")
    else:
        print(f"   ⚠️  Bailian API 失败：{response.status_code}")
        print(f"   建议：检查 API Key 是否正确")
        
except Exception as e:
    print(f"   ❌ LLM API 测试失败：{str(e)[:100]}")

print()

# ============================================================
# 测试 3: 钉钉推送
# ============================================================
print("4. 钉钉推送测试")
print("-" * 40)

try:
    from app.observability.alert_manager import AlertManager
    
    webhook = os.getenv("DINGTALK_WEBHOOK_URL_US")
    secret = os.getenv("DINGTALK_SECRET_US")
    
    if webhook and secret:
        manager = AlertManager(
            provider="dingtalk",
            dingtalk_webhook=webhook,
            dingtalk_secret=secret,
        )
        
        result = manager.send_info(
            title="⚙️ FTSeek-US 数据源测试",
            content="数据源和 API 配置测试完成"
        )
        
        print(f"   ✅ 钉钉推送：{'成功' if result else '失败'}")
    else:
        print(f"   ⚠️  钉钉配置缺失")
        
except Exception as e:
    print(f"   ❌ 钉钉推送测试失败：{str(e)[:100]}")

print()
print("=" * 80)
print("  测试完成")
print("=" * 80)
