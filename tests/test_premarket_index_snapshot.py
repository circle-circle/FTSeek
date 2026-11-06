"""盘前指数快照采集器的判定逻辑守卫（无网络、无 futu）。

守的是三件事:
  ① 窗口外/残值行不得被当成"今日盘前样本"（券商在 01:10 ET 仍返回上一交易日盘前的 pre_change_rate）
  ② 周末不得产样本
  ③ 无成交(pre_volume=0)不得产样本
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import premarket_index_snapshot as pim  # noqa: E402

ET = ZoneInfo("America/New_York")


def _rows(update_time: str, pre_volume: float = 1000.0) -> dict:
    return {c: {"prev_close": 100.0, "last_price": 100.0, "pre_price": 99.0,
                "pre_change_val": -1.0, "pre_change_rate": -1.0,
                "pre_volume": pre_volume, "pre_turnover": 1.0,
                "update_time": update_time} for c in pim.SYMBOLS}


def test_fresh_in_window_is_valid():
    clock = datetime(2026, 11, 6, 8, 32, tzinfo=ET)
    assert pim._evaluate(_rows("2026-11-06 08:31:00.000"), clock)["session_valid"] is True


def test_residual_premarket_data_is_rejected():
    """实测坑: 11-06 01:10 ET 拿到的仍是 11-05 盘前的 pre_change_rate(-1.215%)。"""
    clock = datetime(2026, 11, 6, 8, 32, tzinfo=ET)
    out = pim._evaluate(_rows("2026-11-06 01:09:31.578"), clock)
    assert out["session_valid"] is False
    assert out["stale_symbols"], "陈旧行必须被点名"


def test_outside_premarket_window_is_rejected():
    clock = datetime(2026, 11, 6, 1, 10, tzinfo=ET)
    assert pim._evaluate(_rows("2026-11-06 08:31:00.000"), clock)["session_valid"] is False


def test_weekend_is_rejected():
    clock = datetime(2026, 11, 7, 8, 32, tzinfo=ET)  # Saturday
    assert pim._evaluate(_rows("2026-11-07 08:31:00.000"), clock)["session_valid"] is False


def test_zero_premarket_volume_is_rejected():
    clock = datetime(2026, 11, 6, 8, 32, tzinfo=ET)
    rows = _rows("2026-11-06 08:31:00.000", pre_volume=0.0)
    assert pim._evaluate(rows, clock)["session_valid"] is False


def test_composite_uses_only_traded_symbols():
    clock = datetime(2026, 11, 6, 8, 32, tzinfo=ET)
    rows = _rows("2026-11-06 08:31:00.000")
    rows["US.SMH"]["pre_change_rate"] = -5.0
    out = pim._evaluate(rows, clock)
    assert out["composite_pre_change_rate"] == -2.0  # (-1 -5 -1 -1)/4