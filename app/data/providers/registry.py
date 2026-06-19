"""
数据源注册表

自动发现并注册所有 Provider，支持运行时动态添加。

用法：
    from app.data.providers.registry import ProviderRegistry
    
    registry = ProviderRegistry()
    registry.auto_discover()  # 扫描 providers/ 目录
    
    # 获取所有数据
    all_data = registry.get_all("US.AAPL")
    
    # 获取特定数据源
    insider_data = registry.get("insider_transactions", "US.AAPL")
"""
import importlib
import inspect
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.data.providers.base import BaseDataProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """数据源注册表"""

    def __init__(self):
        self._providers: Dict[str, BaseDataProvider] = {}

    def register(self, provider: BaseDataProvider):
        """注册一个 Provider"""
        self._providers[provider.name] = provider
        logger.info(f"[ProviderRegistry] Registered: {provider.name}")

    def unregister(self, name: str):
        """注销一个 Provider"""
        self._providers.pop(name, None)

    def get(self, name: str, symbol: str, **kwargs) -> Dict[str, Any]:
        """获取特定 Provider 的数据"""
        provider = self._providers.get(name)
        if not provider:
            return {"status": "failed", "error": f"Provider not found: {name}"}
        return provider.get_with_cache(symbol, **kwargs)

    def get_all(self, symbol: str, parallel: bool = True, **kwargs) -> Dict[str, Any]:
        """
        并行获取所有 Provider 的数据

        Args:
            symbol: 股票代码
            parallel: 是否并行执行 (默认 True)
            **kwargs: 传递给每个 Provider 的参数

        Returns:
            {
                "provider_1": {...},
                "provider_2": {...},
                "_meta": {"total_providers": 5, "success": 4, "failed": 1}
            }
        """
        results = {}
        
        if parallel:
            with ThreadPoolExecutor(max_workers=min(len(self._providers), 10)) as executor:
                futures = {
                    executor.submit(provider.get_with_cache, symbol, **kwargs): name
                    for name, provider in self._providers.items()
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        results[name] = future.result(timeout=30)
                    except Exception as e:
                        logger.error(f"[ProviderRegistry] {name} failed: {e}")
                        results[name] = {"status": "failed", "error": str(e)}
        else:
            for name, provider in self._providers.items():
                try:
                    results[name] = provider.get_with_cache(symbol, **kwargs)
                except Exception as e:
                    logger.error(f"[ProviderRegistry] {name} failed: {e}")
                    results[name] = {"status": "failed", "error": str(e)}

        # 统计
        success = sum(1 for v in results.values() if v.get("status") == "ok")
        failed = len(results) - success
        results["_meta"] = {
            "total_providers": len(self._providers),
            "success": success,
            "failed": failed,
        }
        
        return results

    def list_providers(self) -> List[str]:
        """列出所有注册的 Provider"""
        return list(self._providers.keys())

    def auto_discover(self):
        """
        自动发现 providers/ 目录下的所有 Provider
        
        扫描规则：
          - 文件名匹配 *_provider.py
          - 类名匹配 *Provider
          - 继承 BaseDataProvider
        """
        providers_dir = Path(__file__).parent
        
        for file_path in providers_dir.glob("*_provider.py"):
            module_name = file_path.stem  # 如 "finnhub_insider_provider"
            
            try:
                # 动态导入
                module = importlib.import_module(f"app.data.providers.{module_name}")
                
                # 查找所有 Provider 类
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if (issubclass(obj, BaseDataProvider) 
                        and obj is not BaseDataProvider
                        and name.endswith("Provider")):
                        try:
                            instance = obj()
                            self.register(instance)
                        except Exception as e:
                            logger.error(f"[ProviderRegistry] Failed to instantiate {name}: {e}")
            
            except Exception as e:
                logger.error(f"[ProviderRegistry] Failed to load {module_name}: {e}")


# 全局单例
_global_registry: Optional[ProviderRegistry] = None


def get_global_registry() -> ProviderRegistry:
    """获取全局注册表 (懒加载 + 自动发现)"""
    global _global_registry
    if _global_registry is None:
        _global_registry = ProviderRegistry()
        _global_registry.auto_discover()
    return _global_registry
