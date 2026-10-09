"""R-8 契约测试 (2026-08-23, DEB-20260823): 时区正确性.

背景: 宿主时区 = Asia/Shanghai (UTC+8), 交易市场 = 美股 ET (EDT=UTC-4 / EST=UTC-5)。
两者差 12h(夏) / 13h(冬), 且 ET 下午 = 北京次日凌晨 → 任何"日期"语义的裸
datetime.now() 都会把 ET 下午的事件记到 +1 天。

覆盖:
  R-8a  _emit_trigger_miss 遥测 ts/date 必须是 ET (原为 CST, 60% 行错位)
  R-8b  半日市 (提前 13:00 收盘) 必须使 market_phase / is_market_open 判为已收盘
  R-8c  _et_trade_date 对 naive CST 输入在冬令时必须正确 (原固定 -12h, 应 -13h)
"""
import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

from app.utils.timezone import CST, ET, _et_trade_date, is_market_open


class TestR8cEtTradeDateDST:
    """R-8c: naive CST → ET 交易日, 必须跨夏/冬令时都正确."""

    @pytest.mark.parametrize("naive_cst", [
        datetime(2026, 8, 15, 12, 30),   # 夏令时 EDT
        datetime(2026, 8, 15, 21, 40),   # 夏令时, ET 09:40 开盘
        datetime(2026, 1, 15, 12, 30),   # 冬令时 EST — 原实现在此错位
        datetime(2026, 1, 15, 11, 30),   # 冬令时边界外
        datetime(2026, 12, 20, 12, 45),  # 冬令时 — 原实现错位
        datetime(2026, 1, 16, 3, 0),     # 冬令时, ET 前日下午 14:00
        datetime(2026, 11, 1, 12, 30),   # DST 切换当日
    ])
    def test_matches_true_conversion(self, naive_cst):
        """函数结果必须等于按 CST→ET 真实换算的日期."""
        truth = CST.localize(naive_cst).astimezone(ET).strftime("%Y-%m-%d")
        assert _et_trade_date(naive_cst) == truth, (
            f"naive CST {naive_cst} → 函数给 {_et_trade_date(naive_cst)}, 真值 {truth}"
        )

    def test_aware_input_unchanged(self):
        """tz-aware 输入行为不变 (2026-07-24 修复的契约, 不得回退)."""
        aware_et = ET.localize(datetime(2026, 8, 21, 10, 30))
        assert _et_trade_date(aware_et) == "2026-08-21"

    def test_et_afternoon_aware_not_shifted(self):
        """ET 下午 aware 输入不得被二次转换成次日."""
        aware_et = ET.localize(datetime(2026, 8, 21, 15, 45))
        assert _et_trade_date(aware_et) == "2026-08-21"


class TestR8bHalfDayMarket:
    """R-8b: 提前收盘日 (13:00 ET) 必须被识别为已收盘."""

    HALF_DAY = date(2026, 11, 27)   # 感恩节次日, 13:00 ET 收
    REGULAR = date(2026, 11, 24)    # 普通周二

    def test_holiday_calendar_knows_half_day(self):
        from app.utils.holiday_calendar import get_us_close_minutes_et
        assert get_us_close_minutes_et(self.HALF_DAY) == 13 * 60
        assert get_us_close_minutes_et(self.REGULAR) == 16 * 60

    @pytest.mark.parametrize("hhmm,expect_open", [
        ((10, 0), True),    # 半日市上午仍开盘
        ((12, 59), True),   # 收盘前 1 分钟
        ((13, 0), False),   # 13:00 整 = 已收盘
        ((14, 30), False),  # 原实现在此错判为开盘
        ((15, 59), False),  # 原实现在此错判为开盘
    ])
    def test_is_market_open_respects_half_day(self, hhmm, expect_open):
        dt = ET.localize(datetime(self.HALF_DAY.year, self.HALF_DAY.month,
                                  self.HALF_DAY.day, hhmm[0], hhmm[1]))
        assert is_market_open(dt) is expect_open

    def test_is_market_open_regular_day_unchanged(self):
        for hhmm, exp in [((10, 0), True), ((15, 59), True), ((16, 0), False)]:
            dt = ET.localize(datetime(self.REGULAR.year, self.REGULAR.month,
                                      self.REGULAR.day, hhmm[0], hhmm[1]))
            assert is_market_open(dt) is exp, f"{hhmm} 应为 {exp}"

    @pytest.mark.parametrize("hhmm,expect_phase", [
        ((10, 0), "TRADING"),
        ((13, 0), "AFTER_HOURS"),
        ((14, 30), "AFTER_HOURS"),
        ((15, 59), "AFTER_HOURS"),
    ])
    def test_market_phase_respects_half_day(self, hhmm, expect_phase):
        """主调度 SSOT get_market_phase 必须半日市感知 —— 它驱动信号生成与下单。"""
        from app.core import scheduler_triggers as st
        fake_now = ET.localize(datetime(self.HALF_DAY.year, self.HALF_DAY.month,
                                        self.HALF_DAY.day, hhmm[0], hhmm[1]))
        _fake_dt = MagicMock(wraps=datetime)
        _fake_dt.now.return_value = fake_now
        with patch.object(st, "datetime", _fake_dt):
            phase = st.get_market_phase()
        assert phase.name == expect_phase, f"半日市 {hhmm} 应为 {expect_phase}, 实为 {phase.name}"

    def test_half_day_is_not_covered_by_holiday_logic(self):
        """关键前提: 2026-11-27 不在假日表里, 故只能靠半日市逻辑兜住.

        get_market_phase docstring 自述"节假日 (含半日市) 当作 WEEKEND" —— 该断言对
        2026-11-27 **不成立**, 这正是 R-8b 必须修的原因。
        """
        from app.utils.holiday_calendar import get_holiday_calendar, US_EARLY_CLOSE_DATES
        assert get_holiday_calendar().is_us_holiday(self.HALF_DAY) is False
        assert self.HALF_DAY in US_EARLY_CLOSE_DATES

    def test_market_phase_regular_day_unchanged(self):
        from app.core import scheduler_triggers as st
        for hhmm, exp in [((10, 0), "TRADING"), ((15, 59), "TRADING"), ((16, 30), "AFTER_HOURS")]:
            fake_now = ET.localize(datetime(self.REGULAR.year, self.REGULAR.month,
                                            self.REGULAR.day, hhmm[0], hhmm[1]))
            _fake_dt = MagicMock(wraps=datetime)
            _fake_dt.now.return_value = fake_now
            with patch.object(st, "datetime", _fake_dt):
                assert st.get_market_phase().name == exp, f"{hhmm} 应为 {exp}"

    def test_close_lookup_failure_falls_back_to_1600(self):
        """收盘时刻取值失败时兜底 16:00 (不得把整天判成闭市)."""
        from app.core import scheduler_triggers as st
        fake_now = ET.localize(datetime(2026, 11, 24, 14, 0))
        _fake_dt = MagicMock(wraps=datetime)
        _fake_dt.now.return_value = fake_now
        with patch.object(st, "datetime", _fake_dt), \
             patch("app.utils.holiday_calendar.get_us_close_minutes_et",
                   side_effect=RuntimeError("table gone")):
            assert st.get_market_phase().name == "TRADING"


