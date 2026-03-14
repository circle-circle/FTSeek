"""
钉钉群机器人推送
支持 ActionCard 消息，点击按钮直接触发确认回调
"""
import time
import hmac
import hashlib
import base64
import json
import logging
import urllib.parse
import requests
from typing import Optional

from app import config
from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.notifier.dingtalk")

ACTION_LABELS = {
    SignalAction.BUY: "确认买入",
    SignalAction.SELL: "确认卖出",
    SignalAction.ALERT: "查看详情",
}

ACTION_COLORS = {
    SignalAction.BUY: "#FF4444",
    SignalAction.SELL: "#44BB44",
    SignalAction.ALERT: "#4488FF",
}


class DingTalkNotifier:
    """钉钉Webhook推送"""

    def __init__(self, webhook_url: str = None, secret: str = None):
        self.webhook_url = webhook_url or config.DINGTALK_WEBHOOK_URL
        self.secret = secret or config.DINGTALK_SECRET

    def _sign_url(self) -> str:
        """加签方式鉴权"""
        if not self.secret:
            return self.webhook_url
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

    def send_signal(self, signal: Signal) -> bool:
        """
        发送交易信号到钉钉，使用 ActionCard 格式
        包含 "确认执行" 按钮，点击后回调 FastAPI 服务
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return False

        action_text = ACTION_LABELS.get(signal.action, "查看")
        confirm_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/confirm"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )
        reject_url = (
            f"{config.CALLBACK_EXTERNAL_URL}/api/reject"
            f"?signal_id={signal.id}&token={config.API_TOKEN}"
        )

        title = f"{'🔴' if signal.action == SignalAction.BUY else '🟢' if signal.action == SignalAction.SELL else '🔵'} {signal.code} {signal.action.value} 信号"

        currency = "$" if signal.code.startswith("US.") else "HK$"

        markdown_text = (
            f"### {title}\n\n"
            f"**标的**: {signal.code} {signal.name}\n\n"
            f"**方向**: {signal.action.value}\n\n"
            f"**触发价**: {currency}{signal.price:.2f}\n\n"
            f"**数量**: {signal.quantity} 股\n\n"
            f"**策略**: {signal.strategy}\n\n"
            f"**原因**: {signal.reason}\n\n"
            f"---\n\n"
            f"⏰ 有效期 {config.SIGNAL_EXPIRE_SECONDS // 60} 分钟，过期自动作废"
        )

        # 使用独立跳转 ActionCard (双按钮)
        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": title,
                "text": markdown_text,
                "btnOrientation": "1",  # 横向排列
                "btns": [
                    {"title": f"✅ {action_text}", "actionURL": confirm_url},
                    {"title": "❌ 忽略", "actionURL": reject_url},
                ],
            },
        }

        try:
            url = self._sign_url()
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info(f"钉钉推送成功: {signal.id} -> {signal.code} {signal.action.value}")
                return True
            else:
                logger.error(f"钉钉推送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"钉钉推送异常: {e}")
            return False

    def send_text(self, content: str, at_all: bool = False) -> bool:
        """发送纯文本消息"""
        if not self.webhook_url:
            return False
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"isAtAll": at_all},
        }
        try:
            url = self._sign_url()
            resp = requests.post(url, json=payload, timeout=10)
            return resp.json().get("errcode") == 0
        except Exception as e:
            logger.error(f"钉钉文本消息发送失败: {e}")
            return False

    def send_execution_result(self, signal: Signal, success: bool, detail: str = ""):
        """推送交易执行结果"""
        status = "✅ 执行成功" if success else "❌ 执行失败"
        currency = "$" if signal.code.startswith("US.") else "HK$"
        content = (
            f"[交易结果] {status}\n"
            f"标的: {signal.code}\n"
            f"方向: {signal.action.value}\n"
            f"价格: {currency}{signal.price:.2f}\n"
            f"数量: {signal.quantity}\n"
        )
        if detail:
            content += f"详情: {detail}\n"
        self.send_text(content)
