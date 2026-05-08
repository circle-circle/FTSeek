"""
Harness 消息传递模块

提供 Agent 间通信的消息和通道机制。
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("harness.messaging")

__all__ = [
    "MessageType",
    "Message",
    "MessageChannel",
    "MessageBus",
]


class MessageType(Enum):
    """消息类型"""

    REQUEST = "request"  # 请求
    RESPONSE = "response"  # 响应
    EVENT = "event"  # 事件
    ERROR = "error"  # 错误
    STATUS = "status"  # 状态更新


@dataclass
class Message:
    """
    Agent 间传递的消息

    Attributes:
        id: 消息唯一标识
        type: 消息类型
        sender: 发送者名称
        recipient: 接收者名称
        payload: 消息内容
        timestamp: 发送时间
        correlation_id: 关联 ID (用于请求 - 响应配对)
        metadata: 元数据
    """

    type: MessageType
    sender: str
    payload: Dict[str, Any]
    recipient: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "type": self.type.value,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "correlation_id": self.correlation_id,
            "metadata": self.metadata,
        }

    @classmethod
    def create_request(
        cls,
        sender: str,
        recipient: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
    ) -> "Message":
        """创建请求消息"""
        return cls(
            type=MessageType.REQUEST,
            sender=sender,
            recipient=recipient,
            payload=payload,
            correlation_id=correlation_id or str(uuid.uuid4()),
        )

    @classmethod
    def create_response(
        cls,
        sender: str,
        recipient: str,
        payload: Dict[str, Any],
        correlation_id: str,
    ) -> "Message":
        """创建响应消息"""
        return cls(
            type=MessageType.RESPONSE,
            sender=sender,
            recipient=recipient,
            payload=payload,
            correlation_id=correlation_id,
        )

    @classmethod
    def create_event(
        cls,
        sender: str,
        payload: Dict[str, Any],
        recipient: Optional[str] = None,
    ) -> "Message":
        """创建事件消息"""
        return cls(
            type=MessageType.EVENT,
            sender=sender,
            recipient=recipient,
            payload=payload,
        )


class MessageChannel:
    """
    消息通道

    支持发布/订阅模式的异步消息传递。
    """

    def __init__(self, name: str):
        """
        初始化消息通道

        Args:
            name: 通道名称
        """
        self.name = name
        self._subscribers: Dict[str, Callable[[Message], Any]] = {}
        self._queue: asyncio.Queue[Message] = asyncio.Queue()

    def subscribe(self, subscriber_id: str, callback: Callable[[Message], Any]) -> None:
        """
        订阅消息

        Args:
            subscriber_id: 订阅者 ID
            callback: 消息回调函数
        """
        self._subscribers[subscriber_id] = callback

    def unsubscribe(self, subscriber_id: str) -> None:
        """
        取消订阅

        Args:
            subscriber_id: 订阅者 ID
        """
        self._subscribers.pop(subscriber_id, None)

    async def publish(self, message: Message) -> None:
        """
        发布消息

        Args:
            message: 消息对象
        """
        # 放入队列
        await self._queue.put(message)

        # 通知所有订阅者
        for subscriber_id, callback in self._subscribers.items():
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as e:
                # 记录错误但不影响其他订阅者
                logger.error(f"Error notifying subscriber {subscriber_id}: {e}", exc_info=True)

    async def get(self, timeout: Optional[float] = None) -> Message:
        """
        获取消息

        Args:
            timeout: 超时时间 (秒)

        Returns:
            消息对象

        Raises:
            asyncio.TimeoutError: 超时时抛出
        """
        if timeout:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        return await self._queue.get()

    def qsize(self) -> int:
        """获取队列大小"""
        return self._queue.qsize()


class MessageBus:
    """
    消息总线

    管理多个消息通道，提供全局消息传递。
    """

    def __init__(self):
        """初始化消息总线"""
        self._channels: Dict[str, MessageChannel] = {}
        self._global_subscribers: Dict[str, Callable[[Message], Any]] = {}

    def get_channel(self, name: str) -> MessageChannel:
        """
        获取或创建通道

        Args:
            name: 通道名称

        Returns:
            消息通道
        """
        if name not in self._channels:
            self._channels[name] = MessageChannel(name)
        return self._channels[name]

    def subscribe_global(self, subscriber_id: str, callback: Callable[[Message], Any]) -> None:
        """
        订阅全局消息

        Args:
            subscriber_id: 订阅者 ID
            callback: 消息回调函数
        """
        self._global_subscribers[subscriber_id] = callback

    def unsubscribe_global(self, subscriber_id: str) -> None:
        """取消全局订阅"""
        self._global_subscribers.pop(subscriber_id, None)

    async def publish(self, message: Message, channel: Optional[str] = None) -> None:
        """
        发布消息

        Args:
            message: 消息对象
            channel: 通道名称 (可选)
        """
        # 通知全局订阅者
        for subscriber_id, callback in self._global_subscribers.items():
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as e:
                logger.error(f"Error notifying global subscriber {subscriber_id}: {e}", exc_info=True)

        # 发布到指定通道
        if channel and channel in self._channels:
            await self._channels[channel].publish(message)

    def list_channels(self) -> List[str]:
        """列出所有通道"""
        return list(self._channels.keys())


# 全局消息总线实例
_global_bus: Optional[MessageBus] = None


def get_message_bus() -> MessageBus:
    """获取全局消息总线"""
    global _global_bus
    if _global_bus is None:
        _global_bus = MessageBus()
    return _global_bus
