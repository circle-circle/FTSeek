"""MarketHoursCompat 时区回归 (2026-08-10).

缺陷: MarketHoursCompat._now() 用 datetime.now() (服务器本地, 本机 CST),
get_market_phase 把 naive 时间按"市场时区" localize → 美股真实盘中
(北京 21:30-04:00) 全被误判 OVERNIGHT:
  - 订阅管理器 20:00 (北京) 误退订动态活跃集 ("美股收盘" 假日志)
  - 真实交易时段内重启, 动态集被拒绝订阅 (应订集合从不落地)
  - 盘中新增指令标的不被增量订阅

修复: _now 缺省改取 get_current_time(market) (tz-aware 市场本地时间).
本文件锁定: 无论服务器在哪个时区, 盘中判定只取决于真实美东时间。
"""
from datetime import datetime

import pytz

import app.shared.market_hours as mh

CST = pytz.timezone("Asia/Shanghai")
ET = pytz.timezone("America/New_York")


def _patch_server_clock(monkeypatch, server_naive: datetime):
    """模拟服务器本地时钟 (任意时区), datetime.now(tz) 语义与标准库一致."""
    fixed_utc = CST.localize(server_naive).astimezone(pytz.utc)

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            if tz is None:
                return server_naive
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(mh, "datetime", _FakeDatetime)


class TestUSSessionDetection:
    def test_real_trading_session_not_misjudged_overnight(self, monkeypatch):
        """北京 23:39 (= ET 11:39, 盘中) → is_us_active 必须 True (旧 bug: False)."""
        _patch_server_clock(monkeypatch, datetime(2026, 8, 10, 23, 39))
        assert mh.MarketHours.is_us_active() is True
        assert mh.MarketHours.is_us_closed() is False

    def test_beijing_2000_is_premarket_not_closed(self, monkeypatch):
        """北京 20:00 (= ET 08:00, 盘前) → 不得判收盘 (旧 bug: 此时误退订)."""
        _patch_server_clock(monkeypatch, datetime(2026, 8, 10, 20, 0))
        assert mh.MarketHours.is_us_active() is True
        assert mh.MarketHours.is_us_closed() is False

    def test_true_overnight_still_detected(self, monkeypatch):
        """北京 09:00 (= ET 21:00 前一日, 真实隔夜) → closed 保持 True (防误放开)."""
        _patch_server_clock(monkeypatch, datetime(2026, 8, 11, 9, 0))
        assert mh.MarketHours.is_us_closed() is True
        assert mh.MarketHours.is_us_active() is False

    def test_explicit_naive_arg_contract_unchanged(self, monkeypatch):
        """显式 naive 入参契约不变: 仍按市场时区 (ET) 解释."""
        _patch_server_clock(monkeypatch, datetime(2026, 8, 10, 23, 39))
        # 显式传 11:39 naive = ET 11:39 → 盘中
        assert mh.MarketHours.is_us_active(datetime(2026, 8, 10, 11, 39)) is True
        # 显式传 23:39 naive = ET 23:39 → 隔夜
        assert mh.MarketHours.is_us_active(datetime(2026, 8, 10, 23, 39)) is False
