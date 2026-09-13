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
- TrendFirst / ATRStopEngine / playbook 管线均从 ATRProvider 取值

使用方式
--------
    from app.core.di_container import get_di_container
    atr_provider = get_di_container().atr_provider()
    atr = atr_provider.get_atr("US.AAPL")
    atr_pct = atr_provider.get_atr_pct("US.AAPL")
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ftseek.atr_provider")

# ── 选股口径 ATR (2026-09-12, DEB-20260910 / proud-slope-falcon P1.2) ──────────
# 与止损口径严格分离: get_atr()/get_atr_pct() 保持原样 (可能含 ExitMonitor 的
# VIX 缩放 + 三层底限, 或 Router 的分钟级 ATR), 只服务退出/止损。
# 选股初筛需要"原始日线 ATR": 已完成日 K 的 14 周期 TR rolling-mean,
# 不缩放、不加底限、不用分钟 ATR 兜底; 任何口径不明/缺数一律 UNKNOWN, 不编造。
SCREENING_ATR_BASIS = "daily_tr14_rolling_mean"
SCREENING_ATR_SOURCE = "router.get_daily_bars"
# Futu 路径为 QFQ, TwelveData fallback 未声明复权 → 口径不由来源保证,
# 改由数据自检 (拆股断点检测) 兜住; 声明值供审计, 不当作已核验事实。
SCREENING_ATR_ADJUSTMENT = "split_guarded_source_undeclared"
SCREENING_MIN_BARS = 15          # 15 根已完成日线 → 14 个 TR
SCREENING_TR_WINDOW = 14
SCREENING_FETCH_BARS = 40        # 多取以容纳去重/拆股截断后仍够 15 根
SCREENING_SPLIT_RATIO = 1.6      # 相邻收盘比 >1.6 或 <1/1.6 视为拆股/口径断点
SCREENING_CACHE_TTL = 3600       # PASS 结果按 (symbol, 完成交易日, 来源, 口径) 缓存
SCREENING_CACHE_MAX = 1024


def screening_asof_date(asof: Any) -> str:
    """把观测时点归一为 ET 交易日字符串 (只用严格早于该日的已完成日线)。

    naive datetime 直接拒绝: app.utils.timezone.to_et() 把 naive 当北京时间,
    静默转换会移半天 → 可能把当日未完成 bar 当成已完成日线。
    """
    from app.utils.timezone import now_et

    if asof is None:
        return now_et().date().isoformat()
    if isinstance(asof, datetime):
        if asof.tzinfo is None:
            raise ValueError("screening asof datetime must be timezone-aware")
        return asof.astimezone(now_et().tzinfo).date().isoformat()
    if isinstance(asof, date):
        return asof.isoformat()
    if isinstance(asof, str):
        text = asof.strip()[:10]
        date.fromisoformat(text)   # 非法字符串直接抛, 不静默兜底
        return text
    raise TypeError(f"unsupported screening asof type: {type(asof).__name__}")


