"""
Dependency Injection Container — 依赖注入容器

目的：统一管理所有组件的生命周期和依赖关系，消除全局单例。

使用方式:
    from app.core.container import Container
    
    container = Container()
    
    # 注册服务
    container.register("notifier", DingTalkNotifier)
    container.register("executor", TradeExecutor, depends=["notifier", "risk_manager"])
    
    # 解析服务（自动注入依赖）
    executor = container.resolve("executor")
"""

import logging
from typing import Optional, Dict, Any, Type, Callable, List, Set
from functools import wraps

logger = logging.getLogger("ftseek.container")


class DependencyError(Exception):
    """依赖解析错误"""
    pass


class CircularDependencyError(DependencyError):
    """循环依赖错误"""
    pass


class Container:
    """
    轻量级依赖注入容器
    
    特性:
    - 延迟初始化（服务只在首次 resolve 时创建）
    - 自动依赖注入
    - 循环依赖检测
    - 单例模式（默认）或工厂模式
    """
    
    def __init__(self):
        self._factories: Dict[str, Callable] = {}
        self._instances: Dict[str, Any] = {}
        self._dependencies: Dict[str, List[str]] = {}
        self._singleton: Dict[str, bool] = {}
        self._resolving: Set[str] = set()  # 循环依赖检测
    
    def register(self, name: str, factory: Callable = None,
                 depends: List[str] = None, singleton: bool = True):
        """
        注册服务
        
        Args:
            name: 服务名称
            factory: 工厂函数或类（如果为 None，后续可通过 set_instance 设置实例）
            depends: 依赖的其他服务名称列表
            singleton: 是否单例（默认 True）
        """
        if factory is None:
            # 延迟注册模式，先注册名称
            self._factories[name] = lambda: None
        else:
            self._factories[name] = factory
        
        self._dependencies[name] = depends or []
        self._singleton[name] = singleton
        
        # 如果已存在实例且重新注册，清除旧实例
        if name in self._instances:
            del self._instances[name]
        
        logger.debug(f"注册服务: {name} (依赖={depends or []}, 单例={singleton})")
    
    def set_instance(self, name: str, instance: Any):
        """直接设置实例（用于已有对象的注册）"""
        self._instances[name] = instance
        if name not in self._factories:
            self._factories[name] = lambda: instance
        logger.debug(f"设置实例: {name}")
    
    def resolve(self, name: str) -> Any:
        """
        解析服务
        
        Args:
            name: 服务名称
        
        Returns:
            服务实例
        
        Raises:
            DependencyError: 服务未注册
            CircularDependencyError: 循环依赖
        """
        # 已缓存的实例
        if name in self._instances:
            return self._instances[name]
        
        # 未注册
        if name not in self._factories:
            raise DependencyError(f"服务未注册: {name}")
        
        # 循环依赖检测
        if name in self._resolving:
            chain = " -> ".join(list(self._resolving) + [name])
            raise CircularDependencyError(f"循环依赖: {chain}")
        
        # 解析依赖
        self._resolving.add(name)
        try:
            deps = self._resolve_dependencies(name)
            factory = self._factories[name]
            
            # 如果 factory 是类，传入依赖作为构造函数参数
            if isinstance(factory, type):
                instance = factory(**deps) if deps else factory()
            elif callable(factory):
                # 工厂函数：如果接受 kwargs，传入依赖
                import inspect
                sig = inspect.signature(factory)
                if sig.parameters:
                    instance = factory(**deps) if deps else factory()
                else:
                    instance = factory()
            else:
                instance = factory
            
            # 缓存单例
            if self._singleton.get(name, True):
                self._instances[name] = instance
            
            return instance
        finally:
            self._resolving.discard(name)
    
    def has(self, name: str) -> bool:
        """检查服务是否已注册"""
        return name in self._factories
    
    def get_dependency_graph(self) -> Dict[str, List[str]]:
        """获取依赖关系图"""
        return dict(self._dependencies)
    
    def validate(self) -> List[str]:
        """
        验证所有依赖是否可解析
        
        Returns:
            缺失的依赖列表
        """
        missing = []
        for name, deps in self._dependencies.items():
            for dep in deps:
                if dep not in self._factories:
                    missing.append(f"{name} -> {dep}")
        return missing
    
    def reset(self):
        """重置容器（清除所有实例）"""
        self._instances.clear()
        self._resolving.clear()
        logger.info("容器已重置")
    
    def _resolve_dependencies(self, name: str) -> Dict[str, Any]:
        """解析指定服务的所有依赖"""
        deps = {}
        for dep_name in self._dependencies.get(name, []):
            deps[dep_name] = self.resolve(dep_name)
        return deps


# ===== 全局容器 =====

_global_container: Optional[Container] = None


def get_container() -> Container:
    """获取全局容器"""
    global _global_container
    if _global_container is None:
        _global_container = Container()
    return _global_container


def set_container(container: Container):
    """设置全局容器"""
    global _global_container
    _global_container = container


def register(name: str, factory: Callable = None, depends: List[str] = None, 
             singleton: bool = True):
    """便捷注册"""
    get_container().register(name, factory, depends, singleton)


def resolve(name: str) -> Any:
    """便捷解析"""
    return get_container().resolve(name)


def resolve_safe(name: str, default: Any = None) -> Any:
    """安全解析，失败返回默认值"""
    try:
        return get_container().resolve(name)
    except DependencyError:
        return default
