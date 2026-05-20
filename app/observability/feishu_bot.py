"""
飞书机器人告警推送器

使用机器人 Webhook 发送告警消息（不需要用户 ID）
"""
import requests
import hashlib
import hmac
import base64
import time
from typing import Dict, Any, Optional


class FeishuBotAlerter:
    """飞书机器人告警推送器 (Webhook 方式)"""
    
    def __init__(self, webhook_url: str, secret: Optional[str] = None):
        self.webhook_url = webhook_url
        self.secret = secret
    
    def _generate_sign(self, timestamp: int) -> str:
        """生成签名"""
        if not self.secret:
            return ""
        
        string_to_sign = f'{timestamp}\n{self.secret}'
        hmac_code = hmac.new(
            string_to_sign.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        sign = base64.b64encode(hmac_code).decode('utf-8')
        return sign
    
    def send_alert(self, level: str, title: str, content: str, **kwargs) -> bool:
        """发送告警消息"""
        try:
            # 根据级别设置颜色
            colors = {
                "INFO": "blue",
                "WARN": "orange",
                "CRITICAL": "red",
            }
            color = colors.get(level, "gray")
            
            # 构建消息内容
            text_lines = [
                {"tag": "text", "text": f"级别：{level}\n"},
                {"tag": "text", "text": f"{content}\n"}
            ]
            
            # 添加额外信息
            for key, value in kwargs.items():
                text_lines.append({"tag": "text", "text": f"{key}: {value}\n"})
            
            # 构建富文本消息
            content_obj = {
                "title": title,
                "content": [text_lines]
            }
            
            payload = {
                "msg_type": "post",
                "content": content_obj
            }
            
            # 如果有签名，添加时间戳和签名
            if self.secret:
                timestamp = str(int(time.time()))
                sign = self._generate_sign(int(timestamp))
                payload["timestamp"] = timestamp
                payload["sign"] = sign
            
            response = requests.post(self.webhook_url, json=payload, timeout=10)
            result = response.json()
            
            # 飞书 webhook 返回 code 为 0 表示成功
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                print(f"✅ 飞书推送成功：{title}")
                return True
            else:
                print(f"❌ 飞书推送失败：{result}")
                return False
        except Exception as e:
            print(f"飞书推送异常：{e}")
            return False
    
    def send_info(self, title: str, content: str, **kwargs) -> bool:
        """发送 INFO 级别告警"""
        return self.send_alert("INFO", title, content, **kwargs)
    
    def send_warn(self, title: str, content: str, **kwargs) -> bool:
        """发送 WARN 级别告警"""
        return self.send_alert("WARN", title, content, **kwargs)
    
    def send_critical(self, title: str, content: str, **kwargs) -> bool:
        """发送 CRITICAL 级别告警"""
        return self.send_alert("CRITICAL", title, content, **kwargs)


# 测试
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv(".env.us")
    
    # 使用 webhook 方式测试
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL")
    
    if webhook_url:
        alerter = FeishuBotAlerter(webhook_url)
        print("✅ 飞书机器人告警器初始化成功")
        
        # 测试发送消息
        result = alerter.send_info(
            title="FTSeek-US 测试",
            content="飞书机器人告警测试消息"
        )
        
        if result:
            print("✅ 测试推送成功")
        else:
            print("❌ 测试推送失败")
    else:
        print("❌ 缺少飞书 Webhook URL 配置")
        print("请在 .env.us 中设置 FEISHU_WEBHOOK_URL")
