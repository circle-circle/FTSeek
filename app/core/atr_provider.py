"""
ATR 单一真相源 (Single Source of Truth for ATR)
=================================================

设计目标
--------
FTSeek-US 系统中 ATR 在多处独立计算（MarketDataRouter / position_exit_monitor /
trend_first / atr_stop_engine ...），可能导致同一标的同一时刻 ATR 值不一致，
影响止损/止盈精度。

ATRProvider 作为统一获取入口：
- 底层数据由 MarketDataRouter 提供（get_full_market_context）
- ATRProvider 持有内存缓存（默认 60s TTL）避免频繁重算
- 上游异常时返回 5×TTL 内的过期缓存作为宽限期 fallback
- TrendFirst / ATRStopEngine / ExitRuleDispatcher 均从 ATRProvider 取值

使用方式
--------
    from app.core.di_container import get_di_container
    atr_provider = get_di_container().atr_provider()
    atr = atr_provider.get_atr("US.AAPL")
    atr_pct = atr_provider.get_atr_pct("US.AAPL")
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ftseek.atr_provider")


class ATRProvider:
    """ATR 统一数据提供器（C2: 2026-06-03 线程安全修复）"""

    def __init__(self, market_data_router: Any, cache_ttl_seconds: int = 60):
        self._router = market_data_router
        self._cache_ttl = max(1, int(cache_ttl_seconds))
        # symbol -> (atr, timestamp)
        self._cache: Dict[str, Tuple[float, float]] = {}
        # C2: 线程安全锁
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    #  ATR 获取
    # ------------------------------------------------------------------
    def get_atr(self, symbol: str, force_refresh: bool = False) -> Optional[float]:
        """获取标的 ATR 值，优先走缓存

        优先级 (2026-06-14 ATR 统一修复):
          1. ExitMonitor 日线 ATR 缓存 (含 VIX 缩放 + 底限保护) — 与止损逻辑同源
          2. 自身内存缓存 (60s TTL)
          3. MarketDataRouter (1-min K线, 量级可能偏差)
          4. 过期缓存 fallback (5×TTL 宽限期)

        Args:
            symbol: 标的代码 (e.g. 'US.AAPL')
            force_refresh: True 时跳过缓存强制从 Router 重新拉取

        Returns:
            ATR 数值 (float)；获取失败且无可用过期缓存时返回 None
        """
        if not symbol:
            return None

        now = time.time()

        # ── P0 (2026-06-14): 优先读 ExitMonitor 日线 ATR ──
        # ExitMonitor._get_atr() 使用 K_DAY + VIX 缩放 + 三层底限,
        # 与止损决策同源。避免 1-min ATR 量级偏差 (10-50×) 导致止损错位。
        if not force_refresh:
            try:
                from app.risk.position_exit_monitor import get_cached_atr
                _exit_atr = get_cached_atr(symbol)
                if _exit_atr > 0:
                    with self._lock:
                        self._cache[symbol] = (_exit_atr, now)
                    return _exit_atr
            except Exception:
                pass  # ExitMonitor 未初始化或导入失败, fallback 到原逻辑

        with self._lock:
            # 1) 缓存有效命中
            if not force_refresh and symbol in self._cache:
                cached_atr, cached_time = self._cache[symbol]
                if now - cached_time < self._cache_ttl:
                    return cached_atr

        # 2) 从 MarketDataRouter 拉取（锁外执行，避免长时间持锁）
        try:
            context = self._router.get_full_market_context(symbol) if self._router else None
            if context is None:
                logger.warning(f"[ATRProvider] {symbol} 无法获取市场数据 (context=None)")
                return self._get_cached_fallback(symbol)

            atr_raw = context.get("atr") if isinstance(context, dict) else getattr(context, "atr", None)
            if atr_raw is not None and float(atr_raw) > 0:
                atr = float(atr_raw)
                with self._lock:
                    self._cache[symbol] = (atr, now)
                return atr

            logger.warning(f"[ATRProvider] {symbol} ATR 无效: {atr_raw}")
            return self._get_cached_fallback(symbol)

        except Exception as e:
            logger.error(f"[ATRProvider] {symbol} 获取 ATR 异常: {e}")
            return self._get_cached_fallback(symbol)

    def _get_cached_fallback(self, symbol: str) -> Optional[float]:
        """C2: 缓存有过期数据时返回（5×TTL 宽限期），否则返回 last_good 或 None"""
        with self._lock:
            cached = self._cache.get(symbol)
            if not cached:
                return None
            cached_atr, cached_time = cached
            if time.time() - cached_time < self._cache_ttl * 5:
                logger.info(f"[ATRProvider] {symbol} 使用过期缓存 ATR={cached_atr:.4f}")
                return cached_atr
            return None

    # ------------------------------------------------------------------
    #  ATR 百分比
    # ------------------------------------------------------------------
    def get_atr_pct(self, symbol: str, current_price: Optional[float] = None) -> Optional[float]:
        """获取 ATR 占价格的百分比 (atr / price)"""
        atr = self.get_atr(symbol)
        if atr is None:
            return None

        if current_price is None or current_price <= 0:
            try:
                context = self._router.get_full_market_context(symbol) if self._router else None
                if context is None:
                    return None
                price_raw = context.get("price") if isinstance(context, dict) else getattr(context, "price", None)
                current_price = float(price_raw) if price_raw else None
            except Exception as e:
                logger.debug(f"[ATRProvider] {symbol} 获取价格异常: {e}")
                return None

        if current_price and current_price > 0:
            return atr / current_price
        return None

    # ------------------------------------------------------------------
    #  缓存管理
    # ------------------------------------------------------------------
    def invalidate(self, symbol: Optional[str] = None) -> None:
        """清空缓存

        Args:
            symbol: 指定标的清单条；None 表示全部清空
        """
        if symbol:
            self._cache.pop(symbol, None)
        else:
            self._cache.clear()

    def cache_stats(self) -> Dict[str, Any]:
        """诊断用：返回当前缓存条目数与样本"""
        now = time.time()
        fresh = sum(1 for _, ts in self._cache.values() if now - ts < self._cache_ttl)
        return {
            "size": len(self._cache),
            "fresh": fresh,
            "ttl_seconds": self._cache_ttl,
        }
