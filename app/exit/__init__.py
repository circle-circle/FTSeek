"""
退出模块可插拔框架 (Exit Plug-in Framework)

独立于现有退出管线，用于回测/分析/辩论引擎的退出评估。
不直接替换 position_exit_monitor.py 中的逻辑 (Overlay 模式)。

核心组件:
- ExitProfile: 退出配置剖面 (可序列化)
- ExitDecision: 退出决策 (复用现有结构)
- BaseExitModule: 退出模块基类
- ExitModuleRegistry: 退出模块注册中心

退出模块:
- AtrExitModule: ATR 倍数止损
- TrailingExitModule: Chandelier 追踪止损
- ScaleOutExitModule: 分层止盈
"""
from app.exit.base import (
    ExitProfile,
    ExitDecision,
    BaseExitModule,
)
from app.exit.registry import ExitModuleRegistry, get_exit_registry


def _auto_register() -> None:
    """自动注册所有内置退出模块"""
    from app.exit.atr_exit import AtrExitModule
    from app.exit.trailing_exit import TrailingExitModule
    from app.exit.scale_out_exit import ScaleOutExitModule

    registry = get_exit_registry()
    for mod_cls in [AtrExitModule, TrailingExitModule, ScaleOutExitModule]:
        if mod_cls.name not in registry.list_available():
            registry.register(mod_cls)


_auto_register()


__all__ = [
    "ExitProfile",
    "ExitDecision",
    "BaseExitModule",
    "ExitModuleRegistry",
    "get_exit_registry",
]
