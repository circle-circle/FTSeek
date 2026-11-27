"""RFC-171 §9 契约: get_market_hours_us 假日/半日感知 (同源缺陷批 §6/§7/§8 延续).

根因: 原实现只跳周末 + 固定 16:00 收盘 → 假日被当 6.5h 交易日、半日 13:00-16:00 多计 3h。
holding_hours (models.py 复用本函数) 喂报表记录/告警/历史样本 (L1 域, 不驱动 live 出场订单)。
修复: 复用 holiday_calendar.is_us_holiday (跳整休) + get_us_close_minutes_et (半日 13:00)。

离线夹具: 把 _cached_trading_day 钉成 None ⇒ is_us_holiday 走硬编码 US_HOLIDAYS, 不触网。
变异红绿: 把实现改回「只跳周末 + 固定 16:00」⇒ 假日/半日断言转红 (见各条 docstring)。
"""
from datetime import datetime

import pytest
import pytz

from app.risk.position_exit_monitor import get_market_hours_us

ET = pytz.timezone("US/Eastern")


def _et(y, mo, d, h, mi=0):
    return ET.localize(datetime(y, mo, d, h, mi))


@pytest.fixture(autouse=True)
def _offline_calendar(monkeypatch):
    from app.utils import holiday_calendar as hc
    monkeypatch.setattr(hc.TradingHolidayCalendar, "_cached_trading_day",
                        lambda self, market, d: None)


# ── SSOT 锚点: 日历本身对 11-26/11-27 的判定 (修复依赖这两个事实) ──
def test_calendar_ssot_anchors():
    from app.utils.holiday_calendar import get_holiday_calendar, get_us_close_minutes_et
    from datetime import date
    assert get_holiday_calendar().is_us_holiday(date(2026, 11, 26)) is True   # 感恩节整休
    assert get_holiday_calendar().is_us_holiday(date(2026, 11, 27)) is False  # 半日市仍交易
    assert get_us_close_minutes_et(date(2026, 11, 27)) == 780                 # 13:00 ET
    assert get_us_close_minutes_et(date(2026, 11, 24)) == 960                 # 16:00 ET


# ── 正常日不变 (回归守卫: 修复不得改动常规路径) ──
def test_regular_day_full_session_unchanged():
    # 11-24 周二常规日 09:30-16:00 = 6.5h
    assert get_market_hours_us(_et(2026, 11, 24, 9, 30), _et(2026, 11, 24, 16, 0)) == pytest.approx(6.5)


def test_regular_day_partial_unchanged():
    assert get_market_hours_us(_et(2026, 11, 24, 11, 0), _et(2026, 11, 24, 12, 0)) == pytest.approx(1.0)


def test_weekend_still_skipped():
    # 周六 11-21 10:00 → 周一 11-23 10:00: 仅周一 09:30-10:00 = 0.5h
    assert get_market_hours_us(_et(2026, 11, 21, 10, 0), _et(2026, 11, 23, 10, 0)) == pytest.approx(0.5)


# ── 假日整休不计 (变异: 改回只跳周末 ⇒ 11-26 被计 6.5h, 本条转红) ──
def test_holiday_thanksgiving_zero_hours():
    assert get_market_hours_us(_et(2026, 11, 26, 9, 30), _et(2026, 11, 26, 16, 0)) == pytest.approx(0.0)


# ── 半日市 13:00 收盘 (变异: 改回固定 16:00 ⇒ 得 6.5h, 本条转红) ──
def test_half_day_closes_at_1300_not_1600():
    # 11-27 半日: 09:30-16:00 请求只应计 09:30-13:00 = 3.5h
    assert get_market_hours_us(_et(2026, 11, 27, 9, 30), _et(2026, 11, 27, 16, 0)) == pytest.approx(3.5)


def test_half_day_partial_within_session():
    assert get_market_hours_us(_et(2026, 11, 27, 11, 0), _et(2026, 11, 27, 12, 0)) == pytest.approx(1.0)


def test_half_day_after_close_zero():
    # 半日 13:00 已收, 14:00-15:00 不应计任何交易小时
    assert get_market_hours_us(_et(2026, 11, 27, 14, 0), _et(2026, 11, 27, 15, 0)) == pytest.approx(0.0)


# ── 跨假日多日 span (变异: 改回只跳周末 ⇒ 8.0h, 本条转红) ──
def test_span_across_holiday():
    # 11-25 周二 15:00 → 11-27 周五(半日) 10:00
    # 真值: 11-25 15:00-16:00 (1h) + 11-26 感恩节 (0) + 11-27 09:30-10:00 (0.5h) = 1.5h
    assert get_market_hours_us(_et(2026, 11, 25, 15, 0), _et(2026, 11, 27, 10, 0)) == pytest.approx(1.5)


# ── 跨半日+周末多日 span (变异: 改回固定 16:00 ⇒ 5.5h, 本条转红) ──
def test_span_half_day_then_weekend():
    # 11-27 周五(半日) 11:00 → 11-30 周一 10:00
    # 真值: 11-27 11:00-13:00 (2h) + 周末 (0) + 11-30 09:30-10:00 (0.5h) = 2.5h
    assert get_market_hours_us(_et(2026, 11, 27, 11, 0), _et(2026, 11, 30, 10, 0)) == pytest.approx(2.5)


# ── 降级守卫: 日历异常 fail-open 保留原行为 (不阻断报表) ──
def test_holiday_query_exception_falls_back_to_counting(monkeypatch):
    from app.utils import holiday_calendar as hc

    def _boom(self, d):
        raise RuntimeError("calendar unavailable")
    monkeypatch.setattr(hc.TradingHolidayCalendar, "is_us_holiday", _boom)
    # 11-26 假日查询异常 → 降级按工作日计 6.5h (与原行为一致, 不抛)
    assert get_market_hours_us(_et(2026, 11, 26, 9, 30), _et(2026, 11, 26, 16, 0)) == pytest.approx(6.5)


def test_close_minutes_exception_falls_back_to_1600(monkeypatch):
    from app.utils import holiday_calendar as hc

    def _boom(d=None):
        raise RuntimeError("close minutes unavailable")
    monkeypatch.setattr(hc, "get_us_close_minutes_et", _boom)
    # 半日 11-27 收盘取值异常 → 兜底 960(16:00) ⇒ 6.5h (与 get_us_close_minutes_et docstring 约定一致)
    assert get_market_hours_us(_et(2026, 11, 27, 9, 30), _et(2026, 11, 27, 16, 0)) == pytest.approx(6.5)
