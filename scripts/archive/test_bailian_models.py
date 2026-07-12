#!/usr/bin/env python3.11
"""
百炼多模型调用测试脚本

测试模型:
- GLM-5 (智谱)
- MiniMax-M2.5
- Qwen3.5-Plus (阿里)

使用百炼API Key进行统一调用
"""
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

# 加载环境变量
from dotenv import load_dotenv
load_dotenv('/opt/ftseek/FTSeek-US/.env.us')

import requests

# ============================================================
#  配置
# ============================================================
BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY", "")
BAILIAN_BASE_URL = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

# 测试模型列表
TEST_MODELS = [
    {
        "name": "GLM-5",
        "model_id": "glm-5",
        "provider": "智谱",
        "description": "通用大模型，风控和决策支持"
    },
    {
        "name": "MiniMax-M2.5",
        "model_id": "MiniMax-M2.5",
        "provider": "MiniMax",
        "description": "情绪分析和市场判断"
    },
    {
        "name": "Qwen3.5-Plus",
        "model_id": "qwen3.5-plus",
        "provider": "阿里云",
        "description": "主力模型，通用任务"
    }
]

# 测试提示词
TEST_PROMPT = """你是一个专业的美股量化交易分析师。请分析以下市场数据并给出评分:

股票: US.AAPL (Apple Inc.)
当前价格: $178.50
日涨跌: +1.2%
成交量: 58,000,000
RSI(14): 55.2
MACD: 0.85
MA5: $177.80
MA20: $175.20

请根据以上数据，给出 0-100 的综合评分，并严格按照 JSON 格式返回:
{"strength": <0-100整数>, "confidence": <0.0-1.0小数>, "direction": "<LONG或SHORT>", "reasoning": "<50字以内的分析理由>"}"""


def test_model(model_info: dict) -> dict:
    """测试单个模型"""
    result = {
        "model": model_info["name"],
        "model_id": model_info["model_id"],
        "provider": model_info["provider"],
        "success": False,
        "response_time_ms": 0,
        "response": None,
        "error": None,
        "timestamp": datetime.now().isoformat()
    }
    
    if not BAILIAN_API_KEY:
        result["error"] = "BAILIAN_API_KEY 未配置"
        return result
    
    url = f"{BAILIAN_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {BAILIAN_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model_info["model_id"],
        "messages": [
            {"role": "system", "content": "你是一个专业的量化交易分析师。"},
            {"role": "user", "content": TEST_PROMPT}
        ],
        "temperature": 0.7,
        "max_tokens": 500
    }
    
    try:
        start_time = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        elapsed_ms = (time.time() - start_time) * 1000
        result["response_time_ms"] = round(elapsed_ms, 2)
        
        if response.status_code == 200:
            data = response.json()
            result["success"] = True
            result["response"] = {
                "choices": data.get("choices", []),
                "usage": data.get("usage", {}),
                "model": data.get("model", model_info["model_id"])
            }
            
            # 提取回复内容
            if data.get("choices"):
                content = data["choices"][0].get("message", {}).get("content", "")
                result["parsed_response"] = content[:200]  # 截取前200字符
            
            print(f"✅ {model_info['name']} ({model_info['model_id']}) - 成功")
            print(f"   响应时间: {result['response_time_ms']:.0f}ms")
            print(f"   Token使用: {data.get('usage', {})}")
        else:
            result["error"] = f"HTTP {response.status_code}: {response.text[:200]}"
            print(f"❌ {model_info['name']} ({model_info['model_id']}) - 失败")
            print(f"   错误: {result['error']}")
    
    except requests.exceptions.Timeout:
        result["error"] = "请求超时 (30s)"
        print(f"⏱️ {model_info['name']} - 超时")
    except Exception as e:
        result["error"] = str(e)
        print(f"❌ {model_info['name']} - 异常: {e}")
    
    return result


def main():
    """主测试函数"""
    print("=" * 60)
    print("百炼多模型调用测试")
    print("=" * 60)
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"API Key: {BAILIAN_API_KEY[:20]}..." if BAILIAN_API_KEY else "API Key: 未配置")
    print(f"Base URL: {BAILIAN_BASE_URL}")
    print("=" * 60)
    print()
    
    # 测试结果
    results = []
    
    # 逐个测试模型
    for model_info in TEST_MODELS:
        print(f"\n📋 测试模型: {model_info['name']} ({model_info['provider']})")
        print(f"   模型ID: {model_info['model_id']}")
        print(f"   用途: {model_info['description']}")
        print("-" * 40)
        
        result = test_model(model_info)
        results.append(result)
        
        # 显示响应内容
        if result["success"] and result.get("parsed_response"):
            print(f"\n   📝 响应内容预览:")
            print(f"   {result['parsed_response'][:150]}...")
        
        print()
        time.sleep(1)  # 避免请求过快
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    
    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count
    
    print(f"✅ 成功: {success_count}/{len(results)}")
    print(f"❌ 失败: {fail_count}/{len(results)}")
    print()
    
    # 详细结果表格
    print("-" * 60)
    print(f"{'模型':<20} {'状态':<10} {'响应时间':<15} {'备注'}")
    print("-" * 60)
    for r in results:
        status = "✅ 成功" if r["success"] else "❌ 失败"
        time_str = f"{r['response_time_ms']:.0f}ms" if r["response_time_ms"] else "N/A"
        note = r.get("error", "OK")[:30] if not r["success"] else "OK"
        print(f"{r['model']:<20} {status:<10} {time_str:<15} {note}")
    print("-" * 60)
    
    # 保存测试报告
    report = {
        "test_time": datetime.now().isoformat(),
        "api_key_prefix": BAILIAN_API_KEY[:20] if BAILIAN_API_KEY else None,
        "base_url": BAILIAN_BASE_URL,
        "total_tests": len(results),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results
    }
    
    report_file = f"/opt/ftseek/FTSeek-US/docs/bailian_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    os.makedirs(os.path.dirname(report_file), exist_ok=True)
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n📄 测试报告已保存: {report_file}")
    print("=" * 60)
    
    return success_count == len(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)