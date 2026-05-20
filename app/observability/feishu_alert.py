"""
飞书告警推送器

使用飞书机器人发送告警消息
"""
import os
import requests
import hashlib
import hmac
import base64
import time
from typing import Dict, Any, Optional


class FeishuAlerter:
    """飞书告警推送器"""
    
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
        self.token_expire_time = 0
    
    def get_access_token(self) -> Optional[str]:
        """获取访问令牌"""
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
                self.token_expire_time = time.time() + (7200 - 600)
                return self.access_token
            else:
                print(f"获取飞书令牌失败：{result}")
                return None
        except Exception as e:
            print(f"获取飞书令牌异常：{e}")
            return None
    
    def send_alert(self, level: str, title: str, content: str, **kwargs) -> bool:
        """发送告警消息"""
        token = self.get_access_token()
        if not token:
            return False
        
        try:
            # receive_id_type 作为查询参数
            receive_id_type = kwargs.get("receive_id_type", "chat_id")
            receive_id = kwargs.get("receive_id", os.getenv("FEISHU_CHAT_ID", ""))
            url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            # 根据级别设置颜色
            colors = {
                "INFO": "blue",
                "WARN": "orange",
                "CRITICAL": "red",
            }
            color = colors.get(level, "gray")
            
            # 构建富文本消息内容
            content_lines = [
                [{"tag": "text", "text": f"级别：{level}\n"}],
                [{"tag": "text", "text": f"{content}\n"}]
            ]
            
            # 添加额外信息
            for key, value in kwargs.items():
                if key not in ("receive_id_type", "receive_id"):
                    content_lines.append([{"tag": "text", "text": f"{key}: {value}\n"}])
            
            payload = {
                "receive_id": receive_id,
                "msg_type": "post",
                "content": {
                    "zh_cn": {
                        "title": title,
                        "content": content_lines
                    }
                }
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            result = response.json()
            
            if result.get("code") == 0:
                print(f"✅ 飞书推送成功：{title}")
                return True
            else:
                print(f"❌ 飞书推送失败：{result}")
                return False
        except Exception as e:
            print(f"飞书推送异常：{e}")
            return False

    def format_alert(self, level: str, title: str, content: str, **kwargs) -> Dict[str, Any]:
        """格式化告警消息 (卡片格式)"""
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"[{level}] {title}"
                    },
                    "template": self._get_color(level)
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": content
                        }
                    }
                ]
            }
        }

    def _get_color(self, level: str) -> str:
        """获取颜色"""
        colors = {
            "INFO": "blue",
            "WARN": "yellow",
            "CRITICAL": "red"
        }
        return colors.get(level, "grey")
    
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
    
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    
    if app_id and app_secret:
        alerter = FeishuAlerter(app_id, app_secret)
        print("✅ 飞书告警器初始化成功")
        
        # 测试发送消息
        result = alerter.send_info(
            title="FTSeek-US 测试",
            content="飞书告警测试消息"
        )
        
        if result:
            print("✅ 测试推送成功")
        else:
            print("❌ 测试推送失败")
    else:
        print("❌ 缺少飞书配置")
