"""
双市场节假日日历 (Trading Holiday Calendar)

涵盖港股和美股的法定休市日，用于防止在非交易日触发盘前计划/竞价单。

P2 实现 (2026-04-19): 覆盖 2026-2027 年主要节假日。
"""

from datetime import date, timedelta
from typing import Optional, Set
import logging

logger = logging.getLogger("ftseek.holiday_calendar")

# ── 港股节假日 (2026-2027) ──────────────────────────────────
HK_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # 元旦
    date(2026, 2, 17),   # 春节 (年初一)
    date(2026, 2, 18),   # 春节 (年初二)
    date(2026, 2, 19),   # 春节 (年初三)
    date(2026, 4, 3),    # 清明节
    date(2026, 4, 6),    # 复活节星期一
    date(2026, 4, 7),    # 佛诞
    date(2026, 5, 1),    # 劳动节
    date(2026, 5, 25),   # 端午节
    date(2026, 7, 1),    # 香港特区成立纪念日
    date(2026, 10, 1),   # 国庆节
    date(2026, 10, 2),   # 国庆节翌日
    date(2026, 10, 19),  # 重阳节
    date(2026, 12, 25),  # 圣诞节
    date(2026, 12, 26),  # 节礼日
    date(2026, 12, 28),  # 圣诞节补假
}

HK_HOLIDAYS_2027 = {
    date(2027, 1, 1),    # 元旦
    date(2027, 2, 8),    # 春节 (年初一)
    date(2027, 2, 9),    # 春节 (年初二)
    date(2027, 2, 10),   # 春节 (年初三)
    date(2027, 3, 25),   # 耶稣受难节
    date(2027, 3, 26),   # 复活节星期一
    date(2027, 3, 27),   # 清明节补假
    date(2027, 5, 1),    # 劳动节
    date(2027, 5, 3),    # 佛诞补假
    date(2027, 6, 14),   # 端午节
    date(2027, 7, 1),    # 香港特区成立纪念日
    date(2027, 10, 1),   # 国庆节
    date(2027, 10, 11),  # 中秋节补假
    date(2027, 10, 12),  # 重阳节
    date(2027, 12, 27),  # 圣诞节补假
}

# ── 美股节假日 (2026-2027) ────────────────────────────────
US_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # 元旦
    date(2026, 1, 19),   # 马丁路德金日
    date(2026, 2, 16),   # 总统日
    date(2026, 4, 3),    # 耶稣受难节
    date(2026, 5, 25),   # 阵亡将士纪念日
    date(2026, 6, 19),   # 六月节 (Juneteenth)
    date(2026, 7, 3),    # 独立日 (提前休市)
    date(2026, 9, 7),    # 劳动节
    date(2026, 11, 26),  # 感恩节
    date(2026, 12, 24),  # 圣诞前夕 (提前休市)
    date(2026, 12, 25),  # 圣诞节
}

US_HOLIDAYS_2027 = {
    date(2027, 1, 1),    # 元旦
    date(2027, 1, 18),   # 马丁路德金日
    date(2027, 2, 15),   # 总统日
    date(2027, 4, 2),    # 耶稣受难节
    date(2027, 5, 31),   # 阵亡将士纪念日
    date(2027, 6, 18),   # 六月节 (Juneteenth)
    date(2027, 7, 5),    # 独立日补假
    date(2027, 9, 6),    # 劳动节
    date(2027, 11, 25),  # 感恩节
    date(2027, 12, 24),  # 圣诞前夕 (提前休市)
    date(2027, 12, 27),  # 圣诞节补假
}


class TradingHolidayCalendar:
    """交易节假日日历"""

    _instance = None
    _hk_holidays: Set[date] = set()
    _us_holidays: Set[date] = set()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _ensure_loaded(self):
        """确保节假日数据已加载"""
        if self._initialized:
            return
        self._hk_holidays = set()
        self._us_holidays = set()

        for year_holidays in [HK_HOLIDAYS_2026, HK_HOLIDAYS_2027]:
            self._hk_holidays.update(year_holidays)
        for year_holidays in [US_HOLIDAYS_2026, US_HOLIDAYS_2027]:
            self._us_holidays.update(year_holidays)

        self._initialized = True
        logger.info(
            f"📅 节假日日历已加载: HK={len(self._hk_holidays)} 天, "
            f"US={len(self._us_holidays)} 天"
        )

    def is_hk_holiday(self, d: date) -> bool:
        """判断是否为港股节假日"""
        self._ensure_loaded()
        return d in self._hk_holidays

    def is_us_holiday(self, d: date) -> bool:
        """判断是否为美股节假日"""
        self._ensure_loaded()
        return d in self._us_holidays

    def is_trading_day(self, market: str, d: date = None) -> bool:
        """
        判断是否为交易日 (非周末 + 非节假日)

        Args:
            market: "HK" 或 "US"
            d: 日期 (默认今天)
        """
        if d is None:
            d = date.today()
        self._ensure_loaded()

        # 周末判断
        if d.weekday() >= 5:  # 5=周六, 6=周日
            return False

        # 节假日判断
        if market == "HK":
            return d not in self._hk_holidays
        elif market == "US":
            return d not in self._us_holidays

        return True

    def next_trading_day(self, market: str, from_date: date = None, max_days: int = 14) -> Optional[date]:
        """获取下一个交易日"""
        if from_date is None:
            from_date = date.today()
        d = from_date + timedelta(days=1)
        for _ in range(max_days):
            if self.is_trading_day(market, d):
                return d
            d += timedelta(days=1)
        return None

    def get_summary(self) -> dict:
        """获取日历摘要"""
        self._ensure_loaded()
        return {
            "hk_holidays_count": len(self._hk_holidays),
            "us_holidays_count": len(self._us_holidays),
            "next_hk_holiday": self.next_trading_day("HK"),
            "next_us_holiday": self.next_trading_day("US"),
        }


# 全局单例
def get_holiday_calendar() -> TradingHolidayCalendar:
    """获取节假日日历单例"""
    return TradingHolidayCalendar()
