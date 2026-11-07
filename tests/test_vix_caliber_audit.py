"""VIX 口径审计脚本 (RFC-190) 的纯函数承重测试。

守三件:
1. `ratio_stats` 的分位数是 nearest-rank 且不会被小样本误导;
2. `monthly_median_ratios` 按月分组——漂移是这批读数的核心结论 (常数系数不成立),
   分组错了会得出"系数稳定"的相反结论;
3. `gate_impact` 用"该日**前一交易日**现货收盘"判定, 而不是当日收盘
   (盘前门在开盘前就决定, 用当日收盘 = 前视泄漏)。

不触网: 三个函数都只吃传入的 dict/list。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from vix_caliber_audit import gate_impact, monthly_median_ratios, ratio_stats  # noqa: E402


def test_ratio_stats_nearest_rank():
    assert ratio_stats([]) == {}
    stats = ratio_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["n"] == 5
    assert stats["median"] == 3.0
    assert stats["p10"] == 1.0     # ceil/nearest-rank: int(0.1*5)=0 → 第 1 个
    assert stats["p90"] == 5.0
    assert stats["min"] == 1.0 and stats["max"] == 5.0


def test_monthly_median_groups_by_month_and_uses_factor():
    spot = {"2026-01-05": 15.0, "2026-01-06": 21.0, "2026-02-02": 20.0}
    vixy = {"2026-01-05": 20.0, "2026-01-06": 20.0, "2026-02-02": 20.0}
    rows = monthly_median_ratios(spot, vixy, 1.0)
    assert [r[0] for r in rows] == ["2026-01", "2026-02"]
    jan = rows[0]
    assert jan[1] == 2
    assert jan[2] == 0.9           # median(15/20, 21/20) = median(0.75, 1.05) = 0.9
    # 系数变化必须反映到比值上 (0.85 与 1.0 结论不同)
    rows085 = monthly_median_ratios(spot, vixy, 0.85)
    assert rows085[1][2] > rows[1][2]


def test_gate_impact_uses_prior_day_close_not_same_day():
    spot = {"2026-11-04": 16.61, "2026-11-05": 28.18, "2026-11-06": 25.64}
    day_rows = [
        {"trade_date": "2026-11-05", "n": 2, "net": -50.0},   # 前收 16.61 → 不拦
        {"trade_date": "2026-11-06", "n": 18, "net": 109.42},  # 前收 28.18 → 拦
        {"trade_date": "2026-11-09", "n": 3, "net": 1.0},      # 前收 25.64 → 不拦
    ]
    hit = gate_impact(day_rows, spot, 28.0)
    assert [h["trade_date"] for h in hit] == ["2026-11-06"]
    assert hit[0]["entries"] == 18 and hit[0]["net"] == 109.42


def test_gate_impact_skips_days_without_prior_observation():
    assert gate_impact([{"trade_date": "2026-11-04", "n": 1, "net": 1.0}],
                       {"2026-11-04": 99.0}, 28.0) == []