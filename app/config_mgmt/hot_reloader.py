"""
FTSeek-US 配置热重载器 — Layer 3: 文件监控

功能:
1. 监控 unified_config.yaml 文件修改时间
2. 检测到变化后自动重新加载配置
3. 冷却机制防止重复重载 (默认 60 秒)
4. 重载失败自动回退到旧配置
"""

import os
import time
import logging
import threading
from typing import Optional, Callable
from pathlib import Path

logger = logging.getLogger("ftseek_us.config_mgmt.hot_reloader")


class ConfigHotReloader:
    """
    配置热重载器

    监控配置文件变化并自动重新加载。
    """

    def __init__(
        self,
        config_loader,
        cooldown_seconds: int = 60,
        watch_files: list = None,
        post_reload_callback: Callable = None,
    ):
        """
        Args:
            config_loader: ConfigLoader 实例
            cooldown_seconds: 重载冷却时间 (秒)
            watch_files: 额外监控的文件列表
            post_reload_callback: 热重载成功后的回调 (用于同步 ConfigContainer 等下游消费者)
        """
        self.config = config_loader
        self.cooldown_seconds = cooldown_seconds
        self._post_reload_callback = post_reload_callback

        # 监控文件
        config_path = getattr(config_loader, '_config_path', None)
        self.watch_files = list(watch_files) if watch_files else []
        if config_path:
            self.watch_files.insert(0, str(config_path))

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_mtime = {}  # {path: mtime}
        self._last_reload_time = 0.0
        self._reload_count = 0
        self._error_count = 0
        # 2026-08-27 审计⑧: 检测/动作解耦 — 冷却期变更挂起补载, 变更文件名暂存去噪
        self._pending_reload = False
        self._changed_files = set()

        # 初始化 mtime
        for f in self.watch_files:
            try:
                self._last_mtime[f] = os.path.getmtime(f)
            except OSError:
                self._last_mtime[f] = 0

    def start(self):
        """启动后台监控线程"""
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="ConfigHotReloader", daemon=True
        )
        self._thread.start()
        logger.info(
            "[HotReloader] 启动 (cooldown=%ds, files=%d)",
            self.cooldown_seconds, len(self.watch_files)
        )

    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(
            "[HotReloader] 已停止 | 重载=%d 次 | 错误=%d 次",
            self._reload_count, self._error_count
        )

    def _check_files(self) -> bool:
        """检查是否有文件变化 (2026-08-27 审计⑧ 重构: 纯检测, 不打日志).

        旧实现每检测到 mtime 变化就打一行 INFO —— 但 _reload 冷却期内会提前 return
        且不更新 _last_mtime, 于是同一变化在 60s 冷却窗内每秒重复触发 + 刷屏
        (08-27 部署时 05:20→05:23 实测 30+ 行). 检测与日志/动作解耦: 这里只判"变了没"."""
        changed = False
        for f in self.watch_files:
            try:
                mtime = os.path.getmtime(f)
                # 写入防抖: 忽略 mtime 距现在 <2s 的变化, 避免读到编辑器/cp/rsync 半写文件
                if mtime != self._last_mtime.get(f, 0):
                    if time.time() - mtime < 2.0:
                        continue
                    self._changed_files.add(os.path.basename(f))
                    changed = True
            except OSError:
                continue
        return changed

    def _reload(self):
        """重新加载配置 (带冷却; 冷却期内到达的变更挂起, 冷却结束补载, 不丢不刷)"""
        now = time.time()
        names = ", ".join(sorted(self._changed_files)) or "config"
        # 冷却检查: 不丢弃变更 — 标记挂起并消费本次检测, 冷却结束后由 _loop 补触发
        if now - self._last_reload_time < self.cooldown_seconds:
            self._pending_reload = True
            self._consume_detected()
            logger.debug(
                "[HotReloader] 检测到变化 %s 但在冷却期 (剩 %.0fs), 挂起补载",
                names, self.cooldown_seconds - (now - self._last_reload_time)
            )
            return

        logger.info("[HotReloader] 检测到文件变化: %s", names)
        self._pending_reload = False
        self._consume_detected()

        try:
            # 保存旧配置用于回退
            old_config = dict(self.config._raw_config) if hasattr(self.config, '_raw_config') else {}

            # 重新加载
            self.config._load()

            # 更新 mtime (成功才推进冷却基准)
            for f in self.watch_files:
                try:
                    self._last_mtime[f] = os.path.getmtime(f)
                except OSError:
                    pass

            self._last_reload_time = now
            self._reload_count += 1
            logger.info("[HotReloader] ✅ 配置已热重载 (第 %d 次)", self._reload_count)

            # 通知下游消费者 (ConfigContainer 等)
            if self._post_reload_callback:
                try:
                    self._post_reload_callback()
                except Exception as cb_err:
                    logger.warning("[HotReloader] 回调失败 (不影响主配置): %s", cb_err)

        except Exception as e:
            self._error_count += 1
            logger.error("[HotReloader] ❌ 热重载失败: %s", e)
            # 回退到旧配置
            if hasattr(self.config, '_raw_config') and old_config:
                self.config._raw_config = old_config
                logger.warning("[HotReloader] 已回退到旧配置")
            # 失败不丢变更: 挂起等下一冷却窗重试; 同时推进冷却基准防每秒 ERROR 刷屏
            self._pending_reload = True
            self._last_reload_time = now

    def _consume_detected(self):
        """把本轮已检测的变化记账: 刷新 mtime 基准 + 清空暂存名 (消费检测防重报)."""
        for f in self.watch_files:
            try:
                self._last_mtime[f] = os.path.getmtime(f)
            except OSError:
                pass
        self._changed_files.clear()

    def _loop(self):
        """监控循环"""
        while self._running:
            try:
                # 检测 OR 挂起 (冷却期变更 / 失败重试) — 冷却判断在 _reload 内部,
                # 挂起期间每秒进来会被冷却分支挡回 (DEBUG 级, 不刷屏)
                if self._check_files() or self._pending_reload:
                    self._reload()
            except Exception as e:
                logger.error("[HotReloader] 监控异常: %s", e)

            # 每秒检查一次
            for _ in range(1):
                if not self._running:
                    return
                time.sleep(1)

    @property
    def stats(self) -> dict:
        return {
            "reload_count": self._reload_count,
            "error_count": self._error_count,
            "cooldown_seconds": self.cooldown_seconds,
            "watch_files": [os.path.basename(f) for f in self.watch_files],
            "running": self._running,
        }
