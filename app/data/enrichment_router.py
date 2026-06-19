"""
数据增强路由器

统一入口，并行获取所有维度的数据，供下游（plan/pipeline/factor_engine）使用。

用法：
    from app.data.enrichment_router import get_enrichment
    
    data = get_enrichment("US.AAPL")
    # data = {
    #     "fundamental": {...},
    #     "insider": {...},
    #     "news": {...},
    #     "_meta": {"total": 3, "success": 3, "failed": 0}
    # }
"""
import logging
from typing import Dict, Any

from app.data.providers.registry import get_global_registry

logger = logging.getLogger(__name__)


def get_enrichment(symbol: str, providers: list = None, **kwargs) -> Dict[str, Any]:
    """
    获取全维度数据增强
    
    Args:
        symbol: 股票代码 (如 "US.AAPL")
        providers: 指定要获取的 Provider 列表 (默认全部)
        **kwargs: 传递给每个 Provider 的参数
    
    Returns:
        {
            "fundamental": {...},
            "insider": {...},
            "news": {...},
            "_meta": {"total_providers": 3, "success": 3, "failed": 0}
        }
    """
    registry = get_global_registry()
    
    if providers:
        # 只获取指定的 Provider
        results = {}
        for name in providers:
            results[name] = registry.get(name, symbol, **kwargs)
        
        # 统计
        success = sum(1 for v in results.values() if v.get("status") == "ok")
        results["_meta"] = {
            "total_providers": len(providers),
            "success": success,
            "failed": len(providers) - success,
        }
        return results
    else:
        # 获取全部
        return registry.get_all(symbol, parallel=True, **kwargs)


def get_fundamental(symbol: str) -> Dict[str, Any]:
    """快捷方法：只获取基本面"""
    registry = get_global_registry()
    return registry.get("fundamental", symbol)


def get_insider(symbol: str) -> Dict[str, Any]:
    """快捷方法：只获取内部人交易"""
    registry = get_global_registry()
    return registry.get("insider", symbol)
