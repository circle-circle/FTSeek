# -*- coding: utf-8 -*-
"""P1 三轨合一契约测试 (2026-09-06): overnight_eod_decision == 合并前旧链.

合并前两处 EOD 决策面 (poll PRE_CLOSE / EOD 兜底 sweeper) 各自链式调用:
    轨③ ir.overnight_loser_exempt (先判) → W_WEEK pipeline._loser_overnight_exempt
合并后唯一入口 app.playbook.overnight_decision.overnight_eod_decision。

本文件钉锁:
  A. 全组合等价: 360 组 (2×2 开关 × 5 entry × 3 pnl × 3 checker × 2 source_type),
     逐组断言 decision == 旧链 (ir.overnight_loser_exempt or pipeline._loser_overnight_exempt),
     且 track 标签与"实际豁免臂"一致 ("reentry_first_night"/"w_week"/None, RFC-067 值域);
  B. 关键语义显式钉: 首夜→次夜换手 (轨③失效 W_WEEK 接管)、轨③无 source 过滤、
     W_WEEK SIM-only、水深闸只管 W_WEEK 臂、checker 异常落 W_WEEK;
  C. pipeline 转发器忠实: _loser_overnight_exempt / _horizon_max_hold_hit /
     _overnight_eod_exempt 与 overnight_decision 本体逐一相等。
"""
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import app.playbook.intraday_reentry as ir  # noqa: E402
import app.playbook.pipeline as pl  # noqa: E402
from app.playbook import overnight_decision as od  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402


@pytest.fixture(autouse=True)
def offline_calendar(monkeypatch):
    from app.utils import holiday_calendar
    monkeypatch.setattr(holiday_calendar.TradingHolidayCalendar, "_instance", None)
    monkeypatch.setattr(holiday_calendar, "_futu_request_trading_days", lambda *args: None)


YES = lambda sym, date: True   # noqa: E731
NO = lambda sym, date: False   # noqa: E731


def _raise(sym, date):
    raise RuntimeError("db down")


def _today():
    return now_et().replace(hour=11, minute=30).isoformat()


def _yesterday():
    return (now_et() - timedelta(days=1)).isoformat()


def _long_ago():
    # 40 天前 → 远超 5 交易日上限 (max_hold 必命中, 历史日历数据必有)
    return (now_et() - timedelta(days=40)).isoformat()


def _set_cfgs(monkeypatch, reentry_on, wweek_on, max_depth=99, max_days=5):
    monkeypatch.setattr(ir, "_cfg", lambda: {"overnight_hold_loser_enabled": reentry_on})
    fake = {
        "loser_overnight_enabled": wweek_on,
        "loser_overnight_max_depth_pct": max_depth,
        "loser_overnight_max_hold_days": max_days,
    }
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)


# ── A. 全组合等价 (合并前旧链 = 金标准) ─────────────────────────────

_ENTRIES = [("today", _today), ("yesterday", _yesterday), ("long_ago", _long_ago),
            ("garbage", lambda: "not-a-date"), ("empty", lambda: "")]
_PNLS = [-0.8, 0.5, None]
_CHECKERS = [("YES", YES), ("NO", NO), ("RAISE", _raise)]
_SOURCES = ["PLAYBOOK_SIM", "PLAYBOOK_LIVE"]


def test_full_matrix_equivalent_to_legacy_chain(monkeypatch):
    """360 组: decision == (轨③ or W_WEEK), 标签 == 实际豁免臂. 逐组独立配置."""
    n = 0
    for r3_on in (True, False):
        for ww_on in (True, False):
            _set_cfgs(monkeypatch, r3_on, ww_on)
            for ename, efn in _ENTRIES:
                entry = efn()
                for pnl in _PNLS:
                    for cname, checker in _CHECKERS:
                        for src in _SOURCES:
                            # 金标准 = 合并前旧链 (两个现存函数的原样组合)
                            t3 = ir.overnight_loser_exempt("SYM", entry, pnl, is_reentry=checker)
                            ww = pl._loser_overnight_exempt(src, entry, pnl)
                            expected = (bool(t3 or ww),
                                        "reentry_first_night" if t3
                                        else ("w_week" if ww else None))
                            actual = od.overnight_eod_decision(
                                "SYM", src, entry, pnl, is_reentry=checker)
                            assert actual == expected, (
                                f"失配 r3={r3_on} ww={ww_on} entry={ename} pnl={pnl} "
                                f"checker={cname} src={src}: {actual} != {expected}")
                            n += 1
    assert n == 2 * 2 * 5 * 3 * 3 * 2  # 360 组全覆盖 (基数自检)


# ── B. 关键语义显式钉 ───────────────────────────────────────────────

def test_first_night_reentry_track_label(monkeypatch):
    _set_cfgs(monkeypatch, True, True)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), -0.8,
                                     is_reentry=YES) == (True, "reentry_first_night")


