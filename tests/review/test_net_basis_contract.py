"""RFC-218 §22 (P1-A): 未知净额统一合同契约。

钉死四类东西:
  1) **与官方评分器同源** — resolve_net_pnl 与 scripts/okr_2026_11_scoring._row_net
     在全输入矩阵上逐字一致 (parity); 改任一侧语义必须两侧同裁。
  2) **审计合成表** — 毛10/费未知/净未知 ⇒ None (不可测, 旧日报/旧教训helper=10);
     毛10/费4/净缺失 ⇒ 6 (旧日报=10)。未知≠0≠毛。
  3) **日报聚合** — 净额/胜率/均额只覆盖可测样本, net_unknown_count 显式披露;
     完整旧数据 (pnl_net 在场) 结果逐字不变。
  4) **教训不自证** — 未知净额样本不更新 confidence/expiry/validation_count;
     返回 dict 形状 {'validated','invalidated','updated'} 不变。
"""
import itertools
import math
import sqlite3

import pytest

from app.review.daily_review import DailyReviewGenerator
from app.review.lesson_injector import LessonInjector
from app.review.net_basis import resolve_net_pnl
from scripts import okr_2026_11_scoring as scoring


# ── 1) parity: 与官方评分器 _row_net 全矩阵一致 ──────────────────────────────

_NET_VALUES = [None, "", 0, 6, -5, "6.5", float("nan"), float("inf"), True, "abc"]
_GF_VALUES = [None, "", 10, 0, "10", float("nan"), "abc"]


@pytest.mark.parametrize("net,gross,fees", list(itertools.product(_NET_VALUES, _GF_VALUES, _GF_VALUES)))
def test_parity_with_official_scoring_row_net(net, gross, fees):
    row = {"pnl_net": net, "realized_pnl": gross, "fees_est": fees}
    got, want = resolve_net_pnl(row), scoring._row_net(row)
    if want is None:
        assert got is None
    else:
        assert got == pytest.approx(want)


# ── 2) 审计合成表 (独立审计 2026-11-27 原表) ─────────────────────────────────

@pytest.mark.parametrize("row,expected", [
    ({"realized_pnl": 10, "fees_est": None, "pnl_net": None}, None),   # 不可测 (旧=10)
    ({"realized_pnl": 10, "fees_est": 4, "pnl_net": None}, 6.0),       # 回算 (旧日报=10)
    ({"realized_pnl": 10, "fees_est": 4, "pnl_net": 6}, 6.0),          # 已知净保留
    ({"realized_pnl": 10, "fees_est": 4, "pnl_net": -2}, -2.0),        # 已知净优先于毛-费
    ({"realized_pnl": 10}, None),                                       # 缺费键=未知
    ({}, None),
    ({"realized_pnl": 0, "fees_est": None}, None),                      # 未知≠0
    ({"realized_pnl": 0, "fees_est": 0}, 0.0),                          # 已知零=可测
    ({"pnl_net": float("nan"), "realized_pnl": 10, "fees_est": 4}, 6.0),  # 坏净→回算
    ({"pnl_net": float("nan"), "realized_pnl": 10}, None),              # 坏净+缺费→不可测
])
def test_audit_synthetic_table(row, expected):
    got = resolve_net_pnl(row)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_lesson_helper_shares_contract():
    assert LessonInjector._trade_net_pnl({"realized_pnl": 10, "fees_est": None, "pnl_net": None}) is None
    assert LessonInjector._trade_net_pnl({"realized_pnl": 10, "fees_est": 4, "pnl_net": None}) == 6.0
    assert LessonInjector._trade_net_pnl({"realized_pnl": 10, "fees_est": 2, "pnl_net": None}) == 8.0


# ── 3) 日报聚合 ──────────────────────────────────────────────────────────────

def _closed(symbol, gross, fees, net, **kw):
    return {"symbol": symbol, "realized_pnl": gross, "fees_est": fees, "pnl_net": net,
            "exit_price": 101, "entry_price": 100, "pnl_pct": 0.001, "mfe_pct": 0.5,
            "holding_hours": 1.0, "max_drawdown": 0, **kw}


def _summary(trades):
    gen = DailyReviewGenerator.__new__(DailyReviewGenerator)   # 不触发 __init__ 的 IO
    return gen._generate_summary(trades, trades, None)


