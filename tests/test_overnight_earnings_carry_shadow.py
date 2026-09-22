#!/usr/bin/env python3
"""隔夜财报 carry 影子 (record-only 观测面) 的纯逻辑测试 — 无网络、无落盘。

钉住三件事 (2026-09-22 干跑教训驱动):
  ① cohort 选取: 盘前=最近一个已过的 amc 披露日 / 盘后=当日 bmo; 去重 + 代码大写;
  ② 判闸口径: 用 (延长时段价/昨收−1) 计算式, **不用** futu 回报的 `*_change_rate`
     —— 实测该字段在"该票当日延长时段无成交"时返回 0.0 (旧价仍在) ⇒ 会把陈旧读成持平;
  ③ `stale_quote` 必须能把"无新数据"标出来 (否则分析把无数据当"有数据且不涨")。
"""
import importlib.util
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "carry_shadow", ROOT / "scripts" / "overnight_earnings_carry_shadow.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROWS = [
    {"symbol": "AAAA", "date": "2026-09-18", "hour": "amc", "epsEstimate": 1.0, "epsActual": 1.2},
    {"symbol": "BBBB", "date": "2026-09-21", "hour": "amc", "epsEstimate": -0.2, "epsActual": None},
    {"symbol": "bbbb", "date": "2026-09-21", "hour": "amc"},           # 重复 (大小写)
    {"symbol": "CCCC", "date": "2026-09-21", "hour": "bmo"},
    {"symbol": "DDDD", "date": "2026-09-22", "hour": "bmo"},
    {"symbol": "EEEE", "date": "2026-09-22", "hour": "amc"},           # 未来/当日盘后, 盘前窗不用
    {"symbol": "FFFF", "date": "2026-09-22", "hour": ""},
]


def test_premarket_cohort_is_latest_past_amc_day():
    mod = _load()
    cohort, picked = mod.build_cohort("premarket", ROWS, "2026-09-22")
    assert picked == "2026-09-21", "取最近一个已过的 amc 披露日 (9/18 更早, 不用)"
    assert [c["symbol"] for c in cohort] == ["BBBB"], "去重 + 大写归一"


def test_afterhours_cohort_is_today_bmo():
    mod = _load()
    cohort, picked = mod.build_cohort("afterhours", ROWS, "2026-09-22")
    assert picked == "2026-09-22"
    assert [c["symbol"] for c in cohort] == ["DDDD"]


def test_premarket_cohort_empty_when_no_past_amc():
    mod = _load()
    cohort, picked = mod.build_cohort("premarket", ROWS, "2026-09-18")
    assert (cohort, picked) == ([], None)


class _FakeQctx:
    """只实现 get_market_snapshot; 列名与 futu 实盘一致 (measure 口径不得改名)。"""

    def __init__(self, rows):
        self._df = pd.DataFrame(rows)

    def get_market_snapshot(self, codes):
        return 0, self._df[self._df["code"].isin(codes)]


def _snap(code, pre, prev, field_chg, vol, mcap, update="2026-09-22 07:00:00.000"):
    return {"code": code, "pre_price": pre, "prev_close_price": prev,
            "pre_change_rate": field_chg, "pre_volume": vol,
            "total_market_val": mcap, "update_time": update, "last_price": pre}


def test_floor_pass_uses_computed_change_not_reported_field():
    """PASS: 计算式 +3.0% 过闸, 即便回报字段是 0.0 (陈旧字段不得压掉真信号)。"""
    mod = _load()
    cohort = [{"symbol": "PASS1"}]
    q = _FakeQctx([_snap("US.PASS1", 10.30, 10.00, 0.0, 300_000, 2e9)])
    items, stats = mod.evaluate("premarket", cohort, q, "2026-09-22")
    assert items[0]["quote"]["chg"] == pytest.approx(3.0)
    assert items[0]["quote"]["chg_field"] == 0.0
    assert items[0]["passes_floors"] is True and stats["n_passes_floors"] == 1


def test_rejections_and_stale_flag():
    """逐闸拒绝可分解; 旧 update_time 必须标 stale (无数据 ≠ 不涨)。"""
    mod = _load()
    cohort = [{"symbol": "THIN"}, {"symbol": "OLD"}]
    q = _FakeQctx([
        _snap("US.THIN", 10.50, 10.00, 0.0, 50_000, 2e9),                  # 量不足
        _snap("US.OLD", 12.00, 10.00, 0.0, 500_000, 2e9,
              update="2026-09-21 20:01:23.898"),                            # 昨盘后陈旧价
    ])
    items, stats = mod.evaluate("premarket", cohort, q, "2026-09-22")
    assert items[0]["reject"] == ["volume_below_min"]
    assert items[1]["quote"]["stale_quote"] is True
    assert "stale_quote" in items[1]["reject"], "陈旧报价不得计入过闸上界"
    assert items[1]["passes_floors"] is False
    assert stats["n_passes_floors"] == 0


def test_price_and_market_cap_floors_are_decomposed():
    mod = _load()
    cohort = [{"symbol": "PENNY"}, {"symbol": "SMALL"}, {"symbol": "NODATA"}]
    q = _FakeQctx([
        _snap("US.PENNY", 3.00, 2.90, 0.0, 400_000, 2e9),
        _snap("US.SMALL", 9.00, 8.00, 0.0, 400_000, 5e8),
    ])
    items, _ = mod.evaluate("premarket", cohort, q, "2026-09-22")
    by_sym = {i["symbol"]: i for i in items}
    assert "price_below_min" in by_sym["PENNY"]["reject"]
    assert "market_cap_below_min" in by_sym["SMALL"]["reject"]
    assert by_sym["NODATA"]["reject"] == ["no_quote"] and by_sym["NODATA"]["quote"] is None


def test_num_handles_nan_none_and_strings():
    mod = _load()
    assert mod._num(None) is None
    assert mod._num(float("nan")) is None
    assert math.isclose(mod._num("10.5"), 10.5)
    assert mod._num("") is None


def test_script_is_record_only_by_construction():
    """纪律钉: 本影子不得含任何下单/写库/改配置调用 (record-only 是可验证属性)。"""
    src = (ROOT / "scripts" / "overnight_earnings_carry_shadow.py").read_text()
    for forbidden in ("place_order", "modify_order", "cancel_order", "sqlite3",
                      "os.environ[", "setenv"):
        assert forbidden not in src, "影子脚本出现 %s ⇒ 已越出 record-only 边界" % forbidden