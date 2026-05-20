#!/usr/bin/env python3.8
"""
FTSeek-US LLM API 测试脚本

测试 Bailian 和 DeepSeek API 调用
"""
import sys
import os
import requests
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from dotenv import load_dotenv
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

print("=" * 80)
print("  FTSeek-US LLM API 测试")
print("=" * 80)
print()

# ============================================================
# 测试 1: Bailian API
# ============================================================
print("1. Bailian API 测试")
print("-" * 40)

bailian_key = os.getenv("BAILIAN_API_KEY")
bailian_url = os.getenv("BAILIAN_BASE_URL")

print(f"   API Key: {bailian_key[:20]}...")
print(f"   Base URL: {bailian_url}")

if bailian_key and bailian_url:
    try:
        url = f"{bailian_url}/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {bailian_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "qwen3.5-plus",
            "messages": [
                {"role": "user", "content": "你好，请用一句话介绍你自己"}
            ],
            "max_tokens": 100
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"   ✅ Bailian API 正常")
            print(f"   回复：{content[:50]}...")
        else:
            print(f"   ❌ Bailian API 失败：{response.status_code}")
            print(f"   响应：{response.text[:200]}")
    except Exception as e:
        print(f"   ❌ Bailian API 异常：{str(e)[:100]}")
else:
    print(f"   ❌ 配置缺失")

print()

# ============================================================
# 测试 2: DeepSeek API
# ============================================================
print("2. DeepSeek API 测试")
print("-" * 40)

deepseek_key = os.getenv("DEEPSEEK_API_KEY")
deepseek_url = os.getenv("DEEPSEEK_BASE_URL")

print(f"   API Key: {deepseek_key[:20]}...")
print(f"   Base URL: {deepseek_url}")

if deepseek_key and deepseek_url:
    try:
        url = f"{deepseek_url}/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {deepseek_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": "你好，请用一句话介绍你自己"}
            ],
            "max_tokens": 100
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"   ✅ DeepSeek API 正常")
            print(f"   回复：{content[:50]}...")
        else:
            print(f"   ❌ DeepSeek API 失败：{response.status_code}")
            print(f"   响应：{response.text[:200]}")
    except Exception as e:
        print(f"   ❌ DeepSeek API 异常：{str(e)[:100]}")
else:
    print(f"   ❌ 配置缺失")

print()

# ============================================================
# 测试 3: Bailian 其他模型
# ============================================================
print("3. Bailian 其他模型测试")
print("-" * 40)

models_to_test = ["glm-5", "MiniMax-M2.5", "kimi-k2.5"]

for model in models_to_test:
    try:
        url = f"{bailian_url}/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {bailian_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": model,
            "messages": [
                {"role": "user", "content": "你好"}
            ],
            "max_tokens": 50
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            print(f"   ✅ {model}: 正常")
        else:
            print(f"   ⚠️  {model}: {response.status_code}")
    except Exception as e:
        print(f"   ❌ {model}: {str(e)[:50]}")

print()
print("=" * 80)
print("  测试完成")
print("=" * 80)
