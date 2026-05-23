"""
因子可插拔框架 (Factor Plug-in Framework)

独立于现有信号管线，用于回测/分析/辩论引擎的因子计算。
不直接替换 signal_pipeline.py 中的逻辑 (Overlay 模式)。

核心组件:
- BaseFactor: 因子基类 (所有因子必须实现)
- FactorValue: 因子输出标准结构
- FactorParams: 因子参数 (可扫描)
- FactorRegistry: 因子注册中心 (单例)

首批因子:
- SSS (SuperSoldScore): 超卖深度
- GDIAN (GradientDirectionIndex): 趋势方向
- MFFF (MomentumFlowFilterFactor): 动量过滤
"""
from app.factors.base import (
    BaseFactor,
    FactorValue,
    FactorParams,
    FactorTimeframe,
)
from app.factors.registry import FactorRegistry, get_registry


def _auto_register() -> None:
    """自动注册所有内置因子"""
    from app.factors.sss import SSSFactor
    from app.factors.gdian import GDIANFactor
    from app.factors.mfff import MFFFFactor

    registry = get_registry()
    for factor_cls in [SSSFactor, GDIANFactor, MFFFFactor]:
        if factor_cls.name not in registry.list_available():
            registry.register(factor_cls)


# 模块加载时自动注册
_auto_register()


__all__ = [
    "BaseFactor",
    "FactorValue",
    "FactorParams",
    "FactorTimeframe",
    "FactorRegistry",
    "get_registry",
]
