"""
统一数据源接口

所有数据源 Provider 必须继承 BaseDataProvider 并实现 get_data() 方法。
enrichment_router 会并行调用所有注册的 Provider，汇总数据。

设计原则：
  - 每个 Provider 独立文件，职责单一
  - 失败不抛异常，返回 {"status": "failed", "error": "..."}
  - 必须标注 data_source + fetched_at (数据真实度要求)
"""
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class BaseDataProvider(ABC):
    """数据源抽象基类"""

    def __init__(self, name: str, cache_ttl_seconds: int = 3600):
        """
        Args:
            name: 数据源名称 (如 "finnhub_fundamental", "insider_transactions")
            cache_ttl_seconds: 缓存有效期 (秒)，默认 1 小时
        """
        self.name = name
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Dict] = {}

    @abstractmethod
    def get_data(self, symbol: str, **kwargs) -> Dict[str, Any]:
        """
        获取数据

        Args:
            symbol: 股票代码 (如 "US.AAPL", "US.NVDA")
            **kwargs: 额外参数 (如 time_range, limit 等)

        Returns:
            {
                "status": "ok" | "failed",
                "data": {...},  # status=ok 时必填
                "error": "...", # status=failed 时必填
                "data_source": "finnhub/stock/metric",
                "fetched_at": "2026-06-17T05:30:00Z",
            }
        """
        pass

    def _is_cache_valid(self, symbol: str) -> bool:
        """检查缓存是否有效"""
        if symbol not in self._cache:
            return False
        cached = self._cache[symbol]
        from app.utils.timezone import now_utc
        from datetime import timezone
        # DEB-20260903 #25: fetched_at 缺失/naive 时按过期处理 —
        # 原防御默认值 "2000-01-01" 无 Z 后缀, .replace("Z",...) 为 no-op,
        # fromisoformat 返回 naive, 与 aware 的 now_utc() 相减必抛 TypeError;
        # 现 naive 一律视为 UTC, 缺失/解析异常一律按过期(返回 False)
        try:
            fetched_at = datetime.fromisoformat(cached.get("fetched_at", "").replace("Z", "+00:00"))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        age_seconds = (now_utc() - fetched_at).total_seconds()
        return age_seconds < self.cache_ttl

    def _get_cached(self, symbol: str) -> Optional[Dict]:
        """获取缓存"""
        if self._is_cache_valid(symbol):
            return self._cache[symbol]
        return None

    def _set_cache(self, symbol: str, data: Dict):
        """设置缓存"""
        self._cache[symbol] = data

    def get_with_cache(self, symbol: str, **kwargs) -> Dict[str, Any]:
        """带缓存的获取 (子类可直接调用)"""
        cached = self._get_cached(symbol)
        if cached:
            logger.debug(f"[{self.name}] Cache hit: {symbol}")
            return cached
        
        result = self.get_data(symbol, **kwargs)
        if result.get("status") == "ok":
            self._set_cache(symbol, result)
        return result

    def clear_cache(self, symbol: Optional[str] = None):
        """清除缓存"""
        if symbol:
            self._cache.pop(symbol, None)
        else:
            self._cache.clear()
