# -*- coding: utf-8 -*-
"""[RFC-117 2026-09-29] UE LONG 破位硬买拦截 — `_long_breakdown_trend()` 单测。

三臂语义 (与 DA signal_gate.py:424-430 "破位硬买拦截" 同口径):
  1. 空头排列 (ma5<ma10<ma20 且 price<ma5) → trend="空头排列" ⇒ inject 侧 SKIP
  2. 多头排列/短期偏多/震荡 → 放行 (不拦)
  3. 缺数 fail-open: bars 不足 20 / router 异常 / None → ("no_data", None) ⇒ 放行

口径钉 (防两把尺子): MA 用 deep_analyzer.py:183-186 同式 round(sum/period, 2),
价格用最近**已完成**日线收盘 (asof 严格大于 bar 日期), 同日去重取最后一条。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts import llm_universe_scan as ue  # noqa: E402


def _bars(closes, start_day=1):
    """构造日线 bars: closes 升序时间排列, 日期用 2026-09-XX 连续工作日近似。"""
    out = []
    for i, c in enumerate(closes):
        d = start_day + i
        out.append({"time": f"2026-08-{d:02d}", "close": float(c),
                    "open": float(c), "high": float(c), "low": float(c)})
    return out


class _FakeRouter:
    def __init__(self, bars=None, exc=None):
        self._bars = bars
        self._exc = exc
        self.calls = []

    def get_daily_bars(self, symbol, count=30):
        self.calls.append((symbol, count))
        if self._exc:
            raise self._exc
        return self._bars


ASOF = "2026-09-01"   # 所有 8 月 bar 都严格早于它


class TestLongBreakdownTrend:
    def test_bearish_arrangement_detected(self):
        """空头排列: ma5<ma10<ma20 且 close<ma5 → 空头排列 (会被 SKIP)。"""
        # 20 根: 前 15 根高位缓降, 后 5 根急降 → ma5 < ma10 < ma20, 末收 < ma5
        closes = [100 - i * 0.5 for i in range(15)] + [92, 90, 88, 86, 84]
        r = _FakeRouter(_bars(closes))
        trend, detail = ue._long_breakdown_trend(r, "US.UBER", ASOF)
        assert trend == "空头排列"
        assert detail is not None
        assert detail["ma5"] < detail["ma10"] < detail["ma20"]
        assert detail["close"] < detail["ma5"]
        assert r.calls and r.calls[0][1] == 60   # 取 60 根 (≥20 即可, 与生产一致)

    def test_bullish_arrangement_passes(self):
        """多头排列 → 放行。"""
        closes = [80 + i * 0.8 for i in range(20)]
        trend, detail = ue._long_breakdown_trend(_FakeRouter(_bars(closes)), "US.NOK", ASOF)
        assert trend == "多头排列"
        assert detail["ma5"] > detail["ma10"] > detail["ma20"]

    def test_short_term_bullish_passes(self):
        """短期偏多 (ma5>ma10 但 ma10<ma20) → 放行 (QCOM 9/8 案例形态, 择时闸不判冲突)。"""
        # 先深跌后急反弹: ma20 高, ma10 中, ma5 最高, 且 price>ma5? 否 — price 可略低于 ma5
        closes = [100 - i for i in range(12)] + [89, 91, 93, 95, 97, 99, 101, 103]
        trend, detail = ue._long_breakdown_trend(_FakeRouter(_bars(closes)), "US.QCOM", ASOF)
        assert trend in ("短期偏多", "多头排列", "震荡")   # 关键: 不是空头排列
        assert trend != "空头排列"

    def test_oscillation_passes(self):
        """震荡 (ma 交错) → 放行。"""
        closes = [100, 99, 101, 98, 102, 97, 103, 96, 104, 95,
                  105, 94, 106, 93, 107, 92, 108, 91, 109, 90]
        trend, _ = ue._long_breakdown_trend(_FakeRouter(_bars(closes)), "US.WDC", ASOF)
        assert trend != "空头排列"

    def test_asof_excludes_same_day_and_later(self):
        """asof 当日及之后的 bar 必须排除 (盘前只能看到昨收) — 用未来 bar 污染验证。"""
        closes = [80 + i * 0.8 for i in range(20)]      # 多头排列
        bars = _bars(closes)
        # 追加 asof 当日+之后的暴跌 bar: 若未排除会把 trend 拉成空头排列
        bars.append({"time": "2026-09-01", "close": 10.0, "open": 10.0,
                     "high": 10.0, "low": 10.0})
        bars.append({"time": "2026-09-02", "close": 5.0, "open": 5.0,
                     "high": 5.0, "low": 5.0})
        trend, detail = ue._long_breakdown_trend(_FakeRouter(bars), "US.X", ASOF)
        assert trend == "多头排列"
        assert detail["bars_asof"] == "2026-08-20"   # 末根 = asof 前最后一个交易日

    def test_same_day_duplicate_takes_last(self):
        """同日重复 bar 取最后一条 (与 compute_screening_atr 去重口径一致)。"""
        closes = [100 - i * 0.5 for i in range(15)] + [92, 90, 88, 86, 84]
        bars = _bars(closes)
        bars.append({"time": "2026-08-20", "close": 84.0, "open": 84.0,
                     "high": 84.0, "low": 84.0})   # 重复末日
        trend, detail = ue._long_breakdown_trend(_FakeRouter(bars), "US.X", ASOF)
        assert detail["bars_used"] == 20           # 去重后仍 20 根
        assert trend == "空头排列"

    def test_insufficient_bars_fail_open(self):
        """bars < 20 → no_data (fail-open 放行, 缺数≠拒绝)。"""
        trend, detail = ue._long_breakdown_trend(_FakeRouter(_bars([100] * 19)), "US.X", ASOF)
        assert trend == "no_data"
        assert detail is None

    def test_empty_bars_fail_open(self):
        trend, detail = ue._long_breakdown_trend(_FakeRouter([]), "US.X", ASOF)
        assert trend == "no_data" and detail is None

    def test_none_router_fail_open(self):
        trend, detail = ue._long_breakdown_trend(None, "US.X", ASOF)
        assert trend == "no_data" and detail is None

    def test_router_exception_fail_open(self):
        """router 抛异常 → no_data (不因取数故障误杀)。"""
        r = _FakeRouter(exc=RuntimeError("futu down"))
        trend, detail = ue._long_breakdown_trend(r, "US.X", ASOF)
        assert trend == "no_data" and detail is None

    def test_ma_uses_round_two(self):
        """口径钉: MA 必须 round(...,2) — 与 deep_analyzer.py:186 逐字一致。"""
        # 构造 round 前后有差异的 closes: ma5 = (1/3)*5 类无限小数
        closes = [10.0] * 15 + [10.0, 10.0, 10.0, 10.0, 10.001]
        _, detail = ue._long_breakdown_trend(_FakeRouter(_bars(closes)), "US.X", ASOF)
        assert detail["ma5"] == round(sum(closes[-5:]) / 5, 2)
        assert str(detail["ma5"]).count(".") <= 1
        assert len(str(detail["ma5"]).split(".")[-1]) <= 2   # 至多两位小数


class TestInjectWiringContract:
    """源级契约钉 (与 test_ue_funnel_batch.py 同款 inspect 手法): 拦截必须接在
    _cmd_inject_impl 的 LONG 分支, 且带 config 回滚开关 + 归因日志。"""

    def test_gate_wired_in_inject_loop(self):
        import inspect
        src = inspect.getsource(ue._cmd_inject_impl)
        assert "_long_breakdown_trend(" in src
        assert "long_momentum_gate_enabled" in src
        assert "空头排列" in src
        assert "_log_inject_loop_reject(" in src   # 拦截必须落归因行

    def test_gate_only_applies_to_long(self):
        import inspect
        src = inspect.getsource(ue._cmd_inject_impl)
        # 拦截条件必须限定 direction == LONG (SHORT 不设门, 与 DA signal_gate 一致)
        assert 's["direction"] == "LONG"' in src
