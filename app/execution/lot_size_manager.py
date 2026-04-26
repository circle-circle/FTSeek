"""
港股整手股数管理器 (LotSizeManager)

架构:
  Futu API (启动时批量获取) → 内存缓存 → 本地缓存文件 → 配置文件 → 安全默认值

解决了旧方案的 3 个硬编码问题:
  1. config_container.py 中的 hk_lot_sizes 字典
  2. trade_executor.py 中的 LOT_SIZE_MAP 字典
  3. 默认值 100 股的危险性

用法:
    from app.execution.lot_size_manager import get_lot_size_manager
    
    # 启动时 (main.py)
    lsm = get_lot_size_manager()
    await lsm.initialize_from_futu(quote_ctx, watchlist_codes)
    
    # 下单前 (trade_executor.py)
    lot_size = lsm.get_lot_size("HK.00700")  # → 100
"""

import logging
import time
import json
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.execution.lot_size")

# 全局单例
_lot_size_manager = None


class LotSizeManager:
    """
    港股整手股数管理器 — 动态查询 + 智能缓存 + 多级降级
    
    优先级:
    1. 内存缓存 (从 Futu API 动态获取)
    2. 本地缓存文件 (上次 API 查询结果)
    3. 配置文件 (config_center 中的 stock_pool.lot_sizes)
    4. 安全默认值 (100 股，但会记录 WARNING)
    """
    
    def __init__(self, cache_file: str = None):
        self._cache: Dict[str, int] = {}
        self._cache_file = cache_file or str(
            Path(__file__).parent.parent.parent / "data" / "lot_size_cache.json"
        )
        self._last_refresh: Optional[float] = None
        self._refresh_interval = 86400  # 24 小时刷新一次
        self._initialized = False
        
        # 加载本地缓存
        self._load_cache_file()
    
    async def initialize_from_futu(self, quote_ctx, codes: List[str]) -> Dict[str, int]:
        """
        启动时从 Futu API 批量获取整手股数
        
        Args:
            quote_ctx: 富途行情上下文
            codes: 股票代码列表 (如 ["HK.00700", "HK.09988"])
        
        Returns:
            {"HK.00700": 100, "HK.00981": 500, ...}
        """
        try:
            # 批量获取快照 (包含 lot_size 字段)
            ret, data = quote_ctx.get_market_snapshot(codes)
            if ret == 0 and data is not None and not data.empty:
                new_count = 0
                for _, row in data.iterrows():
                    code = row.get('code', '')
                    lot_size = int(row.get('lot_size', 0))
                    if code and lot_size > 0:
                        if code not in self._cache or self._cache[code] != lot_size:
                            logger.info(
                                "[LotSize] %s: %d 股/手%s",
                                code, lot_size,
                                " (已更新)" if code in self._cache else ""
                            )
                        self._cache[code] = lot_size
                        new_count += 1
                
                self._last_refresh = time.time()
                self._initialized = True
                self._save_cache_file()
                logger.info(
                    "[LotSize] ✅ 从 Futu API 获取 %d 只股票的整手股数",
                    new_count
                )
                return self._cache.copy()
            else:
                logger.warning("[LotSize] ⚠️ Futu API 查询返回空数据 (ret=%s)", ret)
        except Exception as e:
            logger.error("[LotSize] ❌ Futu API 异常: %s", e, exc_info=True)
        
        # API 失败时从本地缓存恢复
        if self._cache:
            logger.warning("[LotSize] API 失败，使用本地缓存 (%d 只)", len(self._cache))
            self._initialized = True
        
        return self._cache.copy()
    
    def get_lot_size(self, code: str) -> int:
        """
        获取整手股数 (带 4 级降级逻辑)
        
        Args:
            code: 股票代码 (如 "HK.00700" 或 "00700")
        
        Returns:
            每手股数
        """
        # 标准化代码
        if "." not in code:
            code = f"HK.{code}"
        
        # Level 1: 内存缓存
        if code in self._cache:
            return self._cache[code]
        
        # Level 2: 本地缓存文件
        if not self._cache:  # 只在内存空时加载
            self._load_cache_file()
        if code in self._cache:
            return self._cache[code]
        
        # Level 3: 配置文件 (config_center)
        try:
            from config.config_center import get_config_center
            cc = get_config_center()
            lot_sizes = cc.get('stock_pool.lot_sizes', {})
            if code in lot_sizes:
                lot = int(lot_sizes[code])
                self._cache[code] = lot
                logger.info("[LotSize] %s: %d 股/手 (来自配置文件)", code, lot)
                return lot
        except Exception as e:
            logger.debug("[LotSize] 从配置文件读取失败: %s", e)
        
        # Level 4: 安全默认值 (记录警告)
        logger.warning(
            "[LotSize] ⚠️ %s 无法获取整手股数 (4 级降级全部失败)，"
            "使用默认值 100 股/手。如非 100 股/手可能导致碎股!",
            code
        )
        return 100
    
    def enforce_lot_size(self, code: str, desired_quantity: int) -> int:
        """
        对齐到整手倍数 (向下取整)
        
        Args:
            code: 股票代码
            desired_quantity: 期望数量
        
        Returns:
            对齐后的整手数量
        """
        lot_size = self.get_lot_size(code)
        if desired_quantity % lot_size != 0:
            new_qty = (desired_quantity // lot_size) * lot_size
            logger.warning(
                "[碎股对齐] %s 原数量 %d → 对齐整手(%d) → %d",
                code, desired_quantity, lot_size, new_qty
            )
            return max(new_qty, 0)
        return desired_quantity
    
    def get_all_lot_sizes(self) -> Dict[str, int]:
        """获取所有已知的整手股数"""
        return self._cache.copy()
    
    def needs_refresh(self) -> bool:
        """是否需要刷新"""
        if not self._initialized:
            return True
        if self._last_refresh is None:
            return True
        return (time.time() - self._last_refresh) > self._refresh_interval
    
    def _load_cache_file(self):
        """从本地文件加载缓存"""
        try:
            path = Path(self._cache_file)
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                self._cache.update(data.get('lot_sizes', {}))
                self._last_refresh = data.get('last_refresh', 0)
                self._initialized = data.get('initialized', False)
                logger.debug(
                    "[LotSize] 加载本地缓存: %d 只股票",
                    len(self._cache)
                )
        except Exception as e:
            logger.debug("[LotSize] 加载缓存文件失败: %s", e)
    
    def _save_cache_file(self):
        """保存缓存到本地文件"""
        try:
            path = Path(self._cache_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w') as f:
                json.dump({
                    'lot_sizes': self._cache,
                    'last_refresh': self._last_refresh,
                    'initialized': self._initialized,
                    'count': len(self._cache),
                }, f, indent=2)
        except Exception as e:
            logger.debug("[LotSize] 保存缓存文件失败: %s", e)
    
    def get_status(self) -> dict:
        """获取管理器状态"""
        return {
            "initialized": self._initialized,
            "cached_count": len(self._cache),
            "last_refresh": time.time() - self._last_refresh if self._last_refresh else None,
            "needs_refresh": self.needs_refresh(),
            "cache_file": self._cache_file,
        }


def get_lot_size_manager() -> LotSizeManager:
    """获取全局 LotSizeManager 单例"""
    global _lot_size_manager
    if _lot_size_manager is None:
        _lot_size_manager = LotSizeManager()
    return _lot_size_manager


def reset_lot_size_manager():
    """重置单例 (用于测试)"""
    global _lot_size_manager
    _lot_size_manager = None
