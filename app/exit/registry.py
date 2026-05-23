"""
退出模块注册中心 (Exit Module Registry)

管理所有已注册退出模块的创建、实例化和配置加载。
单例模式 — 整个应用共享一个注册中心。

支持:
- 从 YAML 加载 ExitProfile
- 按名称创建退出模块实例
- 组合多个模块评估 (stop_loss + take_profit)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml

from app.exit.base import BaseExitModule, ExitDecision, ExitProfile

logger = logging.getLogger(__name__)

# 默认配置文件路径
DEFAULT_PROFILES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "exit_profiles.yaml"


class ExitModuleRegistry:
    """
    退出模块注册中心 — 单例
    
    Usage:
        registry = get_exit_registry()
        
        # 注册退出模块类
        registry.register(AtrExitModule)
        
        # 加载 profiles
        registry.load_profiles("config/exit_profiles.yaml")
        
        # 获取模块实例
        module = registry.get_module("atr_exit")
        
        # 获取 profile
        profile = registry.get_profile("stable_15m_base")
        
        # 组合评估
        decisions = registry.evaluate_all(position, ctx, "stable_15m_base")
    """
    
    _instance: Optional[ExitModuleRegistry] = None
    _module_classes: Dict[str, Type[BaseExitModule]] = {}
    
    def __new__(cls) -> ExitModuleRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._module_classes = {}
            cls._instance._profiles: Dict[str, ExitProfile] = {}
        return cls._instance
    
    def register(self, module_cls: Type[BaseExitModule]) -> Type[BaseExitModule]:
        """
        注册退出模块类 — 可作为装饰器使用
        
        Usage:
            @registry.register
            class MyExitModule(BaseExitModule):
                name = "my_exit"
                ...
        """
        if not hasattr(module_cls, 'name') or not module_cls.name:
            raise ValueError(f"{module_cls.__name__} must define class attribute 'name'")
        
        name = module_cls.name
        if name in self._module_classes:
            existing = self._module_classes[name]
            if existing is not module_cls:
                logger.warning(
                    f"Exit module '{name}' already registered as {existing.__name__}, "
                    f"overwriting with {module_cls.__name__}"
                )
        
        self._module_classes[name] = module_cls
        logger.debug(f"Registered exit module: {name} ({module_cls.__name__})")
        return module_cls
    
    def get_module(self, name: str) -> BaseExitModule:
        """
        获取退出模块实例
        
        Args:
            name: 模块名称 (必须已注册)
        
        Returns:
            BaseExitModule: 模块实例
        
        Raises:
            KeyError: 模块未注册
        """
        if name not in self._module_classes:
            available = ", ".join(sorted(self._module_classes.keys()))
            raise KeyError(
                f"Exit module '{name}' not registered. Available: [{available}]"
            )
        
        return self._module_classes[name]()
    
    def list_available(self) -> List[str]:
        """列出所有已注册模块名称"""
        return sorted(self._module_classes.keys())
    
    # ── Profile 管理 ──
    
    def load_profiles(self, yaml_path: str = None) -> int:
        """
        从 YAML 加载所有 exit profile
        
        Args:
            yaml_path: YAML 文件路径 (默认: config/exit_profiles.yaml)
        
        Returns:
            int: 成功加载的 profile 数量
        """
        path = Path(yaml_path) if yaml_path else DEFAULT_PROFILES_PATH
        
        if not path.exists():
            logger.warning(f"Exit profiles file not found: {path}")
            return 0
        
        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load exit profiles: {e}")
            return 0
        
        profiles_data = data.get("exit_profiles", {})
        count = 0
        
        for name, profile_dict in profiles_data.items():
            try:
                profile_dict["name"] = name
                profile = ExitProfile.from_dict(profile_dict)
                self._profiles[name] = profile
                count += 1
                logger.debug(f"Loaded exit profile: {name}")
            except Exception as e:
                logger.error(f"Failed to load profile '{name}': {e}")
        
        logger.info(f"Loaded {count} exit profiles from {path}")
        return count
    
    def get_profile(self, name: str) -> Optional[ExitProfile]:
        """
        获取指定名称的 ExitProfile
        
        Args:
            name: Profile 名称
        
        Returns:
            ExitProfile or None
        """
        return self._profiles.get(name)
    
    def list_profiles(self) -> List[str]:
        """列出所有已加载的 profile 名称"""
        return sorted(self._profiles.keys())
    
    def add_profile(self, profile: ExitProfile) -> None:
        """手动添加/覆盖一个 profile"""
        self._profiles[profile.name] = profile
    
    # ── 组合评估 ──
    
    def evaluate_all(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile_name: str,
    ) -> List[ExitDecision]:
        """
        用指定 profile 的所有退出模块评估
        
        返回所有模块的决策列表 (由调用方决定优先级合并策略)
        
        Args:
            position: 持仓对象
            ctx: 上下文
            profile_name: Profile 名称
        
        Returns:
            List[ExitDecision]: 所有模块的决策
        """
        profile = self.get_profile(profile_name)
        if profile is None:
            logger.warning(f"Profile '{profile_name}' not found")
            return []
        
        decisions = []
        
        # 根据 profile 配置选择评估的模块
        sl_type = profile.stop_loss_type
        tp_type = profile.take_profit_type
        
        # 止损模块
        if sl_type in ("fixed_pct", "atr_multi"):
            try:
                atr_mod = self.get_module("atr_exit")
                decisions.append(atr_mod.evaluate(position, ctx, profile))
            except KeyError:
                pass
        
        if sl_type == "chandelier" or tp_type == "trailing":
            try:
                trail_mod = self.get_module("trailing_exit")
                decisions.append(trail_mod.evaluate(position, ctx, profile))
            except KeyError:
                pass
        
        # 止盈模块
        if tp_type == "scale_out":
            try:
                scale_mod = self.get_module("scale_out_exit")
                decisions.append(scale_mod.evaluate(position, ctx, profile))
            except KeyError:
                pass
        
        # 时间止损
        if profile.max_holding_minutes > 0:
            holding = ctx.get("holding_minutes", 0)
            if holding >= profile.max_holding_minutes:
                decisions.append(ExitDecision(
                    action="CLOSE",
                    qty_pct=1.0,
                    reason="TIME_STOP",
                    priority=3,
                    metadata={
                        "holding_minutes": holding,
                        "max_holding_minutes": profile.max_holding_minutes,
                    },
                ))
        
        return decisions
    
    def evaluate_best(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile_name: str,
    ) -> ExitDecision:
        """
        评估并返回最高优先级的退出决策
        
        合并策略:
        1. 收集所有模块决策
        2. 过滤掉 NO_ACTION
        3. 按 priority 降序排序
        4. 返回最高优先级的决策
        
        Args:
            position: 持仓对象
            ctx: 上下文
            profile_name: Profile 名称
        
        Returns:
            ExitDecision: 最高优先级的决策 (可能是 NO_ACTION)
        """
        decisions = self.evaluate_all(position, ctx, profile_name)
        
        # 过滤 NO_ACTION
        active = [d for d in decisions if d.action != "NO_ACTION"]
        
        if not active:
            return ExitDecision(action="NO_ACTION", reason="all_modules_pass")
        
        # 按 priority 降序排序
        active.sort(key=lambda d: d.priority, reverse=True)
        return active[0]
    
    # ── 测试/重置 ──
    
    def clear(self):
        """清空所有注册和 profiles (主要用于测试)"""
        self._module_classes.clear()
        self._profiles.clear()
    
    @classmethod
    def reset_singleton(cls):
        """重置单例 (主要用于测试)"""
        cls._instance = None
        cls._module_classes = {}


def get_exit_registry() -> ExitModuleRegistry:
    """获取全局注册中心实例"""
    return ExitModuleRegistry()
