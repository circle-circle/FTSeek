#!/usr/bin/env python3
"""
飞书推送服务 - 使用官方 SDK
"""
import os
import json
import logging
from typing import Optional, List

logger = logging.getLogger("ftseek_us.feishu_push")

# 导入 SDK
SDK_AVAILABLE = False
try:
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    SDK_AVAILABLE = True
    logger.info("✅ 飞书 SDK 已加载")
except Exception as e:
    logger.warning(f"⚠️ 飞书 SDK 导入失败：{type(e).__name__}: {e}")


class FeishuChatService:
    """飞书会话推送服务"""
    
    def __init__(self, app_id: str, app_secret: str, chat_id: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        
        self.client = Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .build()
        
        logger.info(f"✅ 飞书 SDK 客户端已初始化")
    
    def send_text(self, text: str) -> bool:
        """发送纯文本消息（兼容 critical_alerts 调用约定）
        
        使用 Webhook + text 类型发送，避免 SDK post 类型的 msg_type 校验问题。
        Webhook 要求消息包含关键词 "[FTSeek-US]"。
        """
        if not SDK_AVAILABLE:
            # 回退到 webhook 方式
            return self._send_via_webhook(text)
        
        # 优先尝试 webhook（已验证稳定）
        return self._send_via_webhook(text)

    def _send_via_webhook(self, text: str) -> bool:
        """通过 Webhook 发送文本消息（绕过 SDK post 类型的 msg_type 问题）"""
        import requests
        webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US")
        if not webhook_url:
            logger.error("❌ FEISHU_WEBHOOK_URL_US 未配置")
            return False
        
        try:
            # 飞书自定义机器人 Webhook 要求消息包含关键词
            content = text if text.startswith("[FTSeek-US]") else f"[FTSeek-US] {text}"
            payload = {
                "msg_type": "text",
                "content": {"text": content}
            }
            headers = {"Content-Type": "application/json"}
            resp = requests.post(webhook_url, json=payload, headers=headers, timeout=10)
            result = resp.json()
            
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                logger.info("✅ 飞书 Webhook 发送成功")
                return True
            else:
                logger.error(f"❌ 飞书 Webhook 发送失败：{result}")
                return False
        except Exception as e:
            logger.error(f"飞书 Webhook 发送异常：{e}")
            return False

    def send_post_message(self, title: str, content_lines: List[str]) -> bool:
        """发送富文本消息"""
        if not SDK_AVAILABLE:
            logger.error("❌ SDK 未加载")
            return False
        
        try:
            content_obj = {
                "zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": line + "\n"}] for line in content_lines]
                }
            }
            
            body_builder = CreateMessageRequestBody.builder()
            body_builder.receive_id(self.chat_id)
            body_builder.msg_type("post")
            body_builder.content(json.dumps(content_obj))
            
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body_builder.build()) \
                .build()
            
            response = self.client.im.v1.message.create(request)
            
            if response.code == 0:
                logger.info(f"✅ 飞书消息发送成功")
                return True
            else:
                logger.error(f"❌ 飞书消息发送失败：{response.msg}")
                return False
        except Exception as e:
            logger.error(f"飞书消息发送异常：{e}")
            return False


class FeishuWebhookOnlyService:
    """DEB-20260904 #205: 仅 Webhook 的降级服务 —
    SDK/APP 凭据缺失但 FEISHU_WEBHOOK_URL_US 已配置时,
    复盘/告警仍可通过自定义机器人 webhook 送达 (text 类型)."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_text(self, text: str) -> bool:
        import requests
        try:
            content = text if text.startswith("[FTSeek-US]") else f"[FTSeek-US] {text}"
            payload = {"msg_type": "text", "content": {"text": content}}
            resp = requests.post(
                self.webhook_url, json=payload,
                headers={"Content-Type": "application/json"}, timeout=10,
            )
            result = resp.json()
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                return True
            logger.error(f"❌ 飞书 Webhook 发送失败：{result}")
            return False
        except Exception as e:
            logger.error(f"飞书 Webhook 发送异常：{e}")
            return False

    def send_post_message(self, title: str, content_lines: List[str]) -> bool:
        return self.send_text("\n".join([title, *content_lines]))


_service = None

def get_feishu_push_service() -> Optional[FeishuChatService]:
    """获取飞书推送服务"""
    global _service
    
    if _service is None:
        app_id = os.getenv("FEISHU_APP_ID")
        app_secret = os.getenv("FEISHU_APP_SECRET")
        chat_id = os.getenv("FEISHU_CHAT_ID")
        webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US")

        # DEB-20260904 #205: SDK/APP 凭据缺失时降级 webhook 服务 —
        # 原直接 return None, 架空 send_text 已有的 webhook 通道,
        # 复盘/告警推送在 webhook 可用时仍整体静默丢失
        if not app_id or not app_secret or not chat_id or not SDK_AVAILABLE:
            if webhook_url:
                logger.warning("⚠️ 飞书 SDK/APP 凭据不可用, 降级 Webhook 服务")
                _service = FeishuWebhookOnlyService(webhook_url)
                return _service
            logger.warning("⚠️ 飞书配置不完整 (SDK/凭据与 Webhook 均不可用)")
            return None

        _service = FeishuChatService(app_id, app_secret, chat_id)
        logger.info(f"✅ 飞书推送服务已初始化")
        return _service
    
    return _service