def test_summary_mixed_unknown_excluded_not_gross_not_zero():
    trades = [_closed("AAA", 100.0, 5.0, 95.0),
              _closed("UNK", 10.0, None, None),      # 不可测 (旧实现把 10 当净)
              _closed("CALC", 10.0, 4.0, None)]      # 回算 6 (旧实现把 10 当净)
    s = _summary(trades)
    assert s["total_pnl"] == s["total_pnl_net"] == pytest.approx(101.0)
    assert s["net_measurable_n"] == 2 and s["net_unknown_count"] == 1
    assert s["win_rate"] == s["win_rate_net"] == 1.0          # 分母=可测 2, 不是 3
    assert s["avg_pnl"] == pytest.approx(50.5)
    assert s["winning_trades"] == 2 and s["losing_trades"] == 0
    assert s["max_single_net_pnl"] == pytest.approx(95.0) and s["max_single_symbol"] == "AAA"
    assert s["fake_wins"] == 0                                 # 未知不算假盈利
    assert "net:" in s["pnl_basis"]
    # 毛/费仍是全量事实披露 (不随净额覆盖收缩)
    assert s["total_pnl_gross"] == pytest.approx(120.0)
    assert s["fees_total"] == pytest.approx(9.0)


def test_summary_all_unknown_does_not_fake_measurable_day():
    s = _summary([_closed("A", 10.0, None, None), _closed("B", -8.0, None, None)])
    assert s["net_measurable_n"] == 0 and s["net_unknown_count"] == 2
    assert s["win_rate"] == 0 and s["avg_pnl"] == 0
    assert s["winning_trades"] == 0 and s["losing_trades"] == 0
    assert s["max_single_symbol"] == "" and s["single_trade_share_abs"] is None


def test_summary_fake_wins_only_over_measurable():
    trades = [_closed("FW", 10.0, 20.0, -10.0),     # 毛赢净亏 = 假盈利
              _closed("UNK", 10.0, None, None)]     # 费未知 ⇒ 不可测, 不得计入假盈利
    s = _summary(trades)
    assert s["fake_wins"] == 1
    assert s["net_unknown_count"] == 1


def test_summary_complete_old_data_unchanged():
    """完整旧数据 (pnl_net 全在场): 所有键与修复前语义逐字一致。"""
    trades = [_closed("AAA", 100.0, 5.0, 95.0), _closed("BBB", -50.0, 5.0, -55.0)]
    s = _summary(trades)
    assert s["total_pnl"] == s["total_pnl_net"] == pytest.approx(40.0)
    assert s["total_pnl_gross"] == pytest.approx(50.0)
    assert s["fees_total"] == pytest.approx(10.0)
    assert s["total_pnl_gross"] - s["fees_total"] == pytest.approx(s["total_pnl_net"])
    assert s["win_rate"] == s["win_rate_net"] == 0.5
    assert s["avg_pnl"] == pytest.approx(20.0)
    assert s["winning_trades"] == 1 and s["losing_trades"] == 1
    assert s["fake_wins"] == 0
    assert s["net_measurable_n"] == 2 and s["net_unknown_count"] == 0
    assert s["max_single_net_pnl"] == pytest.approx(95.0)


def test_summary_empty_day_coverage_keys():
    s = _summary([])
    assert s["net_measurable_n"] == 0 and s["net_unknown_count"] == 0


def test_review_score_win_rate_unknown_not_winning():
    gen = DailyReviewGenerator.__new__(DailyReviewGenerator)
    # 毛赢净亏 (净已知) + 毛赢净未知: 旧实现两者都算胜场 (2/2=100%)
    trades = [_closed("FW", 10.0, 20.0, -10.0), _closed("UNK", 10.0, None, None)]
    out = gen._calculate_review_score({}, {}, {}, {}, trades)
    assert out["breakdown"]["win_rate"] == 0.0
    # 完整数据对照: 1 胜 1 负 = 50% (与修复前一致)
    full = [_closed("W", 100.0, 5.0, 95.0), _closed("L", -50.0, 5.0, -55.0)]
    out = gen._calculate_review_score({}, {}, {}, {}, full)
    assert out["breakdown"]["win_rate"] == 0.5


