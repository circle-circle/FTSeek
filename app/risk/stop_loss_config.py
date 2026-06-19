"""
止损配置 Single Source of Truth (SoT) — P1 2026-05-20

设计原则:
  1. 所有止损参数从 unified_config.yaml 读取，不硬编码
  2. 提供统一的 get_stop_loss_pct() 接口，按策略/市况返回
  3. 保持向后兼容：旧代码可继续用 config['stop_loss'] 等字段
  4. 启动时验证：所有策略的止损参数必须存在且合理

止损参数层级 (优先级从高到低):
  1. LLM 策略自定义 (signal.metadata['custom_stop_loss_pct'])
  2. 市况匹配 (dynamic_exit.strong_trend_up/range_bound/...)
  3. 策略默认 (strategies.defaults.stop_loss_pct)
  4. 系统兜底 (dynamic_exit.default.stop_loss)
"""

from __future__ import annotations

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  默认值 (YAML 缺失时的兜底)
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_STOP_LOSS = {
    "default": -0.03,        # 3% 止损
    "strong_trend_up": -0.08,
    "strong_trend_down": -0.03,
    "range_bound": -0.035,
    "high_volatility": -0.07,
    "intraday": -0.03,
}

_DEFAULT_TAKE_PROFIT = {
    "default": 0.10,
    "strong_trend_up": 0.25,
    "strong_trend_down": 0.045,
    "range_bound": 0.05,
    "high_volatility": 0.09,
    "intraday": 0.05,
}


