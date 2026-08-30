"""财报日历 ET 边界单测 — TZ-2 (DEB-20260830-timezone-duplication-audit)。

病根 (2026-08-30, 修前): 财报窗口/距财报天数锚服务器 CST 日期 — CST 午夜 = ET 正午,
ET 午后服务器日期领先一天 → "财报前3天"门 (screener 排除门 / reentry 证伪门 / 持仓
建议) 的窗口边界整体偏移一天。修: 锚 now_et().date() (SSOT)。

判别式: ET 12/25 15:00 (服务器 CST 墙钟已 12/26 04:00) — 当日财报 ET 口径 days_until=0;
旧 CST 锚判它"昨日"→ 漏出窗口 (screener 财报门放行本该拦的当日财报标的)。
"""
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import app.data.earnings_calendar as ec
import app.playbook.data_collector as dc
from app.data.earnings_calendar import EarningsCalendar
from app.playbook.data_collector import PlaybookDataCollector

ET = ZoneInfo("America/New_York")


class _CstClock(datetime):
    """模拟服务器墙钟: ET 2026-12-25 15:00 = CST 2026-12-26 04:00。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 12, 26, 4, 0)


def test_watchlist_window_anchored_to_et(monkeypatch):
    entries = [
        {"symbol": "AAA", "reportDate": "2026-12-25"},  # ET 今日
        {"symbol": "BBB", "reportDate": "2026-12-26"},  # ET 明日
        {"symbol": "CCC", "reportDate": "2027-01-01"},  # ET 今日+7 (含端点)
        {"symbol": "DDD", "reportDate": "2027-01-02"},  # 今日+8 (窗外)
    ]
    monkeypatch.setattr(EarningsCalendar, "get_earnings_calendar",
                        lambda self, **kw: entries)
    monkeypatch.setattr(ec, "now_et",
                        lambda: datetime(2026, 12, 25, 15, 0, tzinfo=ET))
    # 变异防护: 旧实现走 datetime.now()(CST 墙钟), 冻结之 → 判别不依赖真实运行日
    monkeypatch.setattr(ec, "datetime", _CstClock)

    out = EarningsCalendar().get_watchlist_earnings(days_ahead=7)
    got = {r["ticker"]: r["days_until"] for r in out}

    assert got == {"AAA": 0, "BBB": 1, "CCC": 7}  # DDD 窗外; 旧CST锚漏 AAA


def test_futu_fetch_window_anchored_to_et(monkeypatch):
    """富途主源拉取窗口锚 ET (两段窗口的 today 分界): 断言窗口串由 ET 日期构成。"""
    captured = []

    class _FakeCtx:
        def __init__(self, *a, **kw):
            pass

        def get_earnings_calendar(self, market, begin_date, end_date):
            captured.append((begin_date, end_date))
            return 1, None  # RET_OK, 空 → 走备源, 不需要真数据

        def close(self):
            pass

    monkeypatch.setattr(ec, "now_et",
                        lambda: datetime(2026, 12, 25, 15, 0, tzinfo=ET))
    monkeypatch.setattr(ec, "datetime", _CstClock)
    monkeypatch.setattr("socket.socket.connect", lambda self, addr: None)
    import sys
    import types
    fake_futu = types.ModuleType("futu")
    fake_futu.OpenQuoteContext = _FakeCtx
    fake_futu.RET_OK = 1
    monkeypatch.setitem(sys.modules, "futu", fake_futu)

    EarningsCalendar()._fetch_futu_calendar_primary()

    assert captured[0][1] == "2026-12-25"  # 窗口右端 = ET 今日 (旧CST锚=12/26)


def test_collect_earnings_days_until_et_anchored(monkeypatch):
    """screener 财报门的距日天数 (data_collector→finnhub) 锚 ET。"""
    finnhub = Mock()
    finnhub._get.return_value = {"earningsCalendar": [{"date": "2026-12-25"}]}
    finnhub.get_company_earnings.return_value = []
    collector = PlaybookDataCollector(
        fundamental_provider=Mock(), finnhub_provider=finnhub,
        insider_provider=Mock(), earnings_calendar=Mock())

    import app.utils.timezone as tz_mod
    monkeypatch.setattr(tz_mod, "now_et",
                        lambda: datetime(2026, 12, 25, 15, 0, tzinfo=ET))

    ed = collector._collect_earnings("US.AAPL")

    assert ed.next_date == "2026-12-25"
    assert ed.days_until == 0  # ET 今日; 旧 CST 锚 (12/26) 判昨日 → days_until=-1
    assert ed.quality == "ok"
