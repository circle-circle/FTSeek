"""钉钉告警推送器"""
import hmac
import hashlib
import base64
import logging
import requests
import time
from typing import Dict, Any, Optional
from urllib.parse import quote_plus

# B3 (2026-08-24, 体检 O1): print→logger 收口
logger = logging.getLogger("ftseek_us.dingtalk_alert")


class DingTalkAlerter:
    """钉钉告警推送器"""
    
    def __init__(self, webhook_url: str, secret: str):
        self.webhook_url = webhook_url
        self.secret = secret
    
    def _generate_sign(self) -> str:
        """生成钉钉签名"""
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        
        hmac_code = hmac.new(
            secret_enc,
            string_to_sign_enc,
            digestmod=hashlib.sha256
        ).digest()
        
        sign = quote_plus(base64.b64encode(hmac_code))
        return f"&timestamp={timestamp}&sign={sign}"
    
    def format_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> Dict[str, Any]:
        """格式化告警消息"""
        # 根据级别设置颜色
        colors = {
            "INFO": "#00ff00",
            "WARN": "#ffa500",
            "CRITICAL": "#ff0000",
        }
        color = colors.get(level, "#000000")
        
        # Markdown 格式
        markdown = f"""## {title}

**级别**: <font color="{color}">{level}</font>

**内容**: {content}
"""
        
        # 添加额外字段
        for key, value in kwargs.items():
            markdown += f"\n**{key}**: {value}"
        
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown
            }
        }
    
    def send_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> bool:
        """发送告警"""
        try:
            message = self.format_alert(level, title, content, **kwargs)
            
            url = self.webhook_url
            if self.secret:
                url += self._generate_sign()
            
            response = requests.post(url, json=message, timeout=10)
            
            result = response.json()
            return result.get("errcode") == 0
        except Exception as e:
            logger.error(f"发送钉钉告警失败：{e}")
            return False
