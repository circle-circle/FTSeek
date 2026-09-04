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

    # DEB-20260903 #111: 熔断降级参数 — 连续失败达阈值后暂停通道, 冷却后半开恢复.
    DEGRADE_THRESHOLD = 3        # 连续失败次数阈值
    DEGRADE_RECOVER_SEC = 1800   # 熔断冷却 30min 后半开探测

    def __init__(self, channels: List, interval_sec: int = 3600):
        self._channels = channels
        self.interval_sec = interval_sec
        self._fail_count: Dict[str, int] = {}
        self._last_ok: Dict[str, float] = {}
        self._degraded: Dict[str, float] = {}  # DEB-20260903 #111: name → 熔断时刻
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
            # DEB-20260903 #111: 熔断冷却到期 → 半开探测 (恢复可用, 本轮 ping 验证,
            #   失败会重新降级). 置于可用性检查前, 确保探测真正发出.
            if ch.name in self._degraded and (time.time() - self._degraded[ch.name]) >= self.DEGRADE_RECOVER_SEC:
                logger.info(f"[PushHealth] {ch.name} 熔断冷却到期, 半开探测恢复")
                try:
                    ch._available = True
                except Exception as _e:
                    logger.debug(f"[PushHealth] {ch.name} 半开探测标记可用失败: {_e}")
                self._fail_count[ch.name] = 0
                del self._degraded[ch.name]
            try:
                if not ch.is_available():
                    continue
                ok = ch.send(msg)
                if ok:
                    self._fail_count[ch.name] = 0
                    self._last_ok[ch.name] = time.time()
                else:
                    self._fail_count[ch.name] = self._fail_count.get(ch.name, 0) + 1
                    if self._fail_count[ch.name] >= self.DEGRADE_THRESHOLD:
                        # DEB-20260903 #111: 达阈值执行熔断降级 (原仅 warning 无动作)
                        self._degrade(ch)
            except Exception as e:
                logger.debug(f"[PushHealth] ping {ch.name} 异常: {e}")

    def _degrade(self, ch):
        """熔断降级 (DEB-20260903 #111): 标记通道不可用 + 优先级 +50, 记录熔断时刻供冷却恢复.

        恢复条件: 冷却 DEGRADE_RECOVER_SEC 后半开探测, ping 成功即清零失败计数恢复.
        """
        if ch.name in self._degraded:
            return
        self._degraded[ch.name] = time.time()
        try:
            ch.priority = getattr(ch, "priority", 5) + 50  # 实例属性屏蔽类属性
        except Exception as _e:
            logger.debug(f"[PushHealth] {ch.name} 熔丝调优失败 (priority 只读?): {_e}")
        try:
            ch._available = False  # 熔丝: hub._immediate_send 的 is_available() 将跳过本通道
        except Exception as _e:
            logger.debug(f"[PushHealth] {ch.name} 熔丝标记不可用失败: {_e}")
        logger.error(
            f"[PushHealth] {ch.name} 连续失败 >= {self.DEGRADE_THRESHOLD} 次, "
            f"已熔断降级 (优先级+50, 标记不可用, {self.DEGRADE_RECOVER_SEC}s 后半开探测)")

    def get_status(self) -> Dict:
        return {
            "channels": [
                {
                    "name": ch.name,
                    "priority": ch.priority,
                    "available": ch.is_available(),
                    "fail_count": self._fail_count.get(ch.name, 0),
                    "last_ok": self._last_ok.get(ch.name, 0),
                    "degraded": ch.name in self._degraded,  # DEB-20260903 #111
                }
                for ch in self._channels
            ]
        }
