"""
策略注册中心 — 统一管理所有策略的注册、发现与实例化

功能:
- 从 app/decision/strategies/ 自动发现并注册已有策略
- 支持运行时注册新策略
- 按策略族 (trend/reversion/sentiment) 分组查询
- 策略版本管理
"""
from __future__ import annotations

import logging
from typing import Dict, Any, Optional, List, Type

from .base import BaseStrategy

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """
    策略注册中心

    单例模式，全局唯一入口。
    """

    _instance: Optional["StrategyRegistry"] = None

    def __new__(cls) -> "StrategyRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._strategies: Dict[str, Type[BaseStrategy]] = {}
        self._instances: Dict[str, BaseStrategy] = {}
        self._group_index: Dict[str, List[str]] = {}
        self._initialized = True
        self._load_builtin()

    # ----- 注册 -----
    def register(self, strategy_cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
        """注册策略类"""
        if not issubclass(strategy_cls, BaseStrategy):
            raise TypeError(f"{strategy_cls.__name__} must extend BaseStrategy")

        # 获取 strategy_id
        instance = strategy_cls()
        sid = instance.strategy_id
        if not sid:
            raise ValueError(f"{strategy_cls.__name__}.strategy_id is empty")

        self._strategies[sid] = strategy_cls

        # 分组索引
        group = instance.strategy_group
        if group not in self._group_index:
            self._group_index[group] = []
        if sid not in self._group_index[group]:
            self._group_index[group].append(sid)

        logger.info("[StrategyRegistry] 已注册: %s (group=%s, v%s)",
                    sid, group, instance.version)
        return strategy_cls

    def unregister(self, strategy_id: str) -> None:
        """注销策略"""
        cls = self._strategies.pop(strategy_id, None)
        if cls:
            instance = cls()
            group = instance.strategy_group
            if group in self._group_index:
                self._group_index[group] = [
                    s for s in self._group_index[group] if s != strategy_id
                ]
        self._instances.pop(strategy_id, None)
        logger.info("[StrategyRegistry] 已注销: %s", strategy_id)

    # ----- 发现 -----
    def _load_builtin(self) -> None:
        """从 app/decision/strategies/ 加载内置策略"""
        try:
            from app.decision.strategies import (
                PriceBreakStrategy, MACrossStrategy, RSIReboundStrategy
            )
            from app.decision.strategies.base import _strategy_registry as old_registry

            # 从旧注册表加载
            for name, cls in list(old_registry.items()):
                if hasattr(cls, 'strategy_id') and hasattr(cls, 'strategy_group'):
                    # 新式策略（已有 strategy_group）
                    self.register(cls)
                    continue

                # 旧式策略：用适配器包装
                adapted = self._adapt_old_strategy(name, cls)
                if adapted:
                    self.register(adapted)
        except ImportError as e:
            logger.warning("[StrategyRegistry] 内置策略加载失败: %s", e)

    def _adapt_old_strategy(self, name: str, old_cls: Type) -> Optional[Type[BaseStrategy]]:
        """将旧式策略适配为新接口"""
        try:
            from app.decision.strategies.base import BaseStrategy as OldBase

            # 创建实例获取属性
            instance = old_cls()

            # 推断策略分组
            group = "other"
            if name in ("price_break", "ma_cross", "macd", "supertrend"):
                group = "trend"
            elif name in ("rsi_rebound", "rsi", "bollinger"):
                group = "reversion"
            elif name in ("news", "llm", "sentiment"):
                group = "sentiment"

            class AdaptedStrategy(BaseStrategy):
                strategy_id = name
                strategy_group = group
                version = getattr(instance, 'version', '1.0.0')
                description = getattr(instance, 'description', f"Adapted {name}")

                def __init__(self, params=None):
                    super().__init__(params)
                    self._old_instance = old_cls(params=params)

                def generate_signal(self, symbol, context):
                    from app.core.models import SignalScore as _SS, Direction as _D
                    # 转回 dict 调用旧接口
                    data = context.to_dict()
                    result = self._old_instance.execute(symbol, data)
                    if result is None:
                        return None
                    return _SS(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        direction=_D(result.get("direction", "LONG")),
                        strength=result.get("strength", 50),
                        confidence=result.get("confidence", 0.5),
                        expected_horizon=result.get("expected_horizon", 60),
                        volatility_fit=result.get("volatility_fit", True),
                        market_regime=result.get("market_regime", "RANGE"),
                        risk_reward_estimate=result.get("risk_reward", 2.0),
                        score_breakdown=result.get("score_breakdown", {}),
                    )

                def get_parameters(self):
                    return self._old_instance.params

                def update_parameters(self, params):
                    self._old_instance.params.update(params)
                    self._params.update(params)

            return AdaptedStrategy
        except Exception as e:
            logger.warning("[StrategyRegistry] 适配 %s 失败: %s", name, e)
            return None

    # ----- 查询 -----
    def get(self, strategy_id: str, params: Optional[Dict[str, Any]] = None) -> Optional[BaseStrategy]:
        """获取策略实例（带缓存）"""
        if params is None:
            # 无参数：返回缓存的默认实例
            if strategy_id in self._instances:
                return self._instances[strategy_id]

        cls = self._strategies.get(strategy_id)
        if cls is None:
            return None

        instance = cls(params=params)
        if params is None:
            self._instances[strategy_id] = instance
        return instance

    def get_all(self) -> List[BaseStrategy]:
        """获取所有策略的默认实例"""
        return [self.get(sid) for sid in self._strategies if self.get(sid) is not None]

    def get_by_group(self, group: str) -> List[BaseStrategy]:
        """按策略族获取实例"""
        sids = self._group_index.get(group, [])
        return [s for s in (self.get(sid) for sid in sids) if s is not None]

    def list_ids(self) -> List[str]:
        """列出所有已注册策略ID"""
        return list(self._strategies.keys())

    def list_groups(self) -> Dict[str, List[str]]:
        """列出所有策略族及其成员"""
        return dict(self._group_index)

    def get_stats(self) -> Dict[str, Any]:
        """获取注册中心统计"""
        return {
            "total": len(self._strategies),
            "groups": {
                g: len(ids) for g, ids in self._group_index.items()
            },
            "strategies": [
                {
                    "id": self.get(sid).strategy_id,
                    "group": self.get(sid).strategy_group,
                    "version": self.get(sid).version,
                    "exec_count": self.get(sid).execution_count,
                }
                for sid in self._strategies
            ],
        }


# ============================================================
#  便捷函数
# ============================================================
def get_strategy_registry() -> StrategyRegistry:
    """获取全局策略注册中心"""
    return StrategyRegistry()
