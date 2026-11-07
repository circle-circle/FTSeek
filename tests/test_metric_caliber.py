"""指标口径助手的承重测试（DEB-20261106 §28/§29 的报告层落地）。

守三件:
1. **BE 族定义**必须覆盖生产全部 BE/锁利出场 —— 漏一个族会把 BE 笔算进非 BE 分母, 让"死单率"虚高;
2. **死单率只在非 BE 人群内算** —— 全样本口径会被 BE 占比改写 (实测 BE 55%→80% 时 raw 从 28.5% 掉到 14.8%);
3. **报告必须把 BE 出场占比印出来** —— 只改数字不印分母, 下次还是会被误读。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.review.metric_caliber import (  # noqa: E402
    adjusted_dead_rate,
    be_share,
    caliber_note,
    dead_rate_caliber,
    is_be_exit,
)
from app.review.stage_stats import render_stage_section  # noqa: E402


def _row(reason, mfe, net=0.0, stage="regular"):
    return {"stage": stage, "exit_reason": reason, "mfe_pct": mfe, "net": net, "dir_cn": "多"}


def test_is_be_exit_covers_all_production_families():
    for r in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "SL_CERT_LOCK", "TAKE_PROFIT", "EXIT_MONITOR_CLOSE"):
        assert is_be_exit(r), r
    for r in ("SL_INITIAL", "TIME_EXIT", "OVERNIGHT_GAP_STOP", "MFE_STALL_EXIT", "DEAD_SIGNAL_EXIT", None, ""):
        assert not is_be_exit(r), r


def test_be_share_empty_is_none_not_zero():
    assert be_share([]) is None
    assert be_share([_row("SL_BREAKEVEN", 0.5), _row("SL_INITIAL", 0.0)]) == 0.5


def test_dead_rate_is_computed_among_non_be_only():
    rows = [
        _row("SL_BREAKEVEN", 0.5),   # BE: 定义上不可能死单
        _row("SL_TRAIL_LOCK", 0.8),
        _row("SL_INITIAL", 0.0),     # 死单
        _row("TIME_EXIT", 0.1),      # 死单
        _row("PRE_CLOSE_EXIT", 1.0),  # 活
    ]
    cal = dead_rate_caliber(rows)
    assert cal["n"] == 5 and cal["be_n"] == 2
    assert round(cal["be_share"], 4) == 0.4
    assert cal["nonbe_n"] == 3
    assert round(cal["raw_dead"], 4) == 0.4        # 2/5 全样本
    assert round(cal["nonbe_dead"], 4) == round(2 / 3, 4)   # 2/3 非BE —— 可比口径


def test_mfe_missing_counted_in_denominator_but_not_numerator():
    rows = [_row("SL_INITIAL", None), _row("SL_INITIAL", 0.0), _row("SL_INITIAL", 1.0), _row("SL_INITIAL", 1.0)]
    cal = dead_rate_caliber(rows)
    assert cal["mfe_missing"] == 1
    assert round(cal["nonbe_dead"], 4) == 0.25     # 1/4, 缺失不计入分子


def test_adjusted_rate_applies_nonbe_rate_to_reference_mix():
    assert round(adjusted_dead_rate(0.6, 0.5), 4) == 0.3
    assert adjusted_dead_rate(None, 0.5) is None
    assert adjusted_dead_rate(0.6, 1.5) is None


def test_caliber_note_says_incomparable_when_no_nonbe_samples():
    assert "不可比" in caliber_note(None, 0)
    note = caliber_note(0.726, 62, ref_be_share=0.68)
    assert "73%" in note and "同构成校正" in note and "禁用全样本口径" in note


def test_stage_section_renders_be_column_and_caliber_footnote():
    rows = [_row("SL_BREAKEVEN", 0.5, 10.0), _row("SL_INITIAL", 0.0, -30.0), _row("TIME_EXIT", 0.2, -5.0)]
    out = render_stage_section(rows, title="被测段")
    assert "BE出场" in out                      # 分母可见化
    assert "死单口径" in out                    # 口径脚注存在
    assert "跨期比较禁用全样本口径" in out
    # 盘中: 3 笔中 1 笔 BE ⇒ 33%
    assert "33%" in out