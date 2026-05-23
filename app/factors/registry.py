"""
因子注册中心 (Factor Registry)

管理所有已注册因子的创建、实例化和参数扫描。
单例模式 — 整个应用共享一个注册中心。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type

from app.factors.base import BaseFactor, FactorParams, FactorValue

logger = logging.getLogger(__name__)


class FactorRegistry:
    """
    因子注册中心 — 单例
    
    Usage:
        registry = FactorRegistry()
        
        # 注册因子类
        registry.register(SSSFactor)
        registry.register(GDIANFactor)
        
        # 实例化因子
        factor = registry.create("SSS", FactorParams({"threshold": -1.5}))
        
        # 列出所有已注册因子
        names = registry.list_available()
        
        # 网格扫描
        results = registry.scan_params("SSS", symbol, market_data)
    """

    _instance: Optional[FactorRegistry] = None
    _factor_classes: Dict[str, Type[BaseFactor]] = {}

    def __new__(cls) -> FactorRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._factor_classes = {}
        return cls._instance

    def register(self, factor_cls: Type[BaseFactor]) -> Type[BaseFactor]:
        """
        注册因子类 — 可作为装饰器使用
        
        Usage:
            @registry.register
            class MyFactor(BaseFactor):
                name = "MY_FACTOR"
                ...
        """
        if not hasattr(factor_cls, 'name') or not factor_cls.name:
            raise ValueError(f"{factor_cls.__name__} must define class attribute 'name'")

        name = factor_cls.name
        if name in self._factor_classes:
            existing = self._factor_classes[name]
            if existing is not factor_cls:
                logger.warning(
                    f"Factor '{name}' already registered as {existing.__name__}, "
                    f"overwriting with {factor_cls.__name__}"
                )

        self._factor_classes[name] = factor_cls
        logger.debug(f"Registered factor: {name} ({factor_cls.__name__})")
        return factor_cls

    def create(
        self,
        name: str,
        params: Optional[FactorParams] = None,
    ) -> BaseFactor:
        """
        实例化因子
        
        Args:
            name: 因子名称 (必须已注册)
            params: 因子参数 (可选，使用默认参数)
        
        Returns:
            BaseFactor: 因子实例
        
        Raises:
            KeyError: 因子未注册
        """
        if name not in self._factor_classes:
            available = ", ".join(sorted(self._factor_classes.keys()))
            raise KeyError(
                f"Factor '{name}' not registered. Available: [{available}]"
            )

        factor_cls = self._factor_classes[name]
        factor = factor_cls(params=params)

        if not factor.validate_params():
            raise ValueError(
                f"Factor '{name}' failed parameter validation: {factor.params.params}"
            )

        return factor

    def compute_all(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
        factor_names: Optional[List[str]] = None,
    ) -> Dict[str, FactorValue]:
        """
        批量计算多个因子
        
        Args:
            symbol: 标的代码
            market_data: 市场数据
            ctx: 额外上下文
            factor_names: 要计算的因子名称列表 (None = 全部)
        
        Returns:
            Dict[str, FactorValue]: {因子名: 因子值}
        """
        names = factor_names or list(self._factor_classes.keys())
        results = {}

        for name in names:
            try:
                factor = self.create(name)
                results[name] = factor.compute(symbol, market_data, ctx)
            except Exception as e:
                logger.error(f"Factor '{name}' compute failed: {e}")
                # 不中断其他因子的计算

        return results

    def scan_params(
        self,
        name: str,
        symbol: str,
        market_data: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> List[FactorValue]:
        """
        网格扫描 — 遍历参数空间所有组合
        
        Args:
            name: 因子名称
            symbol: 标的代码
            market_data: 市场数据
            ctx: 额外上下文
        
        Returns:
            List[FactorValue]: 所有参数组合的计算结果
        """
        # 先用默认参数创建实例以获取 param_space
        default_factor = self.create(name)
        combinations = default_factor.get_param_combinations()

        results = []
        for params in combinations:
            try:
                factor = self.create(name, params=params)
                result = factor.compute(symbol, market_data, ctx)
                results.append(result)
            except Exception as e:
                logger.error(f"Factor '{name}' scan failed for params {params.params}: {e}")

        logger.info(f"Factor '{name}' scan: {len(results)}/{len(combinations)} completed")
        return results

    def list_available(self) -> List[str]:
        """列出所有已注册因子名称"""
        return sorted(self._factor_classes.keys())

    def describe_all(self) -> Dict[str, str]:
        """获取所有因子的描述"""
        descriptions = {}
        for name in self._factor_classes:
            try:
                factor = self.create(name)
                descriptions[name] = factor.describe()
            except Exception as e:
                descriptions[name] = f"Error: {e}"
        return descriptions

    def unregister(self, name: str) -> bool:
        """注销因子 (主要用于测试)"""
        if name in self._factor_classes:
            del self._factor_classes[name]
            return True
        return False

    def clear(self):
        """清空所有注册 (主要用于测试)"""
        self._factor_classes.clear()

    @classmethod
    def reset_singleton(cls):
        """重置单例 (主要用于测试)"""
        cls._instance = None
        cls._factor_classes = {}


def get_registry() -> FactorRegistry:
    """获取全局注册中心实例"""
    return FactorRegistry()
