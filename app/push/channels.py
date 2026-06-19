"""Channel 抽象 + 现有 notifier 包装 (2026-05-16)

不删除 app/notifier/{feishu,dingtalk}.py — 仅 wrap 它们作为 Channel 实现.
新增 FlagFileChannel 作为最后兜底 (写 critical_alerts.flag).
"""
from __future__ import annotations
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .message import PushMessage, Level

logger = logging.getLogger(__name__)


class Channel(ABC):
    """推送通道抽象 — 单一职责: send(text) → bool"""
    name: str = "?"
    priority: int = 5  # 1=主, 10=兜底

    @abstractmethod
    def send(self, msg: PushMessage) -> bool:
        """发送消息. 失败返回 False, 上层会 fallback 到下一通道."""
        ...

    def is_available(self) -> bool:
        """检查通道是否可用 (例: webhook 配置非空)"""
        return True


class DingTalkChannel(Channel):
    """DingTalk webhook (主通道)"""
    name = "dingtalk"
    priority = 1

    def __init__(self):
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            self._notifier = DingTalkNotifier()
            self._available = bool(self._notifier.enabled)
        except Exception as e:
            logger.warning(f"[PushChannel·dingtalk] 初始化失败: {e}")
            self._notifier = None
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def send(self, msg: PushMessage) -> bool:
        if not self._available or self._notifier is None:
            return False
        try:
            return bool(self._notifier.send_text(msg.render_text()))
        except Exception as e:
            logger.warning(f"[PushChannel·dingtalk·send] 失败: {e}")
            return False


class FeishuChannel(Channel):
    """Feishu webhook (备用通道)"""
    name = "feishu"
    priority = 2

    def __init__(self):
        try:
            from app.notifier.feishu import FeishuNotifier
            self._notifier = FeishuNotifier()
            # FeishuNotifier 自身判断 enabled
            self._available = bool(getattr(self._notifier, "webhook_url", None)
                                   or os.getenv("FEISHU_WEBHOOK_URL_US"))
        except Exception as e:
            logger.warning(f"[PushChannel·feishu] 初始化失败: {e}")
            self._notifier = None
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def send(self, msg: PushMessage) -> bool:
        if not self._available or self._notifier is None:
            return False
        try:
            return bool(self._notifier.send_text(msg.render_text()))
        except Exception as e:
            logger.warning(f"[PushChannel·feishu·send] 失败: {e}")
            return False


class FlagFileChannel(Channel):
    """本地 flag 文件兜底 (所有 webhook 失败时写, 防告警丢失)"""
    name = "flag_file"
    priority = 99  # 最低

    def __init__(self, flag_path: Optional[str] = None):
        if flag_path is None:
            try:
                from app.core.paths import get_data_dir
                flag_path = str(Path(get_data_dir()) / "critical_alerts.flag")
            except Exception:
                flag_path = str(Path(__file__).resolve().parent.parent.parent /
                              "data" / "critical_alerts.flag")
        self._flag_path = flag_path
        os.makedirs(os.path.dirname(flag_path), exist_ok=True)

    def send(self, msg: PushMessage) -> bool:
        # 仅高优先级才写 flag (避免噪音)
        if msg.level not in (Level.HIGH, Level.CRITICAL):
            return True
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._flag_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {ts} [{msg.level.value}] {msg.category.value} ---\n")
                f.write(msg.render_text())
                f.write("\n")
            return True
        except Exception as e:
            logger.error(f"[FlagFileChannel] 写文件失败: {e}")
            return False
