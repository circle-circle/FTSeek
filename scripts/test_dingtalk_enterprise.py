#!/usr/bin/env python3
"""
测试钉钉企业应用推送和按钮回调
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
import requests
import hmac
import hashlib
import base64
import urllib.parse

def get_access_token(app_key: str, app_secret: str) -> str:
    """获取 access_token"""
    url = "https://oapi.dingtalk.com/gettoken"
    params = {
        "appkey": app_key,
        "appsecret": app_secret
    }
    response = requests.get(url, params=params, timeout=10)
    result = response.json()
    
    if result.get("errcode") == 0:
        return result.get("access_token")
    else:
        raise Exception(f"获取 token 失败：{result}")

def send_interactive_card(agent_id: str, access_token: str, userid: str = "@all"):
    """发送交互式卡片消息"""
    url = f"https://oapi.dingtalk.com/topapi/message/sendto_conversation?access_token={access_token}"
    
    card = {
        "msgtype": "action_card",
        "action_card": {
            "title": "🟢 交易确认 - HK.00700 腾讯控股",
            "markdown": "**方向**: 买入\n**价格**: $500.00\n**数量**: 100 股\n**止损**: $480.00\n**目标**: $550.00",
            "btns": [
                {
                    "title": "✅ 确认买入",
                    "action_url": "http://39.97.58.4:8900/trade/callback?card_id=trade_HK00700_test&decision=confirm&code=HK.00700&action=BUY&quantity=100&price=500.0",
                    "action_type": "link"
                },
                {
                    "title": "❌ 忽略",
                    "action_url": "http://39.97.58.4:8900/trade/callback?card_id=trade_HK00700_test&decision=ignore&code=HK.00700&action=BUY",
                    "action_type": "link"
                }
            ]
        }
    }
    
    payload = {
        "agent_id": agent_id,
        "userid": userid,
        "msgtype": "action_card",
        "action_card": card["action_card"]
    }
    
    response = requests.post(url, json=payload, timeout=10)
    result = response.json()
    
    if result.get("errcode") == 0:
        print("✅ 消息推送成功")
        return True
    else:
        print(f"❌ 消息推送失败：{result}")
        return False

def main():
    print("=" * 70)
    print("🧪 钉钉企业应用推送测试")
    print("=" * 70)
    print()
    
    # 检查配置
    app_key = os.getenv("DINGTALK_APP_KEY", "")
    app_secret = os.getenv("DINGTALK_APP_SECRET", "")
    agent_id = os.getenv("DINGTALK_AGENT_ID", "")
    
    if not app_key or not app_secret or not agent_id:
        print("❌ 配置不完整，请先运行配置脚本或手动配置 .env 文件")
        print()
        print("需要配置:")
        print("  DINGTALK_APP_KEY=dingxxxxx")
        print("  DINGTALK_APP_SECRET=xxxxx")
        print("  DINGTALK_AGENT_ID=1234567890")
        return False
    
    print(f"✅ 配置检查通过")
    print(f"  AppKey: {app_key[:12]}...")
    print(f"  AgentId: {agent_id}")
    print()
    
    # 获取 access_token
    print("📡 获取 access_token...")
    try:
        access_token = get_access_token(app_key, app_secret)
        print(f"✅ Token 获取成功：{access_token[:20]}...")
        print()
    except Exception as e:
        print(f"❌ Token 获取失败：{e}")
        print()
        print("可能原因:")
        print("  1. AppKey 或 AppSecret 错误")
        print("  2. 应用未发布")
        print("  3. 网络问题")
        return False
    
    # 发送测试消息
    print("📤 发送测试消息...")
    success = send_interactive_card(agent_id, access_token)
    
    print()
    print("=" * 70)
    if success:
        print("✅ 测试完成！请在钉钉查看消息并点击按钮测试回调")
        print()
        print("📊 查看回调日志:")
        print("  tail -f /tmp/trade_callback_8901.log")
    else:
        print("❌ 测试失败，请检查配置和网络")
    print("=" * 70)
    
    return success

if __name__ == "__main__":
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / '.env')
    
    success = main()
    sys.exit(0 if success else 1)
