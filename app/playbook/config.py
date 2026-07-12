"""Playbook 配置读取入口 — 从 unified_config.yaml 的 playbook 节读取参数。

设计目标:
- 消灭 instruction_generator.py / pipeline.py 中的硬编码魔法数字
- 提供统一的默认值与配置访问接口
- 不引入循环依赖 (本模块只读 ConfigContainer, 不 import Playbook 业务模块)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class PlaybookConfig:
    """Playbook 配置包装器, 提供点号访问和默认值."""

    # 默认配置 (与 2026-06-26 修复对齐)
    DEFAULTS: Dict[str, Any] = {
        # risk_parameters
        "risk_base_capital": 82000.0,
        "risk_pct": 1.0,
        "rr_ratio": 1.0,
        "fixed_stop_loss_pct": 2.0,
        "max_position_pct": 0.08,
        "atr_multiplier": 2.0,
        # execution_parameters
        "intraday_breakout_pct": 0.5,
        "intraday_volume_ratio": 1.5,
        "min_confidence": 65,
        # 2026-06-28: 方向差异化 confidence 门槛 (BUY 要求更高, SHORT 维持默认)
        "buy_min_confidence": 80,
        "sell_short_min_confidence": 65,
        "buying_power_fallback_alert": True,
        # 2026-06-28: 单日 guardrail (仅 auto_execute 阶段生效)
        # 2026-07-06: 模拟交易期提升交易量 20%→40%, 5→10 (收集更多样本数据)
        # 2026-07-09: 40%→60% — bridge候选直写DB后, deep_analysis信号质量提升,
        #   更多资金给bridge候选执行, 积累更多交易数据验证方向+退出策略
        "daily_notional_cap_pct": 0.60,
        "max_daily_positions": 10,
        # horizon defaults
        "short_rr_ratio": 1.0,
        "long_rr_ratio": 2.5,
        "short_fixed_stop_loss_pct": 2.0,
        "long_fixed_stop_loss_pct": 2.0,
    }

    def __init__(self):
        self._config = self._load()
        self._horizon_config = self._load_horizon()

    @staticmethod
    def _load() -> Dict[str, Any]:
        """尝试从 unified_config.yaml 读取 playbook 配置, 失败则返回默认."""
        try:
            from app.core.config_container import ConfigContainer
            container = ConfigContainer.get_instance()
            cfg = container.get_dotted("playbook", {}) or {}
            # 合并 risk_parameters + execution_parameters 到扁平字典
            merged = {}
            for section in ("risk_parameters", "execution_parameters"):
                section_cfg = cfg.get(section) or {}
                if isinstance(section_cfg, dict):
                    merged.update(section_cfg)
            return merged
        except Exception as e:
            logger.warning(f"[PlaybookConfig] 读取配置失败, 使用默认: {e}")
            return {}

    def _load_horizon(self) -> Dict[str, Any]:
        """从 playbook_horizon.yaml 读取 horizon 配置, 失败则返回默认."""
        try:
            from pathlib import Path
            path = Path(__file__).resolve().parent.parent.parent / "config" / "playbook_horizon.yaml"
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"[PlaybookConfig] 读取 horizon 配置失败, 使用默认: {e}")
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置, 优先使用 PlaybookConfig.DEFAULTS 作为兜底."""
        if default is None and key in self.DEFAULTS:
            default = self.DEFAULTS[key]
        return self._config.get(key, default)

    def get_horizon(self, key: str, default: Any = None) -> Any:
        """读取 horizon 配置"""
        return self._horizon_config.get("horizon_selector", {}).get(key, default)

    def get_horizon_params(self, horizon: str) -> Dict[str, Any]:
        """读取指定 horizon 的参数"""
        return self._horizon_config.get(f"{horizon}_horizon", {})

    def get_horizon_top(self, key: str, default: Any = None) -> Any:
        """读取 playbook_horizon.yaml 顶层键 (如 factor_exit_rules, market_state_overlay)."""
        return self._horizon_config.get(key, default)

    def __getattr__(self, key: str) -> Any:
        return self.get(key)


# 全局懒加载实例
_playbook_config: PlaybookConfig | None = None


def get_playbook_config() -> PlaybookConfig:
    """获取 PlaybookConfig 单例."""
    global _playbook_config
    if _playbook_config is None:
        _playbook_config = PlaybookConfig()
    return _playbook_config
