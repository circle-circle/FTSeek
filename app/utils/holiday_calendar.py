"""
统一交易日历 (Trading Calendar SSOT)

架构:
  Futu API (request_trading_days) → 权威数据源, 批量缓存
  硬编码节假日 → 离线降级, API 不可用时兜底
  get_market_state → 实时市场状态 (盘中/盘前/盘后/休市)

消费方统一入口:
  cal = get_holiday_calendar()
  cal.is_trading_day("US", date.today())
  cal.is_trading_day("HK", date.today())
  cal.next_trading_day("US")
  cal.is_us_holiday(d)  # 兼容旧接口
  cal.is_hk_holiday(d)   # 兼容旧接口
  cal.is_market_open_realtime("US")  # Futu get_market_state

缓存策略:
  - 首次调用批量查询 [today-30d, today+60d] 共 ~90 天交易日
  - 内存缓存, 进程生命周期内有效
  - 缓存 miss (日期超出范围) 自动扩展查询
  - API 失败 → 降级到硬编码, 不影响交易安全

线程安全: threading.Lock 保护缓存读写
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

logger = logging.getLogger("ftseek.trading_calendar")

# ═══════════════════════════════════════════════════════════════════
#  Futu 连接参数 (SSOT: 环境变量 → 默认值)
# ═══════════════════════════════════════════════════════════════════

_FUTU_HOST = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
_FUTU_PORT = int(os.environ.get("FUTU_OPEND_PORT", "11111"))

# 市场名映射: 用户传 "US"/"HK" → Futu TradeDateMarket 枚举
_MARKET_MAP = {
    "US": "US",
    "HK": "HK",
    "CN": "CN",
}


# ═══════════════════════════════════════════════════════════════════
#  硬编码节假日 (2026-2027) — 离线降级用
# ═══════════════════════════════════════════════════════════════════

HK_HOLIDAYS: Set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    date(2026, 4, 3), date(2026, 4, 6), date(2026, 4, 7), date(2026, 5, 1),
    date(2026, 5, 25), date(2026, 7, 1), date(2026, 10, 1), date(2026, 10, 2),
    date(2026, 10, 19), date(2026, 12, 25), date(2026, 12, 26), date(2026, 12, 28),
    # 2027
    date(2027, 1, 1), date(2027, 2, 8), date(2027, 2, 9), date(2027, 2, 10),
    date(2027, 3, 25), date(2027, 3, 26), date(2027, 3, 27), date(2027, 5, 1),
    date(2027, 5, 3), date(2027, 6, 14), date(2027, 7, 1), date(2027, 10, 1),
    date(2027, 10, 11), date(2027, 10, 12), date(2027, 12, 27),
}

US_HOLIDAYS: Set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 24), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 4, 2),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24), date(2027, 12, 27),
}

_HOLIDAYS_BY_MARKET: Dict[str, Set[date]] = {
    "HK": HK_HOLIDAYS,
    "US": US_HOLIDAYS,
}

# 2026-07-25 (隐患扫描修复): NYSE 提前收盘日 (13:00 ET) — 此前全仓硬编码 16:00,
#   半日市 PRE_CLOSE_EXIT/EOD 不触发 → 日内仓被迫隔夜。
#   口径: 感恩节次日恒半日; 平安夜仅当天为交易日时半日 (2027 圣诞观察在 12-24 周五整休, 无半日)。
#   注: 2026-12-24 在上方 US_HOLIDAYS 被保守标为整休 (Futu API 为主真源会覆盖);
#   若实际开市则本表兼容按 13:00 收。新增年份时同步维护本表。
US_EARLY_CLOSE_DATES: Set[date] = {
    date(2026, 11, 27),   # 感恩节次日 (周五)
    date(2026, 12, 24),   # 平安夜 (周四, 若开市)
    date(2027, 11, 26),   # 感恩节次日 (周五)
}

_US_EARLY_CLOSE_MINUTES = 13 * 60   # 13:00 ET
_US_REGULAR_CLOSE_MINUTES = 16 * 60  # 16:00 ET


def get_us_close_minutes_et(d: Optional[date] = None) -> int:
    """当日美股收盘时刻 (ET 分钟数): 提前收盘日 780 (13:00), 否则 960 (16:00).

    d 缺省 = 今日 (ET)。供退出链 minutes_to_close 计算用, 调用方应 try/except 兜底 960。
    """
    if d is None:
        try:
            from app.utils.timezone import now_et
            d = now_et().date()
        except Exception:
            d = date.today()
    return _US_EARLY_CLOSE_MINUTES if d in US_EARLY_CLOSE_DATES else _US_REGULAR_CLOSE_MINUTES


# ═══════════════════════════════════════════════════════════════════
#  Futu API 适配层
# ═══════════════════════════════════════════════════════════════════

def _futu_request_trading_days(market: str, start: str, end: str) -> Optional[List[str]]:
    """
    调用 Futu request_trading_days API.

    Returns:
        交易日列表 ["2026-05-26", ...] 或 None (API 失败)
    """
    try:
        from futu import OpenQuoteContext, TradeDateMarket, RET_OK
        futu_market = getattr(TradeDateMarket, market, None)
        if futu_market is None:
            logger.debug(f"[TradingCalendar] 未知市场: {market}")
            return None

        ctx = OpenQuoteContext(host=_FUTU_HOST, port=_FUTU_PORT)
        try:
            ret, data = ctx.request_trading_days(
                market=futu_market, start=start, end=end
            )
        finally:
            ctx.close()

        if ret == RET_OK:
            return [d["time"] for d in data]
        else:
            logger.debug(f"[TradingCalendar] Futu API 错误: {data}")
            return None
    except Exception as e:
        logger.debug(f"[TradingCalendar] Futu API 调用失败: {e}")
        return None


def _futu_get_market_state(code: str) -> Optional[str]:
    """
    调用 Futu get_market_state API 获取实时市场状态.

    Returns:
        市场状态字符串 (如 "Morning", "Closed") 或 None
    """
    try:
        from futu import OpenQuoteContext, RET_OK
        ctx = OpenQuoteContext(host=_FUTU_HOST, port=_FUTU_PORT)
        try:
            ret, data = ctx.get_market_state([code])
        finally:
            ctx.close()

        if ret == RET_OK and not data.empty:
            return str(data.iloc[0].get("market_state", "N/A"))
        return None
    except Exception as e:
        logger.debug(f"[TradingCalendar] get_market_state 失败: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
#  主类: TradingHolidayCalendar (SSOT)
# ═══════════════════════════════════════════════════════════════════

class TradingHolidayCalendar:
    """
    统一交易日历 (单例).

    优先级: Futu API > 硬编码节假日 > 仅周末判断.
    """

    _instance: Optional[TradingHolidayCalendar] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()

        # 交易日缓存: {"US": {"2026-05-26", "2026-05-27", ...}, "HK": {...}}
        self._trading_days_cache: Dict[str, Set[str]] = {}

        # 缓存覆盖范围: {"US": ("2026-05-01", "2026-07-30"), ...}
        self._cache_range: Dict[str, tuple] = {}

        # Futu API 可用标记 (失败后短时间内不重试)
        self._api_available: bool = True
        self._api_disabled_until: float = 0.0

        logger.info(
            f"📅 交易日历初始化: 硬编码 HK={len(HK_HOLIDAYS)}天, "
            f"US={len(US_HOLIDAYS)}天 | Futu API={_FUTU_HOST}:{_FUTU_PORT}"
        )

    # ─── Futu API 缓存管理 ───────────────────────────────────────

    def _ensure_cache(self, market: str, d: date) -> bool:
        """
        确保指定日期在缓存中. 如不在, 扩展查询.

        Returns:
            True = 缓存命中 (可以判断), False = 缓存 miss 且 API 失败
        """
        date_str = d.isoformat()

        with self._lock:
            # 已在缓存中
            if market in self._trading_days_cache and date_str in self._trading_days_cache[market]:
                return True

            # 在缓存范围内但不是交易日 → 也是命中 (不在集合中 = 非交易日)
            if market in self._cache_range:
                lo, hi = self._cache_range[market]
                if lo <= date_str <= hi:
                    return True

        # 缓存 miss → 查询 Futu API
        return self._refresh_cache(market, d)

    def _refresh_cache(self, market: str, center: date) -> bool:
        """从 Futu API 批量查询交易日并缓存."""
        import time

        # API 冷却期 (失败后 60 秒内不重试)
        if time.time() < self._api_disabled_until:
            return False

        start = (center - timedelta(days=30)).isoformat()
        end = (center + timedelta(days=60)).isoformat()

        result = _futu_request_trading_days(market, start, end)

        if result is not None:
            with self._lock:
                trading_set = set(result)
                if market in self._trading_days_cache:
                    self._trading_days_cache[market].update(trading_set)
                else:
                    self._trading_days_cache[market] = trading_set

                # 更新缓存范围
                old_lo, old_hi = self._cache_range.get(market, ("9999-12-31", "0000-01-01"))
                self._cache_range[market] = (
                    min(old_lo, start),
                    max(old_hi, end),
                )

            self._api_available = True
            logger.info(
                f"[TradingCalendar] Futu 缓存刷新: {market} "
                f"[{start}~{end}] = {len(result)} 交易日"
            )
            return True
        else:
            # API 失败 → 进入冷却期
            self._api_disabled_until = time.time() + 60.0
            self._api_available = False
            logger.debug(f"[TradingCalendar] Futu API 失败, 降级到硬编码 (冷却 60s)")
            return False

    # ─── 核心公开 API ────────────────────────────────────────────

    def is_trading_day(self, market: str, d: date = None) -> bool:
        """
        判断是否为交易日 (非周末 + 非节假日).

        优先级:
          1. Futu API 缓存 (权威)
          2. 硬编码节假日 (离线降级)
          3. 仅周末判断 (最终兜底)

        Args:
            market: "HK" 或 "US"
            d: 日期 (默认今天, 调用方应传 ET 日期)
        """
        if d is None:
            # 2026-07-27 修复 (position_sync 同款漏网): 原 date.today()=CST — CST 周一凌晨
            #   =ET 周日, 无参调用方会把 ET 周日判成"交易日(周一)"。缺省统一 ET 口径。
            try:
                from app.utils.timezone import now_et
                d = now_et().date()
            except Exception:
                d = date.today()

        # 周末快速排除
        if d.weekday() >= 5:
            return False

        market = market.upper()

        # 1. Futu API 缓存 (含自动刷新)
        if self._ensure_cache(market, d):
            with self._lock:
                return d.isoformat() in self._trading_days_cache.get(market, set())

        # 2. 硬编码降级
        holidays = _HOLIDAYS_BY_MARKET.get(market, set())
        if d in holidays:
            return False

        # 3. 非周末 + 非已知假日 → 视为交易日
        return True

    def is_us_holiday(self, d: date) -> bool:
        """判断是否为美股节假日 (兼容旧接口)"""
        return d in US_HOLIDAYS

    def is_hk_holiday(self, d: date) -> bool:
        """判断是否为港股节假日 (兼容旧接口)"""
        return d in HK_HOLIDAYS

    def next_trading_day(self, market: str, from_date: Optional[date] = None, max_days: int = 14) -> Optional[date]:
        """获取下一个交易日"""
        if from_date is None:
            from_date = date.today()
        d = from_date + timedelta(days=1)
        for _ in range(max_days):
            if self.is_trading_day(market, d):
                return d
            d += timedelta(days=1)
        return None

    def prev_trading_day(self, market: str, from_date: Optional[date] = None, max_days: int = 14) -> Optional[date]:
        """获取上一个交易日"""
        if from_date is None:
            from_date = date.today()
        d = from_date - timedelta(days=1)
        for _ in range(max_days):
            if self.is_trading_day(market, d):
                return d
            d -= timedelta(days=1)
        return None

    def nth_trading_day_after(self, market: str, from_date: date, n: int, max_days: int = 30) -> Optional[date]:
        """RFC-061: 从 from_date 起第 n 个交易日后的日期 (from_date 本身不计入).

        用于 MAX_HOLD_T5 (5 交易日后强平), 自动跳过周末/假期 (如 Juneteenth).
        例: from_date=6/16(周一), n=5 → 6/17,18,19(假期跳过),22,23 → 返回 6/23.
        max_days 防死循环 (节假日数据异常时兜底).
        """
        if n <= 0 or from_date is None:
            return from_date
        d = from_date
        count = 0
        for _ in range(max_days):
            d += timedelta(days=1)
            if self.is_trading_day(market, d):
                count += 1
                if count >= n:
                    return d
        return None

    def trading_days_in_range(self, market: str, start: date, end: date) -> List[date]:
        """获取日期范围内的所有交易日"""
        days = []
        d = start
        while d <= end:
            if self.is_trading_day(market, d):
                days.append(d)
            d += timedelta(days=1)
        return days

    def is_market_open_realtime(self, market: str) -> bool:
        """
        通过 Futu get_market_state 获取实时市场状态.

        Returns:
            True = 市场正在交易中 (Morning/Afternoon)
            False = 休市/盘前/盘后/未知
        """
        # 选一个代表性标的查询市场状态
        code_map = {"US": "US.SPY", "HK": "HK.00700"}
        code = code_map.get(market.upper())
        if not code:
            return False

        state = _futu_get_market_state(code)
        if state is None:
            return False

        # Futu MarketState 枚举: Morning, Afternoon = 盘中
        open_states = {"Morning", "Afternoon", "QotMarketState_Morning", "QotMarketState_Afternoon"}
        return state in open_states

    def get_market_state_realtime(self, market: str) -> str:
        """获取实时市场状态字符串"""
        code_map = {"US": "US.SPY", "HK": "HK.00700"}
        code = code_map.get(market.upper())
        if not code:
            return "Unknown"
        return _futu_get_market_state(code) or "Unknown"

    # ─── 诊断 / 调试 ─────────────────────────────────────────────

    def get_cache_info(self) -> Dict:
        """获取缓存状态 (诊断用)"""
        with self._lock:
            return {
                "api_available": self._api_available,
                "markets_cached": list(self._trading_days_cache.keys()),
                "trading_days_count": {
                    m: len(days) for m, days in self._trading_days_cache.items()
                },
                "cache_ranges": dict(self._cache_range),
            }

    def get_summary(self) -> Dict:
        """获取日历摘要 (兼容旧接口)"""
        return {
            "hk_holidays_count": len(HK_HOLIDAYS),
            "us_holidays_count": len(US_HOLIDAYS),
            "next_us_trading_day": self.next_trading_day("US"),
            "next_hk_trading_day": self.next_trading_day("HK"),
            "futu_api_available": self._api_available,
            **self.get_cache_info(),
        }

    def warm_cache(self, market: str = "US"):
        """预热缓存 (启动时调用, 可选)"""
        self._refresh_cache(market, date.today())


# ═══════════════════════════════════════════════════════════════════
#  全局单例
# ═══════════════════════════════════════════════════════════════════

def get_holiday_calendar() -> TradingHolidayCalendar:
    """获取交易日历单例"""
    return TradingHolidayCalendar()


def nth_trading_day_after(market: str, from_date: date, n: int, max_days: int = 30) -> Optional[date]:
    """RFC-061 便捷函数: 从 from_date 起第 n 个交易日后的日期 (模块级, 复用单例)."""
    return get_holiday_calendar().nth_trading_day_after(market, from_date, n, max_days)
