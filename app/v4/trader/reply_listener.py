"""
ReplyListener - 钉钉回复监听器

监听钉钉消息回复，处理确认/忽略决策
"""
import time
import logging
import re
from typing import Dict, Optional, Any, Callable, List
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("ftseek.v4.reply_listener")
logger.propagate = False  # 防止日志重复


@dataclass
class ReplyMessage:
    """回复消息"""
    msg_id: str
    content: str
    sender_id: str
    timestamp: float
    matched_request_id: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "content": self.content,
            "sender_id": self.sender_id,
            "timestamp": datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            "matched_request_id": self.matched_request_id,
        }


class ReplyListener:
    """回复监听器"""
    
    def __init__(self):
        self.pending_confirmations: Dict[str, Dict[str, Any]] = {}
        self.received_replies: List[ReplyMessage] = []
        self.logger = logging.getLogger("ftseek.v4.reply_listener")
        self.logger.propagate = False  # 防止日志重复

        # 关键词匹配规则
        self.confirm_patterns = [
            r'确认.*买入',
            r'确认.*卖出',
            r'确认.*交易',
            r'确认',
            r'买入',
            r'卖出',
        ]
        
        self.ignore_patterns = [
            r'忽略',
            r'取消',
            r'放弃',
        ]
    
    def add_pending_confirmation(
        self,
        request_id: str,
        code: str,
        action: str,
        expire_time: float
    ):
        """
        添加待确认请求
        
        Args:
            request_id: 请求 ID
            code: 股票代码
            action: 买卖方向
            expire_time: 过期时间
        """
        self.pending_confirmations[request_id] = {
            "code": code,
            "action": action,
            "expire_time": expire_time,
            "created_at": time.time(),
            "status": "pending",  # pending/confirmed/ignored/expired
            "decision": None,
        }
        
        self.logger.info(
            f"添加待确认：{request_id} {code} {action}, "
            f"过期：{datetime.fromtimestamp(expire_time).strftime('%H:%M:%S')}"
        )
    
    def process_reply(
        self,
        content: str,
        sender_id: str = "user",
        msg_id: str = ""
    ) -> Optional[Dict[str, Any]]:
        """
        处理回复消息
        
        Args:
            content: 回复内容
            sender_id: 发送者 ID
            msg_id: 消息 ID
            
        Returns:
            匹配的决策数据
        """
        content = content.strip()
        timestamp = time.time()
        
        # 创建回复消息
        reply = ReplyMessage(
            msg_id=msg_id or f"msg_{int(timestamp)}",
            content=content,
            sender_id=sender_id,
            timestamp=timestamp
        )
        
        self.received_replies.append(reply)
        
        # 匹配待确认请求 (匹配最近的请求)
        for request_id, confirmation in list(self.pending_confirmations.items()):
            if confirmation["status"] != "pending":
                continue
            
            # 检查是否过期
            if timestamp > confirmation["expire_time"]:
                confirmation["status"] = "expired"
                confirmation["decision"] = "auto_expire"
                self.logger.info(f"请求过期：{request_id}")
                continue
            
            # 匹配关键词
            decision = self._match_decision(content, confirmation["action"])
            
            if decision:
                confirmation["status"] = "confirmed"
                confirmation["decision"] = decision
                reply.matched_request_id = request_id
                
                self.logger.info(
                    f"收到回复：{request_id} - {content} → {decision}"
                )
                
                return {
                    "request_id": request_id,
                    "code": confirmation["code"],
                    "action": confirmation["action"],
                    "decision": decision,
                    "reply_content": content,
                    "timestamp": timestamp,
                }
        
        return None
    
    def _match_decision(self, content: str, action: str) -> Optional[str]:
        """
        匹配决策
        
        Args:
            content: 回复内容
            action: 买卖方向
            
        Returns:
            "confirm" 或 "ignore" 或 None
        """
        content_lower = content.lower()
        
        # 匹配确认
        for pattern in self.confirm_patterns:
            if re.search(pattern, content_lower):
                return "confirm"
        
        # 匹配忽略
        for pattern in self.ignore_patterns:
            if re.search(pattern, content_lower):
                return "ignore"
        
        return None
    
    def check_expired(self) -> List[Dict[str, Any]]:
        """检查过期请求"""
        expired = []
        current_time = time.time()
        
        for request_id, confirmation in list(self.pending_confirmations.items()):
            if confirmation["status"] == "pending" and current_time > confirmation["expire_time"]:
                confirmation["status"] = "expired"
                confirmation["decision"] = "auto_expire"
                
                expired.append({
                    "request_id": request_id,
                    "code": confirmation["code"],
                    "action": confirmation["action"],
                    "decision": "auto_expire",
                })
                
                self.logger.info(f"请求过期：{request_id}")
        
        return expired
    
    def get_pending_count(self) -> int:
        """获取待确认数量"""
        return sum(
            1 for c in self.pending_confirmations.values()
            if c["status"] == "pending"
        )
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            "pending_count": self.get_pending_count(),
            "total_replies": len(self.received_replies),
            "pending_requests": {
                k: {
                    "code": v["code"],
                    "action": v["action"],
                    "status": v["status"],
                    "expire_time": datetime.fromtimestamp(v["expire_time"]).strftime("%H:%M:%S"),
                }
                for k, v in self.pending_confirmations.items()
            },
        }
    
    def cleanup(self, max_age: int = 3600):
        """清理过期数据"""
        current_time = time.time()
        
        # 清理过期的待确认
        expired = [
            k for k, v in self.pending_confirmations.items()
            if current_time - v["created_at"] > max_age
        ]
        
        for k in expired:
            del self.pending_confirmations[k]
        
        # 清理旧回复
        self.received_replies = [
            r for r in self.received_replies
            if current_time - r.timestamp < max_age
        ]
        
        if expired:
            self.logger.info(f"清理{len(expired)}个过期请求")


# 全局实例
reply_listener: Optional[ReplyListener] = None


def get_reply_listener() -> ReplyListener:
    """获取单例"""
    global reply_listener
    if reply_listener is None:
        reply_listener = ReplyListener()
    return reply_listener