def test_review_score_fees_known_net_missing_uses_net_not_gross():
    gen = DailyReviewGenerator.__new__(DailyReviewGenerator)
    # 毛 10 费 12 ⇒ 净 -2: 旧实现回退毛额算胜场, 现在算负场
    trades = [_closed("X", 10.0, 12.0, None)]
    out = gen._calculate_review_score({}, {}, {}, {}, trades)
    assert out["breakdown"]["win_rate"] == 0.0


# ── 4) 教训验证: 未知不自证 ──────────────────────────────────────────────────

@pytest.fixture
def injector(tmp_path):
    db = tmp_path / "lesson_net_contract.db"
    instance = LessonInjector(db_path=str(db))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory, "
        "confidence, expiry_date) VALUES ('test', 'timing', 'premarket_loss', 0.5, '2099-01-01')")
    conn.commit()
    conn.close()
    return instance


def _row(injector):
    conn = injector._get_conn()
    result = conn.execute("SELECT confidence, expiry_date, validation_count FROM lessons").fetchone()
    conn.close()
    return tuple(result)


def _premarket(**kw):
    return dict({"entry_time": "2026-11-20T08:30:00-05:00", "exit_price": 101}, **kw)


def test_premarket_unknown_net_does_not_validate_or_invalidate(injector):
    # 毛 +10 费未知 净未知: 旧 helper 回退毛额 ⇒ 误判 invalidated; 现在不可测 ⇒ 原样保留
    result = injector.validate_lessons([_premarket(realized_pnl=10, fees_est=None, pnl_net=None)])
    assert result == {"validated": 0, "invalidated": 0, "updated": 0}
    assert _row(injector) == (0.5, "2099-01-01", 0)


def test_premarket_fees_known_net_missing_uses_net(injector):
    # 毛 +10 费 12 ⇒ 净 -2 < 0 ⇒ validated (旧实现毛 +10 ⇒ 误判 invalidated)
    result = injector.validate_lessons([_premarket(realized_pnl=10, fees_est=12, pnl_net=None)])
    assert result == {"validated": 1, "invalidated": 0, "updated": 1}
    assert _row(injector)[0] == 0.6


def test_premarket_unknown_excluded_from_mixed_sum(injector):
    # 未知毛 +100 不得抵消可测净 -1 ⇒ 总和 -1 < 0 ⇒ validated
    result = injector.validate_lessons([
        _premarket(realized_pnl=100, fees_est=None, pnl_net=None),
        _premarket(realized_pnl=1, fees_est=2, pnl_net=-1),
    ])
    assert result == {"validated": 1, "invalidated": 0, "updated": 1}
    assert _row(injector)[0] == 0.6


def test_high_signal_unknown_net_does_not_update(injector):
    conn = injector._get_conn()
    conn.execute(
        "INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory, "
        "confidence, expiry_date) VALUES ('hs', 'signal', 'high_signal_loss', 0.5, '2099-01-01')")
    conn.commit()
    conn.close()
    result = injector.validate_lessons([
        {"signal_strength": 80, "exit_price": 101, "realized_pnl": 10, "fees_est": None, "pnl_net": None}])
    assert result == {"validated": 0, "invalidated": 0, "updated": 0}
    conn = injector._get_conn()
    stored = conn.execute(
        "SELECT confidence, expiry_date, validation_count FROM lessons "
        "WHERE lesson_subcategory='high_signal_loss'").fetchone()
    conn.close()
    assert tuple(stored) == (0.5, "2099-01-01", 0)


def test_high_signal_avg_over_measurable_only(injector):
    conn = injector._get_conn()
    conn.execute(
        "INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory, "
        "confidence, expiry_date) VALUES ('hs', 'signal', 'high_signal_loss', 0.5, '2099-01-01')")
    conn.commit()
    conn.close()
    # 未知毛 +100 不入均值; 可测净 -1 ⇒ avg -1 < 0 ⇒ validated
    result = injector.validate_lessons([
        {"signal_strength": 80, "exit_price": 101, "realized_pnl": 100, "fees_est": None, "pnl_net": None},
        {"signal_strength": 80, "exit_price": 101, "realized_pnl": 1, "fees_est": 2, "pnl_net": -1},
    ])
    assert result == {"validated": 1, "invalidated": 0, "updated": 1}