class StopLossConfig:
    """止损配置管理器 — 全局唯一 SoT"""

    _instance: Optional[StopLossConfig] = None
    _config: Dict[str, Any] = {}

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._validate()

    @classmethod
    def initialize(cls, config: Dict[str, Any]) -> "StopLossConfig":
        """初始化 (服务启动时调用一次)"""
        cls._instance = cls(config)
        logger.info("[StopLossSoT] ✅ 初始化完成")
        return cls._instance

    @classmethod
    def get_instance(cls) -> "StopLossConfig":
        """获取单例 (未初始化时用默认值)"""
        if cls._instance is None:
            logger.warning("[StopLossSoT] ⚠️ 未初始化，使用默认值")
            cls._instance = cls({})
        return cls._instance

    def _validate(self):
        """启动时验证止损参数"""
        dynamic_exit = self._config.get("dynamic_exit", {})
        if not dynamic_exit:
            logger.warning("[StopLossSoT] dynamic_exit 配置缺失，使用默认值")
            return

        default_sl = dynamic_exit.get("default", {}).get("stop_loss")
        if default_sl is None:
            logger.error("[StopLossSoT] ❌ dynamic_exit.default.stop_loss 缺失!")
        elif default_sl > 0:
            logger.error(f"[StopLossSoT] ❌ stop_loss 应为负数: {default_sl}")

        # 验证所有 regime 配置
        for regime in ["strong_trend_up", "strong_trend_down", "range_bound", "high_volatility"]:
            regime_config = dynamic_exit.get(regime, {})
            sl = regime_config.get("stop_loss")
            if sl is None:
                logger.warning(f"[StopLossSoT] {regime}.stop_loss 缺失，使用默认")
            elif sl > 0:
                logger.warning(f"[StopLossSoT] {regime}.stop_loss 应为负数: {sl}")

    def get_stop_loss_pct(
        self,
        strategy_id: str = "voting_llm",
        market_regime: str = "default",
        custom_sl: Optional[float] = None,
    ) -> float:
        """
        获取止损百分比 (负数表示亏损)

        优先级: custom_sl > regime > strategy_default > system_default

        Args:
            strategy_id: 策略 ID (如 "voting_llm")
            market_regime: 市场状态 (如 "strong_trend_up", "range_bound")
            custom_sl: LLM 策略自定义止损 (最高优先级)

        Returns:
            止损百分比 (负数, 如 -0.05 表示 5% 止损)
        """
        # 1. LLM 自定义 (最高优先级)
        if custom_sl is not None:
            return -abs(custom_sl)  # 确保为负数

        # 2. 市况匹配
        dynamic_exit = self._config.get("dynamic_exit", {})
        regime_config = dynamic_exit.get(market_regime, {})
        if regime_config and "stop_loss" in regime_config:
            sl = regime_config["stop_loss"]
            if isinstance(sl, (int, float)) and sl < 0:
                return sl
            elif isinstance(sl, (int, float)) and sl > 0:
                return -sl  # 兼容正数写法

        # 3. 策略默认
        strategies = self._config.get("strategies", {})
        strategy_config = strategies.get(strategy_id, {})
        if strategy_config and "stop_loss_pct" in strategy_config:
            sl = strategy_config["stop_loss_pct"]
            if isinstance(sl, (int, float)):
                return sl if sl < 0 else -sl

        # 4. 系统兜底
        default_sl = dynamic_exit.get("default", {}).get("stop_loss", _DEFAULT_STOP_LOSS["default"])
        if isinstance(default_sl, (int, float)):
            return default_sl if default_sl < 0 else -default_sl

        return _DEFAULT_STOP_LOSS["default"]

    def get_take_profit_pct(
        self,
        strategy_id: str = "voting_llm",
        market_regime: str = "default",
        custom_tp: Optional[float] = None,
    ) -> float:
        """获取止盈百分比 (正数)"""
        if custom_tp is not None:
            return abs(custom_tp)

        dynamic_exit = self._config.get("dynamic_exit", {})
        regime_config = dynamic_exit.get(market_regime, {})
        if regime_config and "take_profit" in regime_config:
            tp = regime_config["take_profit"]
            if isinstance(tp, (int, float)):
                return abs(tp)

        strategies = self._config.get("strategies", {})
        strategy_config = strategies.get(strategy_id, {})
        if strategy_config and "take_profit_pct" in strategy_config:
            tp = strategy_config["take_profit_pct"]
            if isinstance(tp, (int, float)):
                return abs(tp)

        return _DEFAULT_TAKE_PROFIT.get(market_regime, _DEFAULT_TAKE_PROFIT["default"])

    def get_all_regimes(self) -> Dict[str, Dict[str, float]]:
        """获取所有市况的止损止盈配置 (用于启动验证)"""
        result = {}
        dynamic_exit = self._config.get("dynamic_exit", {})
        for regime in ["default", "strong_trend_up", "strong_trend_down", "range_bound", "high_volatility"]:
            regime_config = dynamic_exit.get(regime, {})
            sl = regime_config.get("stop_loss", _DEFAULT_STOP_LOSS.get(regime, _DEFAULT_STOP_LOSS["default"]))
            tp = regime_config.get("take_profit", _DEFAULT_TAKE_PROFIT.get(regime, _DEFAULT_TAKE_PROFIT["default"]))
            result[regime] = {
                "stop_loss": sl if isinstance(sl, (int, float)) else _DEFAULT_STOP_LOSS["default"],
                "take_profit": tp if isinstance(tp, (int, float)) else _DEFAULT_TAKE_PROFIT["default"],
            }
        return result


# ═══════════════════════════════════════════════════════════════════
#  便捷函数 (全局访问)
# ═══════════════════════════════════════════════════════════════════

def get_stop_loss_pct(strategy_id: str = "voting_llm", market_regime: str = "default", custom_sl: Optional[float] = None) -> float:
    """获取止损百分比 (全局便捷函数)"""
    return StopLossConfig.get_instance().get_stop_loss_pct(strategy_id, market_regime, custom_sl)


def get_take_profit_pct(strategy_id: str = "voting_llm", market_regime: str = "default", custom_tp: Optional[float] = None) -> float:
    """获取止盈百分比 (全局便捷函数)"""
    return StopLossConfig.get_instance().get_take_profit_pct(strategy_id, market_regime, custom_tp)
