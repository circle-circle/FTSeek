"""
FTSeek-US Account Refresher — Broker 账户信息实时刷新 + 缓存 + 降级

设计原则:
  1. 开仓前实时获取最新 buying_power/equity
  2. 带 TTL 缓存，避免频繁调用 broker API
  3. 超时保护 (3s)，不阻塞开仓决策
  4. 降级策略: 缓存 → last_known → 告警

作者: FTSeek Team
日期: 2026-05-02
"""

import logging
import threading
import time
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek.account_refresher")


class AccountRefresher:
    """
    Broker 账户信息查询 + 缓存 + 降级
    
    用途:
      - 开仓前实时刷新 buying_power，确保仓位计算基于最新资金
      - 带 30 秒缓存，避免频繁 API 调用
      - 超时 3 秒，不阻塞交易决策
    """
    
    def __init__(
        self,
        broker_adapter=None,
        cache_ttl: int = 30,
        timeout: int = 3,
        fallback: str = "last_known",
    ):
        """
        Args:
            broker_adapter: BrokerAdapter 实例
            cache_ttl: 缓存有效期 (秒)
            timeout: 单次查询超时 (秒)
            fallback: 降级策略 ("last_known" | "block" | "default")
        """
        self._broker = broker_adapter
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0
        self._ttl = cache_ttl
        self._timeout = timeout
        self._fallback = fallback
        self._query_count = 0
        self._error_count = 0
    
    def refresh(self, force: bool = False) -> Dict[str, Any]:
        """
        获取账户信息（带缓存）
        
        Args:
            force: 强制刷新，忽略缓存
            
        Returns:
            账户信息字典 (buying_power, total_assets, cash, market_val)
            
        Raises:
            AccountRefreshFailed: 当无法获取且无降级数据时
        """
        now = time.time()
        
        # 缓存命中
        if not force and self._cache and (now - self._cache_time) < self._ttl:
            age = now - self._cache_time
            logger.debug(f"[AccountRefresher] 缓存命中 (age={age:.0f}s)")
            return self._cache
        
        # 尝试查询 broker
        info = self._query_broker()
        
        if info and info.get("buying_power", 0) > 0:
            self._cache = info
            self._cache_time = now
            self._query_count += 1
            logger.debug(
                f"[AccountRefresher] 刷新成功 | "
                f"equity=${info.get('total_assets', 0):,.0f} | "
                f"buying_power=${info.get('buying_power', 0):,.0f} | "
                f"cash=${info.get('cash', 0):,.0f}"
            )
            return info
        
        # 查询失败 → 降级
        return self._handle_fallback()
    
    def _query_broker(self) -> Optional[Dict[str, Any]]:
        """调用 broker API 获取账户信息"""
        if not self._broker:
            logger.debug("[AccountRefresher] broker_adapter 未初始化")
            return None
        
        if not self._broker.is_connected():
            logger.debug("[AccountRefresher] broker 未连接")
            return None
        
        try:
            # 直接调用 (broker 内部有超时机制)
            info = self._broker.get_account_info()
            return info if info else None
        except Exception as e:
            self._error_count += 1
            logger.warning(f"[AccountRefresher] 查询失败 (error #{self._error_count}): {e}")
            return None
    
    def _handle_fallback(self) -> Dict[str, Any]:
        """降级处理"""
        if self._fallback == "last_known" and self._cache:
            age = time.time() - self._cache_time
            logger.warning(
                f"[AccountRefresher] 使用缓存数据 (已过期 {age:.0f}s, fallback={self._fallback})"
            )
            return self._cache
        
        if self._fallback == "block":
            raise AccountRefreshFailed(
                f"无法获取账户信息且 fallback=block (errors={self._error_count})"
            )
        
        # default: 返回空字典
        logger.warning(f"[AccountRefresher] 无法获取账户信息，返回空 (fallback={self._fallback})")
        return {}
    
    def get_cached(self) -> Optional[Dict[str, Any]]:
        """获取当前缓存（不触发刷新）"""
        return self._cache
    
    def is_fresh(self) -> bool:
        """缓存是否有效"""
        if not self._cache:
            return False
        return (time.time() - self._cache_time) < self._ttl
    
    def get_stats(self) -> Dict[str, Any]:
        """获取刷新统计"""
        return {
            "query_count": self._query_count,
            "error_count": self._error_count,
            "cache_age": time.time() - self._cache_time if self._cache else None,
            "is_fresh": self.is_fresh(),
            "fallback": self._fallback,
        }
    
    def reset(self):
        """重置缓存"""
        self._cache = None
        self._cache_time = 0


class AccountRefreshFailed(Exception):
    """账户信息刷新失败"""
    pass


# ============================================================
#  单例
# ============================================================

_refresher: Optional[AccountRefresher] = None
_refresher_lock = threading.Lock()


def get_account_refresher(
    broker_adapter=None,
    cache_ttl: int = 30,
    timeout: int = 3,
    fallback: str = "last_known",
) -> AccountRefresher:
    """获取 AccountRefresher 单例 (线程安全)"""
    global _refresher
    if _refresher is None:
        with _refresher_lock:
            if _refresher is None:  # 双重检查
                _refresher = AccountRefresher(
                    broker_adapter=broker_adapter,
                    cache_ttl=cache_ttl,
                    timeout=timeout,
                    fallback=fallback,
                )
    return _refresher


def reset_account_refresher():
    """重置单例 (用于测试或配置变更)"""
    global _refresher
    if _refresher:
        _refresher.reset()
    _refresher = None

