#!/usr/bin/env python3
"""
获取飞书会话 ID (Chat ID)

使用方法:
1. 确保已配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET
2. 运行此脚本
3. 获取与 ftseek-us agent 绑定的会话 ID
"""
import os
import sys
import requests
import json

# 配置
app_id = "cli_a94874620b38dbc8"
app_secret = "22BzYqAYzOctHEsoNG53XfZJUDDYzIQX"

def get_access_token():
    """获取 access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": app_id,
        "app_secret": app_secret
    }
    
    response = requests.post(url, json=payload)
    result = response.json()
    
    if result.get("code") == 0:
        return result.get("tenant_access_token")
    else:
        print(f"❌ 获取 token 失败：{result}")
        return None

def get_bot_info(token):
    """获取机器人信息"""
    url = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # 获取应用信息
    url = "https://open.feishu.cn/open-apis/application/v6/applications/current"
    response = requests.get(url, headers=headers)
    result = response.json()
    
    if result.get("code") == 0:
        print(f"✅ 应用信息:")
        print(f"   名称：{result.get('data', {}).get('name')}")
        print(f"   App ID: {result.get('data', {}).get('app_id')}")
        return result.get('data')
    else:
        print(f"❌ 获取应用信息失败：{result}")
        return None

def main():
    print("=" * 60)
    print("获取飞书会话 ID")
    print("=" * 60)
    print()
    
    # 获取 token
    print("【1】获取 access_token...")
    token = get_access_token()
    if not token:
        sys.exit(1)
    print(f"✅ Token 获取成功：{token[:20]}...")
    print()
    
    # 获取机器人信息
    print("【2】获取机器人信息...")
    bot_info = get_bot_info(token)
    if not bot_info:
        sys.exit(1)
    print()
    
    # 说明
    print("=" * 60)
    print("【获取会话 ID 的方法】")
    print("=" * 60)
    print()
    print("方法 1: 从飞书群聊获取")
    print("  1. 打开飞书群聊")
    print("  2. 点击右上角设置")
    print("  3. 查看群聊信息")
    print("  4. 复制 '群聊 ID' (chat_id)")
    print()
    print("方法 2: 使用 API 获取")
    print("  访问：https://open.feishu.cn/document/ukTMukTMukTM/uEjNwUjL2YDM14SM2ATN")
    print()
    print("方法 3: 从消息中获取")
    print("  发送消息到群聊后，从回调中获取 chat_id")
    print()
    print("=" * 60)
    print()
    print("获取到 chat_id 后，请在 .env.us 中配置:")
    print(f"  FEISHU_CHAT_ID=your_chat_id_here")
    print()

if __name__ == "__main__":
    main()
