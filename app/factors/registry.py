"""
因子注册中心 (FactorRegistry) — 统一管理因子的注册、发现、实例化

设计:
- 单例模式，全局唯一入口
- 支持自动发现 (auto-discover) app/factors/ 下的因子模块
- 支持运行时动态注册
- 按时间框架分组查询

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List, Optional, Type

from .base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class FactorRegistry:
    """
    因子注册中心 — 单例

    Usage:
        registry = FactorRegistry.instance()
        registry.register(SSSFactor)
        factor = registry.create("SSS", FactorParams(params={"threshold": -1.5}))
        value = factor.compute("US.AAPL", market_data)
    """

    _instance: Optional[FactorRegistry] = None
    _initialized: bool = False

    def __new__(cls) -> FactorRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._factors: Dict[str, Type[BaseFactor]] = {}
        self._instances: Dict[str, BaseFactor] = {}
        self._timeframe_index: Dict[str, List[str]] = {}
        self._initialized = True
        logger.info("[FactorRegistry] 初始化完成")

    @classmethod
    def instance(cls) -> FactorRegistry:
        """获取单例"""
        return cls()

    @classmethod
    def reset(cls):
        """重置单例 (仅测试用)"""
        cls._instance = None
        cls._initialized = False

    # ── 注册 ──

    def register(self, factor_cls: Type[BaseFactor]) -> Type[BaseFactor]:
        """
        注册因子类

        Args:
            factor_cls: BaseFactor 子类

        Returns:
            原始类 (可用作装饰器)
        """
        if not issubclass(factor_cls, BaseFactor):
            raise TypeError(f"{factor_cls.__name__} must extend BaseFactor")

        # 实例化获取 name
        probe = factor_cls()
        name = probe.name
        if not name:
            raise ValueError(f"{factor_cls.__name__}.name is empty")

        if name in self._factors:
            logger.warning("[FactorRegistry] 覆盖已有因子: %s", name)

        self._factors[name] = factor_cls

        # 时间框架索引
        tf = probe.timeframe.value
        if tf not in self._timeframe_index:
            self._timeframe_index[tf] = []
        if name not in self._timeframe_index[tf]:
            self._timeframe_index[tf].append(name)

        # 清除缓存实例
        self._instances.pop(name, None)

        logger.info("[FactorRegistry] 已注册: %s (tf=%s, v%s, params=%d combinations)",
                     name, tf, probe.version, len(probe.get_param_combinations()))
        return factor_cls

    def unregister(self, name: str) -> None:
        """注销因子"""
        cls = self._factors.pop(name, None)
        if cls:
            probe = cls()
            tf = probe.timeframe.value
            if tf in self._timeframe_index:
                self._timeframe_index[tf] = [
                    n for n in self._timeframe_index[tf] if n != name
                ]
        self._instances.pop(name, None)
        logger.info("[FactorRegistry] 已注销: %s", name)

    # ── 发现 ──

    def auto_discover(self) -> int:
        """
        自动发现并注册 app/factors/ 下的因子模块

        约定: 每个因子模块必须有一个与模块同名的类，
              或在模块级定义 FACTOR_CLASS 变量指向因子类。

        Returns:
            新注册的因子数量
        """
        count = 0
        factor_modules = [
            "app.factors.sss",
            "app.factors.gdian",
            "app.factors.mfff",
            "app.factors.vol_regime",
        ]

        for module_path in factor_modules:
            try:
                mod = importlib.import_module(module_path)

                # 方式1: FACTOR_CLASS 变量
                if hasattr(mod, "FACTOR_CLASS"):
                    self.register(mod.FACTOR_CLASS)
                    count += 1
                    continue

                # 方式2: 扫描模块中的 BaseFactor 子类
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (isinstance(attr, type)
                            and issubclass(attr, BaseFactor)
                            and attr is not BaseFactor
                            and getattr(attr, 'name', '')):
                        self.register(attr)
                        count += 1
                        break  # 每模块只注册一个

            except ImportError as e:
                logger.debug("[FactorRegistry] 跳过 %s: %s", module_path, e)
            except Exception as e:
                logger.warning("[FactorRegistry] 加载 %s 失败: %s", module_path, e)

        logger.info("[FactorRegistry] 自动发现完成: 注册 %d 个新因子", count)
        return count

    # ── 实例化 ──

    def create(self, name: str, params: Optional[FactorParams] = None) -> BaseFactor:
        """
        创建因子实例

        Args:
            name: 因子名称
            params: 参数 (None 使用默认)

        Returns:
            BaseFactor 实例
        """
        if name not in self._factors:
            raise KeyError(f"因子未注册: {name}. 已注册: {list(self._factors.keys())}")

        cls = self._factors[name]
        instance = cls(params=params)

        if not instance.validate_params():
            raise ValueError(f"参数验证失败: {name} with {params}")

        return instance

    def get_or_create(self, name: str, params: Optional[FactorParams] = None) -> BaseFactor:
        """获取缓存实例或创建新的 (仅当 params 为 None 时使用缓存)"""
        if params is None and name in self._instances:
            return self._instances[name]

        instance = self.create(name, params)

        if params is None:
            self._instances[name] = instance

        return instance

    # ── 查询 ──

    def list_available(self) -> List[str]:
        """列出所有已注册因子名称"""
        return sorted(self._factors.keys())

    def list_by_timeframe(self, timeframe: FactorTimeframe) -> List[str]:
        """按时间框架列出"""
        return self._timeframe_index.get(timeframe.value, [])

    def get_info(self, name: str) -> Dict[str, Any]:
        """获取因子详细信息"""
        if name not in self._factors:
            return {}

        cls = self._factors[name]
        probe = cls()
        return {
            "name": probe.name,
            "version": probe.version,
            "timeframe": probe.timeframe.value,
            "param_space": probe.param_space,
            "param_combinations": len(probe.get_param_combinations()),
            "description": probe.describe(),
        }

    def compute_all(self, symbol: str, market_data: Dict[str, Any],
                    ctx: Optional[Dict[str, Any]] = None,
                    timeframe: Optional[FactorTimeframe] = None
                    ) -> Dict[str, FactorValue]:
        """
        计算所有 (或指定时间框架) 因子的值

        Args:
            symbol: 标的代码
            market_data: 行情数据
            ctx: 额外上下文
            timeframe: 可选过滤 (None = 全部)

        Returns:
            Dict[factor_name, FactorValue]
        """
        results = {}
        names = (self.list_by_timeframe(timeframe)
                 if timeframe else self.list_available())

        for name in names:
            try:
                factor = self.get_or_create(name)
                value = factor.compute(symbol, market_data, ctx)
                results[name] = value
            except Exception as e:
                logger.warning("[FactorRegistry] 计算 %s 失败: %s", name, e)

        return results

    def __len__(self):
        return len(self._factors)

    def __contains__(self, name: str):
        return name in self._factors

    def __repr__(self):
        return f"<FactorRegistry({len(self._factors)} factors: {self.list_available()})>"


# ── 便捷函数 ──

def get_registry() -> FactorRegistry:
    """获取全局 FactorRegistry 单例 (便捷函数)"""
    return FactorRegistry.instance()
