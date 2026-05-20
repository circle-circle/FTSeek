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
    ):
        """
        Args:
            config_loader: ConfigLoader 实例
            cooldown_seconds: 重载冷却时间 (秒)
            watch_files: 额外监控的文件列表
        """
        self.config = config_loader
        self.cooldown_seconds = cooldown_seconds

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
        """检查是否有文件变化"""
        for f in self.watch_files:
            try:
                mtime = os.path.getmtime(f)
                if mtime != self._last_mtime.get(f, 0):
                    logger.info(
                        "[HotReloader] 检测到文件变化: %s",
                        os.path.basename(f)
                    )
                    return True
            except OSError:
                continue
        return False

    def _reload(self):
        """重新加载配置"""
        now = time.time()
        # 冷却检查
        if now - self._last_reload_time < self.cooldown_seconds:
            return

        try:
            # 保存旧配置用于回退
            old_config = dict(self.config._raw_config) if hasattr(self.config, '_raw_config') else {}

            # 重新加载
            self.config._load()

            # 更新 mtime
            for f in self.watch_files:
                try:
                    self._last_mtime[f] = os.path.getmtime(f)
                except OSError:
                    pass

            self._last_reload_time = now
            self._reload_count += 1
            logger.info("[HotReloader] ✅ 配置已热重载 (第 %d 次)", self._reload_count)

        except Exception as e:
            self._error_count += 1
            logger.error("[HotReloader] ❌ 热重载失败: %s", e)
            # 回退到旧配置
            if hasattr(self.config, '_raw_config') and old_config:
                self.config._raw_config = old_config
                logger.warning("[HotReloader] 已回退到旧配置")

    def _loop(self):
        """监控循环"""
        while self._running:
            try:
                if self._check_files():
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