def test_second_night_handoff_to_w_week(monkeypatch):
    """轨③仅首夜 → 次夜起 W_WEEK 接管 (HOOD 9/4 实弹场景: source 标签随夜换手)."""
    _set_cfgs(monkeypatch, True, True)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _yesterday(), -0.8,
                                     is_reentry=YES) == (True, "w_week")


def test_track3_has_no_source_filter_pinned(monkeypatch):
    """轨③不查 source_type (既有语义 — reentry 仓实践中恒 SIM, 此处钉住不对称性)."""
    _set_cfgs(monkeypatch, True, False)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_LIVE", _today(), -0.8,
                                     is_reentry=YES) == (True, "reentry_first_night")


def test_w_week_dual_arm(monkeypatch):
    """RFC-130 (2026-10-03 用户裁「sim和实盘策略一致」): W_WEEK 豁免双臂同语义.

    原 test_w_week_sim_only 断言 LIVE 不豁免 (False, None) — 该分叉已按用户裁决
    删除, LIVE 臂继承同一 W_WEEK 语义。SHADOW/其他源仍不豁免 (非双臂白名单)。
    """
    _set_cfgs(monkeypatch, False, True)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_LIVE", _today(), -0.8,
                                     is_reentry=NO) == (True, "w_week")
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), -0.8,
                                     is_reentry=NO) == (True, "w_week")
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SHADOW", _today(), -0.8,
                                     is_reentry=NO) == (False, None)


def test_both_off_never_exempt(monkeypatch):
    _set_cfgs(monkeypatch, False, False)
    for efn in (_today, _yesterday):
        assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", efn(), -0.8,
                                         is_reentry=YES) == (False, None)


def test_profitable_never_exempt(monkeypatch):
    _set_cfgs(monkeypatch, True, True)
    for pnl in (0.0, 0.5, 2.3):
        assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), pnl,
                                         is_reentry=YES) == (False, None)


def test_checker_exception_falls_through_to_w_week(monkeypatch):
    """轨③ checker 异常 → 轨③不豁免 → W_WEEK 照常评 (合并前链式行为)."""
    _set_cfgs(monkeypatch, True, True)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), -0.8,
                                     is_reentry=_raise) == (True, "w_week")


def test_depth_gate_only_guards_w_week_arm(monkeypatch):
    """水深闸在 W_WEEK 臂内 (轨③无水深概念): 深水 reentry 首夜仍豁免, 非 reentry 不豁免."""
    _set_cfgs(monkeypatch, True, True, max_depth=2.0)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), -3.0,
                                     is_reentry=YES) == (True, "reentry_first_night")
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), -3.0,
                                     is_reentry=NO) == (False, None)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _today(), -1.5,
                                     is_reentry=NO) == (True, "w_week")


def test_max_hold_expiry_blocks_w_week(monkeypatch):
    _set_cfgs(monkeypatch, True, True)
    # 40 天前入场: 轨③非首夜 False; W_WEEK 超 5 交易日上限 False
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", _long_ago(), -0.8,
                                     is_reentry=YES) == (False, None)


def test_garbage_entry_w_week_quirk_pinned(monkeypatch):
    """既有怪癖钉住 (等价≠美化): entry 无法解析时轨③保守 False, 但 W_WEEK 的
    max_hold 判定异常→不命中→仍豁免. 行为等价要求原样保留; P2 再评是否收紧."""
    _set_cfgs(monkeypatch, False, True)
    assert od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", "not-a-date", -0.8,
                                     is_reentry=NO) == (True, "w_week")


def test_bool_projection_matches_decision(monkeypatch):
    _set_cfgs(monkeypatch, True, True)
    for efn in (_today, _yesterday):
        for pnl in _PNLS:
            d = od.overnight_eod_decision("SYM", "PLAYBOOK_SIM", efn(), pnl, is_reentry=YES)
            assert od.overnight_eod_exempt("SYM", "PLAYBOOK_SIM", efn(), pnl,
                                           is_reentry=YES) == d[0]


# ── C. pipeline 转发器忠实 ──────────────────────────────────────────

def test_pipeline_forwarders_match_new_home(monkeypatch):
    _set_cfgs(monkeypatch, True, True, max_depth=2.0)
    for efn in (_today, _yesterday, _long_ago, lambda: "not-a-date", lambda: ""):
        entry = efn()
        for pnl in (-3.0, -0.8, 0.5, None):
            for src in _SOURCES:
                assert pl._loser_overnight_exempt(src, entry, pnl) == \
                    od.w_week_loser_exempt(src, entry, pnl)
                # is_reentry=NO: 避开默认查库路径 (单测不打生产 DB)
                assert pl._overnight_eod_exempt("SYM", src, entry, pnl, is_reentry=NO) == \
                    od.overnight_eod_exempt("SYM", src, entry, pnl, is_reentry=NO)
    for days in (0, 1, 5):
        for efn in (_today, _long_ago, lambda: "", lambda: "not-a-date"):
            assert pl._horizon_max_hold_hit(efn(), days) == \
                od.horizon_max_hold_hit(efn(), days)