def compute_screening_atr(bars: Any, asof: Any, *, symbol: str = "",
                          source: str = SCREENING_ATR_SOURCE) -> Dict[str, Any]:
    """纯函数: 已完成日线 → 原始筛选 ATR (14 周期 TR rolling-mean), 零 IO。

    与 position_exit_monitor._calc_atr_from_kline 的 TR 数学同源
    (tr = max(h-l, |h-prev_c|, |l-prev_c|), rolling(14).mean() 取末值),
    但**不含** VIX 缩放 / 三层底限 / 中位 TR 兜底 —— 那些是止损口径。

    固定口径:
      1. time|date 归一为 "YYYY-MM-DD", 同日重复取最后一条, 按日期升序;
      2. 交易日截断: 只保留严格早于 asof(ET) 的 bar (当日未完成 bar 一律排除);
      3. 拆股/复权断点: 自末根向前, 相邻收盘比 >1.6 或 <1/1.6 即截断, 不跨断点算 TR;
      4. 连续可用段不足 15 根 (14 个 TR) → UNKNOWN, 不外推、不用分钟 ATR 兜底;
      5. 任何非有限/非正 OHLC 或 high<low → UNKNOWN。

    Args:
        bars: [{time|date, open, high, low, close}, ...] (任意顺序, 允许重复日期)
        asof: 带时区 datetime / date / "YYYY-MM-DD"
        symbol / source: 只用于审计回显 (source 让调用方声明自己的日线来源)
    """
    asof_date = screening_asof_date(asof)

    def _result(status, reason, *, atr=None, bars_asof=None, bars_used=0, ref_close=None):
        return {
            "symbol": symbol, "atr": atr, "status": status, "reason": reason,
            "basis": SCREENING_ATR_BASIS, "source": source,
            "adjustment": SCREENING_ATR_ADJUSTMENT,
            "bars_asof": bars_asof, "bars_used": bars_used,
            "asof_date": asof_date, "cached": False,
            # 最后一根**已完成**日线收盘 — 只在调用方没有有效参考价时作分母兜底,
            # 不是"当前价"; screening_atr_ratio 的分母口径由调用方显式声明。
            "ref_close": ref_close,
        }

    parsed: Dict[str, Dict[str, Any]] = {}
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        raw_date = str(bar.get("time") or bar.get("date") or "")[:10]
        if len(raw_date) != 10 or (asof_date and raw_date >= asof_date):
            continue
        values: Dict[str, Any] = {"date": raw_date}
        for key in ("open", "high", "low", "close"):
            try:
                values[key] = float(bar.get(key))
            except (TypeError, ValueError):
                values[key] = float("nan")
        parsed[raw_date] = values          # 同日重复取最后一条 (去重口径固定)
    if not parsed:
        return _result("UNKNOWN", "no_completed_daily_bars")
    series = [parsed[key] for key in sorted(parsed)]

    tail: list = []
    stop_reason = "insufficient_bars"
    later_close: Optional[float] = None
    for bar in reversed(series):
        high, low, close = bar["high"], bar["low"], bar["close"]
        if not (math.isfinite(high) and math.isfinite(low) and math.isfinite(close)
                and high > 0 and low > 0 and close > 0 and high >= low):
            stop_reason = "invalid_bar"
            break
        if later_close is not None:
            ratio = close / later_close
            if ratio > SCREENING_SPLIT_RATIO or ratio < 1.0 / SCREENING_SPLIT_RATIO:
                stop_reason = "split_discontinuity"   # 复权口径断点, 不跨断点算 TR
                break
        tail.append(bar)
        later_close = close
    tail.reverse()
    if len(tail) < SCREENING_MIN_BARS:
        return _result("UNKNOWN", stop_reason,
                       bars_asof=tail[-1]["date"] if tail else None, bars_used=len(tail))

    window = tail[-SCREENING_MIN_BARS:]
    true_ranges = []
    for idx in range(1, len(window)):
        prev_close = window[idx - 1]["close"]
        high, low = window[idx]["high"], window[idx]["low"]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else float("nan")
    if len(true_ranges) != SCREENING_TR_WINDOW or not math.isfinite(atr) or atr <= 0:
        return _result("UNKNOWN", "non_positive_atr", bars_asof=window[-1]["date"],
                       bars_used=len(window))
    return _result("PASS", "ok", atr=atr, bars_asof=window[-1]["date"],
                   bars_used=len(window), ref_close=window[-1]["close"])


