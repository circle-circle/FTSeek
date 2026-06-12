"""
主线程 Watchdog (P2-8 2026-05-14)

监控 scheduler 各主循环的心跳, 检测死锁/卡死.

机制:
- 每个主循环 (scan_loop, monitor_loop) 调 ping("scan_loop") 标记心跳
- watchdog 后台线程每 60s 检查所有心跳, 超 stale_threshold 的 → 告警
- 默认 stale_threshold = monitor_interval * 5 (主线程预期周期的 5 倍)
- 告警走 critical_alerts 多通道
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class Watchdog:
    def __init__(self):
        self._heartbeats: Dict[str, float] = {}
        self._thresholds: Dict[str, float] = {}  # name -> seconds
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._alerted: Dict[str, float] = {}  # name -> last alert ts (避免刷屏)
        self._alert_cooldown = 600  # 10min

    def register(self, name: str, stale_threshold_sec: float):
        """注册一个心跳源"""
        with self._lock:
            self._thresholds[name] = stale_threshold_sec
            self._heartbeats[name] = time.time()
        logger.info(f"[Watchdog] 注册心跳: {name} threshold={stale_threshold_sec}s")

    def ping(self, name: str):
        """心跳更新 (线程安全, 无锁短路径)"""
        self._heartbeats[name] = time.time()

    def auto_ping_when_alive(self, name: str, check_fn, interval_sec: float = 30.0):
        """
        启动后台 ping 线程, 当 check_fn() True 时自动 ping.
        用于解决主循环内部阻塞导致 ping 不及时的问题.

        2026-05-15: 基于昨日 scan_loop 1802s 卡死告警 (实际只是 inner await 慢)
        """
        def _loop():
            while self._running:
                try:
                    if check_fn():
                        self.ping(name)
                except Exception:
                    pass
                time.sleep(interval_sec)

        t = threading.Thread(target=_loop, daemon=True, name=f"AutoPing-{name}")
        t.start()

    def start(self, check_interval_sec: float = 60.0):
        """启动后台 watchdog"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, args=(check_interval_sec,),
            daemon=True, name="Watchdog",
        )
        self._thread.start()
        logger.info(f"[Watchdog] 启动 — 检查间隔 {check_interval_sec}s")

    def stop(self):
        self._running = False

    def _loop(self, check_interval: float):
        while self._running:
            try:
                self._check_all()
            except Exception as e:
                logger.error(f"[Watchdog·loop] 异常: {e}")
            time.sleep(check_interval)

    def _check_all(self):
        now = time.time()
        with self._lock:
            items = list(self._heartbeats.items())
            thresholds = dict(self._thresholds)
        for name, last_ts in items:
            threshold = thresholds.get(name, 600)
            elapsed = now - last_ts
            if elapsed > threshold:
                # 告警 cooldown
                last_alert = self._alerted.get(name, 0)
                if now - last_alert < self._alert_cooldown:
                    continue
                self._alerted[name] = now
                self._alert_stale(name, elapsed, threshold)

    def _alert_stale(self, name: str, elapsed: float, threshold: float):
        msg = f"⚠️ Watchdog: 主循环 {name} 已 {elapsed:.0f}s 无心跳 (阈值 {threshold:.0f}s)"
        logger.error(msg)
        try:
            from app.utils.critical_alerts import send_critical
            send_critical(msg, async_mode=True)
        except Exception:
            pass

    def get_status(self) -> Dict[str, Dict]:
        now = time.time()
        with self._lock:
            return {
                name: {
                    "last_ping_sec_ago": round(now - ts, 1),
                    "threshold_sec": self._thresholds.get(name, 600),
                    "is_stale": (now - ts) > self._thresholds.get(name, 600),
                }
                for name, ts in self._heartbeats.items()
            }


_singleton: Optional[Watchdog] = None


def get_watchdog() -> Watchdog:
    global _singleton
    if _singleton is None:
        _singleton = Watchdog()
    return _singleton