class TestR8aTelemetryUsesET:
    """R-8a: trigger_miss 遥测的 date 必须是 ET 交易日.

    _emit_trigger_miss 是嵌套函数 (pipeline.py:4256, 定义在 auto_execute 函数体内),
    无法直接 import 调用, 故用源码级断言 + 独立验证换算逻辑。
    """

    @staticmethod
    def _src():
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "app" / "playbook" / "pipeline_parts"
                / "trigger_flow.py").read_text(encoding="utf-8")

    def test_date_field_no_longer_naive_now(self):
        src = self._src()
        assert '"date": _dt.now().strftime("%Y-%m-%d")' not in src, \
            "trigger_miss 遥测 date 仍用裸 datetime.now() (CST)"
        assert '"date": _now_et_row.strftime("%Y-%m-%d")' in src

    def test_ts_field_no_longer_naive_now(self):
        src = self._src()
        assert '"ts": _dt.now().isoformat(timespec="seconds")' not in src
        assert '"ts": _now_et_row.isoformat(timespec="seconds")' in src

    def test_tz_marker_present(self):
        """新行带 tz='ET' 标记, 使下游能区分修复前后的行."""
        assert '"tz": "ET"' in self._src()

    def test_et_afternoon_maps_to_same_et_day(self):
        """换算逻辑本身: ET 下午事件的 ET 日期 != 对应的北京日期."""
        et_afternoon = ET.localize(datetime(2026, 8, 21, 14, 30))
        assert et_afternoon.strftime("%Y-%m-%d") == "2026-08-21"
        # 同一瞬间的北京日期是次日 —— 这正是旧实现记错的值
        assert et_afternoon.astimezone(CST).strftime("%Y-%m-%d") == "2026-08-22"

    def test_no_weekend_dates_in_new_rows(self):
        """修复后写入的行 (带 tz 标记) 不得出现周末日期."""
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "data" / "trigger_miss_telemetry.jsonl"
        if not p.exists():
            pytest.skip("遥测文件不存在")
        bad = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("tz") != "ET":
                continue  # 旧行属历史污染, 不在本契约范围
            d = r.get("date")
            if d and datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
                bad.append(d)
        assert not bad, f"修复后仍写入周末日期: {set(bad)}"

    def test_historical_pollution_is_measurable(self):
        """留档: 旧行 (无 tz 标记) 中周末日期的规模, 供下游分析剔除."""
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "data" / "trigger_miss_telemetry.jsonl"
        if not p.exists():
            pytest.skip("遥测文件不存在")
        old_weekend = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("tz") == "ET":
                continue
            d = r.get("date")
            if d and datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
                old_weekend += 1
        # 只断言"能算出来", 不断言具体值 (旧数据是既成事实)
        assert old_weekend >= 0
