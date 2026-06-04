"""
全局交易模式管理器（单一真相源）

统一封装 TRADE_ENV / LIVE_CONSERVATIVE 等环境变量判定逻辑，
避免模式判定散落在 ConfigContainer / futu_trade_env 等多处。

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
from typing import Optional

logger = logging.getLogger(__name__)


class TradingModeEnum(Enum):
    SIMULATE = "simulate"
    LIVE_CONSERVATIVE = "live_conservative"  # 实盘保守模式（缩小仓位、收紧风控）
    LIVE_NORMAL = "live_normal"              # 实盘正常模式


class TradingMode:
    """全局交易模式管理器 - 单一真相源（线程安全的进程级单例）"""

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
        self._mode = self._detect_mode()
        logger.info("TradingMode initialized: %s", self._mode.value)

    # ------------------------------------------------------------------ #
    # 模式检测
    # ------------------------------------------------------------------ #
    def _detect_mode(self) -> TradingModeEnum:
        """
        从环境变量检测当前模式
        - TRADE_ENV=1  -> SIMULATE
        - TRADE_ENV=0 + LIVE_CONSERVATIVE=1 -> LIVE_CONSERVATIVE
        - TRADE_ENV=0 + LIVE_CONSERVATIVE=0 -> LIVE_NORMAL
        """
        trade_env = os.getenv("TRADE_ENV", "1").strip()
        if trade_env == "0":
            conservative = os.getenv("LIVE_CONSERVATIVE", "1").strip()
            if conservative == "1":
                return TradingModeEnum.LIVE_CONSERVATIVE
            return TradingModeEnum.LIVE_NORMAL
        return TradingModeEnum.SIMULATE

    def reload(self) -> TradingModeEnum:
        """重新读取环境变量（仅在切换脚本/测试中使用，运行时不应频繁调用）"""
        old = self._mode
        self._mode = self._detect_mode()
        if old != self._mode:
            logger.warning("TradingMode reload: %s -> %s", old.value, self._mode.value)
        return self._mode

    # ------------------------------------------------------------------ #
    # 状态访问
    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> TradingModeEnum:
        return self._mode

    @property
    def is_simulate(self) -> bool:
        return self._mode == TradingModeEnum.SIMULATE

    @property
    def is_live(self) -> bool:
        return self._mode in (
            TradingModeEnum.LIVE_CONSERVATIVE,
            TradingModeEnum.LIVE_NORMAL,
        )

    @property
    def is_conservative(self) -> bool:
        return self._mode == TradingModeEnum.LIVE_CONSERVATIVE

    @property
    def mode_label(self) -> str:
        """用于日志和报告的中文标签"""
        labels = {
            TradingModeEnum.SIMULATE: "模拟盘",
            TradingModeEnum.LIVE_CONSERVATIVE: "实盘(保守)",
            TradingModeEnum.LIVE_NORMAL: "实盘(正常)",
        }
        return labels[self._mode]

    @property
    def db_mode_value(self) -> str:
        """与 trade_records.mode 列对齐的取值（兼容历史 sandbox 命名）"""
        if self._mode == TradingModeEnum.SIMULATE:
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
        return multipliers[self._mode]

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
    # 调试
    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:
        return f"<TradingMode mode={self._mode.value} label={self.mode_label}>"


__all__ = ["TradingMode", "TradingModeEnum"]
