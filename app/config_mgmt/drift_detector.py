"""
FTSeek-US 配置漂移检测器 — Layer 2: 定期巡检

功能:
1. 加载时创建配置快照 (hash + key set)
2. 定期检查当前配置是否与快照一致
3. 漂移时发送告警通知
4. 自动重新快照 (避免重复告警)
"""

import os
import time
import json
import hashlib
import logging
import threading
from typing import Optional, Callable
from pathlib import Path

logger = logging.getLogger("ftseek_us.config_mgmt.drift_detector")

DEFAULT_DRIFT_LOG_PATH = os.path.expanduser("~/.hermes/ftseek/data/us_config_drift_log.json")


class ConfigSnapshot:
    """配置快照"""

    def __init__(self, config_dict: dict):
        self.timestamp = time.time()
        self.key_count = self._count_keys(config_dict)
        self.content_hash = self._compute_hash(config_dict)
        self.top_level_keys = sorted(config_dict.keys()) if isinstance(config_dict, dict) else []

    @staticmethod
    def _compute_hash(d: dict) -> str:
        content = json.dumps(d, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @staticmethod
    def _count_keys(d: dict, prefix: str = "") -> int:
        count = 0
        for k, v in d.items():
            full_key = f"{prefix}.{k}" if prefix else k
            count += 1
            if isinstance(v, dict):
                count += ConfigSnapshot._count_keys(v, full_key)
        return count

    def diff(self, other: "ConfigSnapshot") -> dict:
        """计算两个快照的差异"""
        return {
            "timestamp_diff": other.timestamp - self.timestamp,
            "hash_changed": self.content_hash != other.content_hash,
            "key_count_diff": other.key_count - self.key_count,
            "old_hash": self.content_hash,
            "new_hash": other.content_hash,
            "old_keys": self.key_count,
            "new_keys": other.key_count,
        }


class ConfigDriftDetector:
    """
    配置漂移检测器

    定期检查配置文件是否被外部修改 (非热重载修改)。
    """

    def __init__(
        self,
        config_loader,
        interval_hours: float = 4.0,
        log_path: str = None,
        on_drift_detected: Optional[Callable] = None,
    ):
        """
        Args:
            config_loader: ConfigLoader 实例
            interval_hours: 巡检间隔 (小时)
            log_path: 漂移日志路径
            on_drift_detected: 漂移回调 (如钉钉通知)
        """
        self.config = config_loader
        self.interval_seconds = int(interval_hours * 3600)
        self.log_path = log_path or DEFAULT_DRIFT_LOG_PATH
        self.on_drift_detected = on_drift_detected

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_snapshot: Optional[ConfigSnapshot] = None
        self._drift_count = 0

        # 启动时创建初始快照
        self._take_snapshot()
        logger.info(
            "[DriftDetector] 初始快照: hash=%s keys=%d",
            self._last_snapshot.content_hash,
            self._last_snapshot.key_count,
        )

    def _take_snapshot(self):
        """创建配置快照"""
        raw = self.config._raw_config if hasattr(self.config, '_raw_config') else {}
        self._last_snapshot = ConfigSnapshot(raw)

    def _check_drift(self) -> bool:
        """检查是否有漂移"""
        current = ConfigSnapshot(self.config._raw_config if hasattr(self.config, '_raw_config') else {})
        if self._last_snapshot is None:
            return False

        diff = self._last_snapshot.diff(current)
        if diff["hash_changed"]:
            self._drift_count += 1
            logger.warning(
                "[DriftDetector] ⚠️ 检测到配置漂移! hash: %s → %s | keys: %d → %d",
                diff["old_hash"], diff["new_hash"], diff["old_keys"], diff["new_keys"]
            )
            # 记录漂移
            self._log_drift(diff)
            # 回调通知
            if self.on_drift_detected:
                try:
                    self.on_drift_detected(diff)
                except Exception as e:
                    logger.error("[DriftDetector] 漂移回调异常: %s", e)
            # 更新快照
            self._take_snapshot()
            return True
        return False

    def _log_drift(self, diff: dict):
        """记录漂移事件"""
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        record = {
            "timestamp": time.time(),
            "datetime": __import__('datetime').datetime.now().isoformat(),
            **diff,
        }
        try:
            if os.path.exists(self.log_path):
                with open(self.log_path, "r") as f:
                    history = json.load(f)
            else:
                history = []
            history.append(record)
            # 只保留最近 50 条
            history = history[-50:]
            with open(self.log_path, "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.error("[DriftDetector] 记录漂移日志失败: %s", e)

    def start(self):
        """启动后台检测线程"""
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="ConfigDriftDetector", daemon=True
        )
        self._thread.start()
        logger.info("[DriftDetector] 启动 (interval=%dh)", self.interval_seconds // 3600)

    def stop(self):
        """停止检测"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[DriftDetector] 已停止 | 累计漂移=%d", self._drift_count)

    def _loop(self):
        """检测循环"""
        while self._running:
            try:
                self._check_drift()
            except Exception as e:
                logger.error("[DriftDetector] 检测异常: %s", e)

            # 可中断休眠
            for _ in range(self.interval_seconds):
                if not self._running:
                    return
                time.sleep(1)

    @property
    def stats(self) -> dict:
        return {
            "drift_count": self._drift_count,
            "last_hash": self._last_snapshot.content_hash if self._last_snapshot else None,
            "interval_hours": self.interval_seconds // 3600,
            "running": self._running,
        }
