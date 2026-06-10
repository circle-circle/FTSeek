"""
全局交易模式管理器（单一真相源）

统一封装 TRADE_ENV / LIVE_CONSERVATIVE 等环境变量判定逻辑，
避免模式判定散落在 ConfigContainer / futu_trade_env 等多处。

热加载: 每次访问 mode 属性时从 .env.us 文件实时读取，无需重启进程。

用法:
    from app.core.trading_mode import TradingMode, TradingModeEnum

    tm = TradingMode()
    if tm.is_live:
        ...
    pos = tm.get_max_position_size(base_size=10000)
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# .env.us 文件路径
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env.us"
_YAML_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"


class TradingModeEnum(Enum):
    SIMULATE = "simulate"
    LIVE_CONSERVATIVE = "live_conservative"  # 实盘保守模式（缩小仓位、收紧风控）
    LIVE_NORMAL = "live_normal"              # 实盘正常模式


def _read_env_file(key: str, default: str = "") -> str:
    """从 .env.us 实时读取 (每次调用都读文件，保证热加载)"""
    try:
        if _ENV_FILE.exists():
            with open(_ENV_FILE) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
                        return stripped.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.getenv(key, default)


class TradingMode:
    """全局交易模式管理器 - 热加载单例 (每次访问从 .env.us 实时读取)"""

    _instance: Optional["TradingMode"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        logger.info("TradingMode initialized (hot-reload from .env.us)")

    # ------------------------------------------------------------------ #
    # 模式检测 (每次从文件读取，实现热加载)
    # ------------------------------------------------------------------ #
    def _detect_mode(self) -> TradingModeEnum:
        """
        从 .env.us 实时检测当前模式 (热加载，无需重启)
        - TRADE_ENV=1  -> SIMULATE
        - TRADE_ENV=0 + LIVE_CONSERVATIVE=1 -> LIVE_CONSERVATIVE
        - TRADE_ENV=0 + LIVE_CONSERVATIVE=0 -> LIVE_NORMAL
        """
        trade_env = _read_env_file("TRADE_ENV", "1")
        if trade_env == "0":
            conservative = _read_env_file("LIVE_CONSERVATIVE", "1")
            if conservative == "1":
                return TradingModeEnum.LIVE_CONSERVATIVE
            return TradingModeEnum.LIVE_NORMAL
        return TradingModeEnum.SIMULATE

    # ------------------------------------------------------------------ #
    # 状态访问 (每次调用 _detect_mode，实时反映 .env.us 变更)
    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> TradingModeEnum:
        return self._detect_mode()

    @property
    def is_simulate(self) -> bool:
        return self.mode == TradingModeEnum.SIMULATE

    @property
    def is_live(self) -> bool:
        return self.mode in (
            TradingModeEnum.LIVE_CONSERVATIVE,
            TradingModeEnum.LIVE_NORMAL,
        )

    @property
    def is_conservative(self) -> bool:
        return self.mode == TradingModeEnum.LIVE_CONSERVATIVE

    @property
    def mode_label(self) -> str:
        """用于日志和报告的中文标签"""
        labels = {
            TradingModeEnum.SIMULATE: "模拟盘",
            TradingModeEnum.LIVE_CONSERVATIVE: "实盘(保守)",
            TradingModeEnum.LIVE_NORMAL: "实盘(正常)",
        }
        return labels[self.mode]

    @property
    def db_mode_value(self) -> str:
        """与 trade_records.mode 列对齐的取值（兼容历史 sandbox 命名）"""
        if self.mode == TradingModeEnum.SIMULATE:
            return "sandbox"
        return "normal"

    # ------------------------------------------------------------------ #
    # 风控适配
    # ------------------------------------------------------------------ #
    def get_risk_multiplier(self) -> float:
        """根据模式返回风控倍数，实盘更严"""
        multipliers = {
            TradingModeEnum.SIMULATE: 1.0,
            TradingModeEnum.LIVE_CONSERVATIVE: 0.5,  # 仓位减半
            TradingModeEnum.LIVE_NORMAL: 0.8,        # 仓位 80%
        }
        return multipliers[self.mode]

    def get_max_position_size(self, base_size: float) -> float:
        """根据模式调整最大仓位"""
        return base_size * self.get_risk_multiplier()

    def get_max_daily_loss(self, base_loss: float) -> float:
        """
        根据模式调整最大日亏损容忍度
        实盘的日亏损容忍度更低，保守模式仅放行 30%
        """
        if self.is_conservative:
            return base_loss * 0.3
        if self.is_live:
            return base_loss * 0.6
        return base_loss

    # ------------------------------------------------------------------ #
    # YAML mode_adaptation 配置读取 (热加载，每次从文件读取)
    # ------------------------------------------------------------------ #
    _MODE_YAML_KEY = {
        TradingModeEnum.SIMULATE: "simulate",
        TradingModeEnum.LIVE_CONSERVATIVE: "live_conservative",
        TradingModeEnum.LIVE_NORMAL: "live_normal",
    }

    def _read_mode_adaptation(self) -> dict:
        """从 unified_config.yaml 读取当前模式的 mode_adaptation 参数"""
        try:
            if _YAML_CONFIG.exists():
                import yaml
                with open(_YAML_CONFIG) as f:
                    cfg = yaml.safe_load(f)
                mode_key = self._MODE_YAML_KEY.get(self.mode, "simulate")
                return (cfg.get("risk", {}).get("mode_adaptation", {}).get(mode_key, {})) or {}
        except Exception:
            pass
        return {}

    def get_max_daily_loss_pct(self) -> float:
        """当前模式的最大日亏损百分比 (来自 YAML mode_adaptation)"""
        val = self._read_mode_adaptation().get("max_daily_loss_pct")
        if val is not None:
            return float(val) / 100.0
        # 硬编码 fallback (与 YAML 默认值对齐)
        defaults = {TradingModeEnum.SIMULATE: 0.05, TradingModeEnum.LIVE_CONSERVATIVE: 0.02, TradingModeEnum.LIVE_NORMAL: 0.03}
        return defaults.get(self.mode, 0.03)

    def get_max_position_pct(self) -> float:
        """当前模式的单标的最大仓位占总资产百分比 (来自 YAML mode_adaptation)"""
        val = self._read_mode_adaptation().get("max_position_pct")
        if val is not None:
            return float(val) / 100.0
        defaults = {TradingModeEnum.SIMULATE: 0.15, TradingModeEnum.LIVE_CONSERVATIVE: 0.08, TradingModeEnum.LIVE_NORMAL: 0.12}
        return defaults.get(self.mode, 0.15)

    def get_max_total_exposure_pct(self) -> float:
        """当前模式的最大总敞口占总资产百分比 (来自 YAML mode_adaptation)"""
        val = self._read_mode_adaptation().get("max_total_exposure_pct")
        if val is not None:
            return float(val) / 100.0
        defaults = {TradingModeEnum.SIMULATE: 0.80, TradingModeEnum.LIVE_CONSERVATIVE: 0.40, TradingModeEnum.LIVE_NORMAL: 0.60}
        return defaults.get(self.mode, 0.80)

    def get_circuit_breaker_loss_pct(self) -> float:
        """当前模式的熔断触发百分比 (来自 YAML mode_adaptation)"""
        val = self._read_mode_adaptation().get("circuit_breaker_loss_pct")
        if val is not None:
            return float(val) / 100.0
        defaults = {TradingModeEnum.SIMULATE: 0.08, TradingModeEnum.LIVE_CONSERVATIVE: 0.03, TradingModeEnum.LIVE_NORMAL: 0.05}
        return defaults.get(self.mode, 0.08)

    # ------------------------------------------------------------------ #
    # 调试
    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:
        return f"<TradingMode mode={self.mode.value} label={self.mode_label}>"


__all__ = ["TradingMode", "TradingModeEnum"]
