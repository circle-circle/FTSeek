"""
飞书客户端 - 用于发送告警消息

使用 App ID 和 App Secret 进行身份验证
"""
import requests
import hashlib
import hmac
import base64
import time
from typing import Optional, Dict, Any


class FeishuClient:
    """飞书客户端"""
    
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
        self.token_expire_time = 0
    
    def get_access_token(self) -> Optional[str]:
        """获取访问令牌"""
        # 如果令牌未过期，直接返回
        if self.access_token and time.time() < self.token_expire_time:
            return self.access_token
        
        try:
            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            payload = {
                "app_id": self.app_id,
                "app_secret": self.app_secret
            }
            
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            
            if result.get("code") == 0:
                self.access_token = result["tenant_access_token"]
                # 令牌有效期 2 小时，提前 10 分钟刷新
                self.token_expire_time = time.time() + (7200 - 600)
                return self.access_token
            else:
                print(f"获取飞书令牌失败：{result}")
                return None
        except Exception as e:
            print(f"获取飞书令牌异常：{e}")
            return None
    
    def send_message(self, user_id: str, content: str, msg_type: str = "text") -> bool:
        """发送消息给用户"""
        token = self.get_access_token()
        if not token:
            return False
        
        try:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "receive_id": user_id,
                "msg_type": msg_type,
                "content": content
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            result = response.json()
            
            if result.get("code") == 0:
                return True
            else:
                print(f"发送飞书消息失败：{result}")
                return False
        except Exception as e:
            print(f"发送飞书消息异常：{e}")
            return False
    
    def send_text_message(self, user_id: str, text: str) -> bool:
        """发送文本消息"""
        content = f'{{"text": "{text}"}}'
        return self.send_message(user_id, content, "text")
    
    def send_post_message(self, user_id: str, title: str, content_lines: list) -> bool:
        """发送富文本消息"""
        content_obj = {
            "zh_cn": {
                "title": title,
                "content": content_lines
            }
        }
        return self.send_message(user_id, json.dumps(content_obj), "post")


# 测试
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv(".env.us")
    
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    
    if app_id and app_secret:
        client = FeishuClient(app_id, app_secret)
        print("✅ 飞书客户端初始化成功")
        
        # 测试获取令牌
        token = client.get_access_token()
        if token:
            print(f"✅ 获取令牌成功：{token[:20]}...")
        else:
            print("❌ 获取令牌失败")
    else:
        print("❌ 缺少飞书配置")
