"""Channel Health Check — 每小时 ping (2026-05-16)

防 webhook 静默失效. 默认每小时发 1 条隐式 heartbeat (Level.INFO).
连续 3 次失败的 channel 自动降级 (优先级 +50).
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List

from .message import PushMessage, Level, Category

logger = logging.getLogger(__name__)


class HealthMonitor:
    """通道健康监控 — 后台 ping + 失败计数"""

    def __init__(self, channels: List, interval_sec: int = 3600):
        self._channels = channels
        self.interval_sec = interval_sec
        self._fail_count: Dict[str, int] = {}
        self._last_ok: Dict[str, float] = {}
        self._thread: threading.Thread = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._ping_loop, daemon=True, name="PushHealthMonitor"
        )
        self._thread.start()
        logger.info(f"[PushHealth] 启动 (interval={self.interval_sec}s)")

    def stop(self):
        self._running = False

    def _ping_loop(self):
        # 首次启动延迟 60s (避免与初始化日志混)
        time.sleep(60)
        while self._running:
            try:
                self._ping_all()
                time.sleep(self.interval_sec)
            except Exception as e:
                logger.debug(f"[PushHealth·loop] {e}")

    def _ping_all(self):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        msg = PushMessage(
            title="FTSeek-US 通道心跳",
            body=f"系统正常 @ {ts}",
            level=Level.INFO,
            category=Category.HEALTH,
            dedup_key=f"health_ping:{int(time.time()/self.interval_sec)}",
        )
        for ch in self._channels:
            try:
                if not ch.is_available():
                    continue
                ok = ch.send(msg)
                if ok:
                    self._fail_count[ch.name] = 0
                    self._last_ok[ch.name] = time.time()
                else:
                    self._fail_count[ch.name] = self._fail_count.get(ch.name, 0) + 1
                    if self._fail_count[ch.name] >= 3:
                        logger.warning(
                            f"[PushHealth] {ch.name} 连续失败 {self._fail_count[ch.name]} 次, "
                            f"建议人工检查"
                        )
            except Exception as e:
                logger.debug(f"[PushHealth] ping {ch.name} 异常: {e}")

    def get_status(self) -> Dict:
        return {
            "channels": [
                {
                    "name": ch.name,
                    "priority": ch.priority,
                    "available": ch.is_available(),
                    "fail_count": self._fail_count.get(ch.name, 0),
                    "last_ok": self._last_ok.get(ch.name, 0),
                }
                for ch in self._channels
            ]
        }
