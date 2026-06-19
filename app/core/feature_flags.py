"""
Feature Flags 框架 — 渐进部署控制

Task #1 (2026-06-17): 安全基础设施
- 从 unified_config.yaml 的 deployment.feature_flags 读取
- 支持 shadow_mode 判断
- 支持 reload (配置热重载)
"""
import logging
import threading
import yaml
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 配置文件路径
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"


class FeatureFlags:
    """
    Feature Flags 单例

    从 unified_config.yaml 的 deployment 段读取:
    - feature_flags: 各功能开关
    - shadow_mode: 影子模式配置
    """

    _instance: Optional["FeatureFlags"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._state_lock = threading.Lock()
        self._flags: dict = {}
        self._shadow_mode: dict = {}
        self._load()
        logger.info(
            f"[FeatureFlags] 初始化完成 | flags={self._flags} | shadow={self._shadow_mode}"
        )

    def _load(self) -> None:
        """从 unified_config.yaml 加载 deployment 配置"""
        try:
            if not _CONFIG_PATH.exists():
                logger.warning(f"[FeatureFlags] 配置文件不存在: {_CONFIG_PATH}")
                return

            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            deployment = config.get("deployment", {})
            self._flags = deployment.get("feature_flags", {})
            self._shadow_mode = deployment.get("shadow_mode", {})

        except Exception as e:
            logger.error(f"[FeatureFlags] 加载配置失败: {e}")
            # fail-safe: 所有 flag 默认 False (不启用新功能)
            self._flags = {}
            self._shadow_mode = {}

    def is_enabled(self, flag_name: str) -> bool:
        """
        检查某个 feature flag 是否启用

        Args:
            flag_name: flag 名称 (如 "event_driven_scan")

        Returns:
            True = 已启用, False = 未启用或不存在
        """
        with self._state_lock:
            return bool(self._flags.get(flag_name, False))

    def is_shadow_mode(self) -> bool:
        """
        是否处于 shadow mode (记录但不执行)

        Returns:
            True = shadow mode 开启
        """
        with self._state_lock:
            return bool(self._shadow_mode.get("enabled", False))

    def should_compare_legacy(self) -> bool:
        """是否需要新旧对比"""
        with self._state_lock:
            return bool(self._shadow_mode.get("compare_with_legacy", False))

    def reload(self) -> None:
        """热重载配置"""
        with self._state_lock:
            self._load()
            logger.info(
                f"[FeatureFlags] 配置已重载 | flags={self._flags} | shadow={self._shadow_mode}"
            )

    def get_all_flags(self) -> dict:
        """获取所有 flag 状态 (只读副本)"""
        with self._state_lock:
            return dict(self._flags)


# ── 全局单例访问 ──
def get_feature_flags() -> FeatureFlags:
    """获取 FeatureFlags 全局单例"""
    return FeatureFlags()
