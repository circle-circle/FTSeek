"""假日历覆盖与缓存记账契约测试 (2026-08-26 续编·用户批准).

两个被咬合的病灶:
  A. 缓存记账越权 (P0): _refresh_cache 曾把"请求边界"当"实际覆盖"记账。
     实测 Futu request_trading_days("US",2026-12-01~2027-03-31) 只返回到
     2026-12-31, 旧代码却记到 2027-03-31 → 2027 年日期落进声称区间 →
     不在集合内=判假日。日志显示"缓存刷新成功"完全正常 = 用户所称
     "日志正常但其实 bug"的典型。修后按实际返回边界+可合并区间列表记账。
  B. 覆盖腐化警报: 硬编码表必须始终覆盖未来 ≥180 天, 否则测试失败强制续编。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.utils.holiday_calendar as hc
from app.utils.holiday_calendar import (
    US_HOLIDAYS, US_EARLY_CLOSE_DATES, TradingHolidayCalendar,
)

US = "US"


def _fresh_calendar():
    """重置单例取干净实例 (不触网: __init__ 只建缓存结构)."""
    TradingHolidayCalendar._instance = None
    cal = TradingHolidayCalendar()
    # 强制离线: API 冷却期设到远期, is_trading_day 直接走硬编码层
    cal._api_disabled_until = float("inf")
    return cal


def teardown_function():
    TradingHolidayCalendar._instance = None


# ── B. 覆盖腐化警报 ───────────────────────────────────────────
def test_us_holiday_horizon_anti_rot():
    """硬编码表必须覆盖未来 ≥180 天 — 到期本测试红, 倒逼续编."""
    horizon = date.today() + timedelta(days=180)
    assert max(US_HOLIDAYS) >= horizon, \
        f"US_HOLIDAYS 只到 {max(US_HOLIDAYS)}, 已进入 {horizon} 警戒线内 — 需续编日历"


def test_us_2027_corrected_dates():
    """勘误核验: 受难日2027=3/26 (非4/2); 圣诞观察=12/24 (无12/27)."""
    assert date(2027, 3, 26) in US_HOLIDAYS
    assert date(2027, 4, 2) not in US_HOLIDAYS
    assert date(2027, 12, 24) in US_HOLIDAYS
    assert date(2027, 12, 27) not in US_HOLIDAYS


def test_us_2028_entries():
    """2028 续编抽查: 元旦周六不观察 (1/3周一开市), 受难日4/14, MLK 1/17."""
    assert date(2028, 1, 17) in US_HOLIDAYS
    assert date(2028, 4, 14) in US_HOLIDAYS
    assert date(2028, 1, 3) not in US_HOLIDAYS
    assert date(2028, 11, 24) in US_EARLY_CLOSE_DATES


# ── A. 离线裁决正确性 (硬编码层直判) ──────────────────────────
def test_offline_verdicts_2027():
    cal = _fresh_calendar()
    assert cal.is_trading_day(US, date(2027, 1, 15)) is True   # 普通周五
    assert cal.is_trading_day(US, date(2027, 1, 18)) is False  # MLK
    assert cal.is_trading_day(US, date(2026, 12, 25)) is False # 圣诞
    assert cal.is_trading_day(US, date(2028, 4, 14)) is False  # 受难日2028


def test_offline_weekend_still_rejected():
    cal = _fresh_calendar()
    assert cal.is_trading_day(US, date(2027, 1, 16)) is False  # 周六


# ── A+. 缓存记账: 截断返回不得虚构覆盖 ────────────────────────
def test_truncated_fetch_falls_through_to_hardcode(monkeypatch):
    """Futu 截断返回时: 区间只记实际边界; 超界日期回落硬编码而非误判假日."""
    calls = []

    def fake_fetch(market, start, end):
        calls.append((market, start, end))
        # 截断+窗口内缺日: 远小于请求窗, 且 12-03 缺失
        return ["2026-12-01", "2026-12-02", "2026-12-04"]

    monkeypatch.setattr(hc, "_futu_request_trading_days", fake_fetch)
    TradingHolidayCalendar._instance = None
    cal = TradingHolidayCalendar()

    # 区间内命中日期触发刷新并走缓存层
    assert cal.is_trading_day(US, date(2026, 12, 2)) is True
    rs = cal._cache_ranges[US]
    assert rs == [("2026-12-01", "2026-12-04")], f"必须按实际边界记账: {rs}"
    # 超出实际边界的日期: 即使刷新"成功"(Futu截断)也回落硬编码层 → 判交易日
    assert cal.is_trading_day(US, date(2027, 1, 15)) is True
    assert cal.is_trading_day(US, date(2027, 3, 10)) is True
    # 实际区间内但不在集合中 (2026-12-03 周三缺失): 判非交易日 (语义保留)
    assert cal.is_trading_day(US, date(2026, 12, 3)) is False


def test_gap_between_disjoint_fetches_not_claimed(monkeypatch):
    """两次不连续抓取不得 union 出中间假覆盖 (6月日期须回落硬编码)."""
    seq = [["2027-01-04", "2027-01-05"], ["2027-10-04", "2027-10-05"]]
    state = {"i": 0}

    def fake_fetch(market, start, end):
        r = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return r

    monkeypatch.setattr(hc, "_futu_request_trading_days", fake_fetch)
    TradingHolidayCalendar._instance = None
    cal = TradingHolidayCalendar()
    assert cal.is_trading_day(US, date(2027, 1, 4)) is False or True  # 触发刷新A
    assert cal.is_trading_day(US, date(2027, 10, 4)) is False or True  # 触发刷新B
    rs = cal._cache_ranges[US]
    assert len(rs) == 2, f"两段不连续抓取应为两个区间: {rs}"
    # 6月落在两段之间的空隙: 不得视为已覆盖, 回落硬编码 → 普通周三=交易日
    assert cal.is_trading_day(US, date(2027, 6, 16)) is True


def test_empty_fetch_treated_as_failure(monkeypatch):
    """空列表 ≠ 成功: 必须走降级而非把整段判成非交易日."""
    monkeypatch.setattr(hc, "_futu_request_trading_days",
                        lambda m, s, e: [])
    TradingHolidayCalendar._instance = None
    cal = TradingHolidayCalendar()
    assert cal.is_trading_day(US, date(2027, 3, 10)) is True  # 回落硬编码


# ── C. 2026-09-19 (DEB-20260919 §十三): is_us_holiday 委托 SSOT ─────────
#   病灶: 原实现只查硬编码表, 而它是 6 处盘中守卫 (含 position_exit_monitor /
#   futu_lv3_provider 等热路径) 的唯一判据 ⇒ ①非表内临时休市 ②2029+ 表外年份
#   会被当交易日。修后: 周末→True; SSOT 热缓存→权威; 未覆盖→硬编码表; 都不中→fail-open。

def test_is_us_holiday_covers_weekend_and_hardcoded():
    cal = _fresh_calendar()
    assert cal.is_us_holiday(date(2026, 9, 19)) is True      # 周六
    assert cal.is_us_holiday(date(2026, 9, 20)) is True      # 周日
    assert cal.is_us_holiday(date(2026, 11, 26)) is True     # 感恩节 (硬编码表)
    assert cal.is_us_holiday(date(2026, 9, 21)) is False     # 普通周一 + 无缓存 ⇒ fail-open


def test_is_us_holiday_uses_authoritative_cache_even_if_not_in_hardcoded():
    """核心新增能力: API 缓存说"非交易日"就必须判非交易日 —— 即使它不在硬编码表内。

    用 2026-12-30 (周三, 非表内) 模拟"临时休市/国丧日"被权威源标为非交易日的情况。
    """
    cal = _fresh_calendar()
    d = date(2026, 12, 30)
    assert d not in US_HOLIDAYS
    assert cal.is_us_holiday(d) is False                     # 无缓存 ⇒ 回退硬编码表
    cal._cache_ranges[US] = [("2026-01-01", "2026-12-31")]
    cal._trading_days_cache[US] = {"2026-12-29", "2026-12-31"}   # 未含 d
    assert cal.is_us_holiday(d) is True                      # 缓存权威 ⇒ 非交易日
    assert cal.is_us_holiday(date(2026, 12, 29)) is False    # 缓存内是交易日


def test_is_us_holiday_never_calls_the_network(monkeypatch):
    """热路径契约: 缓存 miss 时**不得**触发 Futu 查询 (否则给秒级/请求级路径塞网络)。"""
    cal = _fresh_calendar()
    called = []

    def _boom(*a, **k):
        called.append(1)
        raise AssertionError("is_us_holiday 不应触发网络/刷新")
    monkeypatch.setattr(cal, "_refresh_cache", _boom)
    monkeypatch.setattr(hc, "_futu_request_trading_days", _boom)
    cal.is_us_holiday(date(2026, 9, 21))
    cal.is_us_holiday(date(2026, 11, 26))
    assert called == []


def test_is_hk_holiday_delegates_same_way():
    cal = _fresh_calendar()
    assert cal.is_hk_holiday(date(2026, 10, 1)) is True       # 国庆 (硬编码表)
    assert cal.is_hk_holiday(date(2026, 9, 19)) is True       # 周六