class ATRProvider:
    """ATR 统一数据提供器（C2: 2026-06-03 线程安全修复）"""

    def __init__(self, market_data_router: Any, cache_ttl_seconds: int = 60):
        self._router = market_data_router
        self._cache_ttl = max(1, int(cache_ttl_seconds))
        # symbol -> (atr, timestamp)
        self._cache: Dict[str, Tuple[float, float]] = {}
        # 选股口径缓存: (symbol, asof_date, source, basis) -> (result, timestamp)
        self._screening_cache: Dict[Tuple[str, str, str, str], Tuple[Dict[str, Any], float]] = {}
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
    #  选股口径 ATR (原始日线; 与止损口径物理分离)
    # ------------------------------------------------------------------
    def get_screening_atr(self, symbol: str, asof: Any = None, bars: Any = None,
                          fetch_count: int = SCREENING_FETCH_BARS) -> Dict[str, Any]:
        """选股初筛专用: 原始日线 ATR (14 周期 TR rolling-mean), 不缩放/不加底限。

        与 get_atr()/get_atr_pct() 的区别 (P1.2 口径分离):
          - 不读 ExitMonitor 缓存 (那条链含 VIX 缩放 + 三层底限, 是止损口径);
          - 不回落 Router 分钟级 context["atr"];
          - 不做过期缓存宽限 (宁 UNKNOWN 不用旧值冒充当日口径);
          - 只用严格早于 asof 的 ET 已完成日线, 当日未完成 bar 一律排除。

        数学与口径全部在纯函数 compute_screening_atr() 内 (仓外 DA 亦复用同一函数,
        只换日线来源), 本方法只负责取数 + 缓存。

        任何缺数/口径不明/非有限值 → status="UNKNOWN" 且 atr=None, 绝不编造 ATR。
        调用方语义: UNKNOWN ≠ 可交易, 也 ≠ 不可交易; 研究阶段可继续,
        执行阶段仍受原有低 ATR 风险门约束。

        Args:
            symbol: 标的代码 (如 "US.NVDA")
            asof: 带时区 datetime / date / "YYYY-MM-DD"; naive datetime 直接拒绝
            bars: 可注入的日线列表 (测试用), 元素需含 time|date + open/high/low/close
            fetch_count: 向 router 请求的日线根数 (需容纳去重与拆股截断)
        """
        symbol = str(symbol or "")
        if not symbol:
            return compute_screening_atr([], asof, symbol=symbol)
        asof_date = screening_asof_date(asof)
        cache_key = (symbol, asof_date, SCREENING_ATR_SOURCE, SCREENING_ATR_BASIS)
        now_ts = time.time()
        with self._lock:
            hit = self._screening_cache.get(cache_key)
            if hit and now_ts - hit[1] < SCREENING_CACHE_TTL:
                return {**hit[0], "cached": True}

        if bars is None:
            if self._router is None:
                return {**compute_screening_atr(None, asof_date, symbol=symbol),
                        "reason": "no_router"}
            try:
                bars = self._router.get_daily_bars(symbol, count=int(fetch_count))
            except Exception as exc:
                logger.warning(f"[ATRProvider·screening] {symbol} 日线获取异常: {exc}",
                               exc_info=True)
                return {**compute_screening_atr(None, asof_date, symbol=symbol),
                        "reason": f"daily_bars_error:{type(exc).__name__}"}

        result = compute_screening_atr(bars, asof_date, symbol=symbol)
        if result.get("status") == "PASS":
            with self._lock:
                if len(self._screening_cache) >= SCREENING_CACHE_MAX:
                    self._screening_cache.clear()   # 键含观测日, 跨日自然失效; 有界即可
                self._screening_cache[cache_key] = (result, now_ts)
        return result

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
            with self._lock:
                for key in [k for k in self._screening_cache if k[0] == symbol]:
                    self._screening_cache.pop(key, None)
        else:
            self._cache.clear()
            with self._lock:
                self._screening_cache.clear()

    def cache_stats(self) -> Dict[str, Any]:
        """诊断用：返回当前缓存条目数与样本"""
        now = time.time()
        fresh = sum(1 for _, ts in self._cache.values() if now - ts < self._cache_ttl)
        return {
            "size": len(self._cache),
            "fresh": fresh,
            "ttl_seconds": self._cache_ttl,
            "screening_size": len(self._screening_cache),
        }
