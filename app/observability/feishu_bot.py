"""
飞书机器人告警推送器

使用机器人 Webhook 发送告警消息（不需要用户 ID）
"""
import logging
import requests
import hashlib
import hmac
import base64
import time
from typing import Dict, Any, Optional

# B3 (2026-08-24, 体检 O1): print→logger 收口
logger = logging.getLogger("ftseek_us.feishu_bot")


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
            # 2026-08-26 实弹证伪+真修: 飞书自定义机器人"关键词"安全设置只扫描
            #   text 型消息 — post 富文本的 title 与 body 均不参与关键词匹配
            #   (首轮修在 post body 注入 [FTSeek-US] 实测仍 19024; text 型探针
            #   实测 code:0 放行)。改 text 型, 与 app/notifier/feishu.py send_text
            #   L169 五月以来工作正常的同款约定。排版: 首行级别+标题, 余下正文。
            lines = [f"[FTSeek-US] [{level}] {title}", content]
            for key, value in kwargs.items():
                lines.append(f"{key}: {value}")
            payload = {
                "msg_type": "text",
                "content": {"text": "\n".join(lines)},
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
                logger.info(f"✅ 飞书推送成功：{title}")
                return True
            else:
                logger.error(f"❌ 飞书推送失败：{result}")
                return False
        except Exception as e:
            logger.error(f"飞书推送异常：{e}")
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
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US")
    
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
        print("请在 .env.us 中设置 FEISHU_WEBHOOK_URL_US")
