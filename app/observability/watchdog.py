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
        # P1-5 (2026-06-12) self-heal
        self._heal_multiplier = 3.0
        self._heal_history: list = []
        self._heal_max_per_day = 3
        self._heal_enabled = True
        self._heal_last_attempt: float = 0
        # RFC-060 (2026-06-22): 初始化 grace window
        # monitor_loop / scan_loop 线程在主初始化完成后才 start, 期间主线程可能被
        # 飞书 SDK / OpenD 连接等同步调用阻塞 (实测 06-19 Juneteenth 卡 104s).
        # watchdog 检查线程先于工作线程启动 → 误判 stale → systemctl restart 循环.
        # start() 后 INIT_GRACE_SEC 内不报 stale, 给初始化 + 首次 ping 留时间.
        self._started_at: float = 0.0
        self._init_grace_sec = 300.0

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
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._loop, args=(check_interval_sec,),
            daemon=True, name="Watchdog",
        )
        self._thread.start()
        logger.info(f"[Watchdog] 启动 — 检查间隔 {check_interval_sec}s (init grace {self._init_grace_sec:.0f}s)")

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
        # 假日/周末: 跳过所有心跳检查 (后台线程故意不启动，无心跳是正常的)
        # get_market_phase() 已把假日 (is_us_holiday) 归为 WEEKEND.
        try:
            from app.core.scheduler_triggers import get_market_phase, MarketPhase
            if get_market_phase() == MarketPhase.WEEKEND:
                return
        except Exception:
            pass  # 导入失败时正常检查

        # RFC-060 (2026-06-22): 初始化 grace window
        # 工作线程 (monitor_loop/scan_loop) 在主初始化完成后才 .start(), 期间主线程
        # 可能被飞书 SDK / OpenD 同步调用阻塞 (06-19 Juneteenth 实测卡 104s).
        # watchdog 检查线程先于工作线程启动 → 误判 stale → systemctl restart 循环.
        # start() 后 grace 内不报 stale, 给初始化 + 首次 ping 留时间.
        if self._started_at and (time.time() - self._started_at) < self._init_grace_sec:
            return

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
        if self._heal_enabled and elapsed > threshold * self._heal_multiplier:
            self._self_heal(name, elapsed, threshold)

    def _self_heal(self, name, elapsed, threshold):
        """P1-5 (2026-06-12) systemctl restart ftseek-us when stale > threshold*3, rate-limited."""
        import os
        import time as _t
        if os.environ.get("WATCHDOG_AUTO_HEAL", "1") == "0":
            logger.warning("[Watchdog.heal] skip: WATCHDOG_AUTO_HEAL=0")
            return
        # 2026-11-03 (全链审计 P1-3): **盘中不做自愈重启**。
        #   llm_strategy_loop 的心跳只由主循环**轮首**推进 ⇒ 盘中任一同步长步骤
        #   (定桶实测 31.4min = 1884s > 阈值 600s × 3 = 1800s) 就会被判成"主循环卡死"
        #   ⇒ 旧实现直接 `systemctl restart ftseek-us` ⇒ **盘中重启 = 内存态持仓与在途委托
        #   全丢** (比慢一轮更糟)。真冻结由**外部** freeze_sentinel (cron, 每分钟) 负责
        #   SIGKILL (DEB-20261023-freeze-fast-lane), 覆盖不受影响。
        #   盘中改为: 只告警 + 通知值班, 不重启。
        try:
            from app.core.scheduler_triggers import get_market_phase, MarketPhase
            if get_market_phase() == MarketPhase.TRADING:
                logger.error(
                    "[Watchdog.heal] 盘中不自动重启 (长步骤≠卡死; 内存态持仓/在途委托会丢) — "
                    "已告警, 真冻结交外部 freeze_sentinel 处置: %s stale %.0fs",
                    name, elapsed)
                try:
                    from app.utils.critical_alerts import send_critical
                    send_critical(
                        f"Watchdog: {name} 无心跳 {elapsed:.0f}s, 盘中未自动重启 "
                        f"(长步骤非卡死), 请人工确认; 真冻结由 freeze_sentinel 处置",
                        async_mode=True)
                except Exception as _ce:
                    # 通知失败必须留痕 (R5; except:pass 棘轮) —— 这条告警本身就是
                    # "盘中不自动重启" 的唯一通知渠道, 静默失败等于值班人不知情。
                    logger.warning("[Watchdog.heal] 盘中不重启告警发送失败: %s", _ce, exc_info=True)
                return
        except Exception as _ph_err:
            # 阶段判定失败 ⇒ 回落"按原逻辑"(可能重启)。这是安全相关的判断 ⇒ 必须可见
            # (R5; W4 debug-swallow 棘轮)。
            logger.warning("[Watchdog.heal] 阶段判定失败, 继续按原逻辑: %s", _ph_err, exc_info=True)
        now = _t.time()
        if now - self._heal_last_attempt < 60:
            return
        cutoff = now - 86400
        self._heal_history = [t for t in self._heal_history if t > cutoff]
        if len(self._heal_history) >= self._heal_max_per_day:
            logger.error(f"[Watchdog.heal] {name} reached daily limit, manual intervention required")
            try:
                from app.utils.critical_alerts import send_critical
                send_critical(f"Watchdog heal exhausted: {name} restarted {len(self._heal_history)}x in 24h, manual intervention required", async_mode=True)
            except Exception:
                pass
            return
        self._heal_last_attempt = now
        self._heal_history.append(now)
        logger.error(f"[Watchdog.heal] restart: {name} stale {elapsed:.0f}s ({len(self._heal_history)}/{self._heal_max_per_day} today)")
        try:
            from app.utils.critical_alerts import send_critical
            send_critical(f"Watchdog auto-heal restart ftseek-us: {name} stale {elapsed:.0f}s ({len(self._heal_history)}/{self._heal_max_per_day})", async_mode=True)
        except Exception:
            pass
        def _restart():
            import subprocess
            try:
                subprocess.run(["systemctl", "restart", "ftseek-us"], capture_output=True, text=True, timeout=30)
            except Exception as e:
                logger.error(f"[Watchdog.heal] systemctl failed: {e}")
        threading.Thread(target=_restart, daemon=True, name="WatchdogHeal").start()

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
