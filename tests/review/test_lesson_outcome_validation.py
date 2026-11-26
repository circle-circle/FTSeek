import sqlite3

import pytest

from app.review.daily_review import DailyReviewGenerator
from app.review.lesson_injector import LessonInjector


@pytest.fixture
def injector(tmp_path):
    db = tmp_path / 'lesson_validation.db'
    instance = LessonInjector(db_path=str(db))
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory, "
        "confidence, expiry_date) VALUES ('test', 'timing', 'premarket_loss', 0.5, '2099-01-01')")
    conn.commit()
    conn.close()
    return instance


def row(injector):
    conn = injector._get_conn()
    result = conn.execute('SELECT confidence, expiry_date, validation_count FROM lessons').fetchone()
    conn.close()
    return tuple(result)


def test_summary_legacy_consumers_receive_only_closed_trades():
    generator = DailyReviewGenerator.__new__(DailyReviewGenerator)
    closed = {'entry_time': '2026-11-20T08:30:00-05:00', 'exit_price': 101,
              'entry_price': 100, 'realized_pnl': 1, 'pnl_net': 0.5, 'pnl_pct': 0.01,
              'fees_est': 0.5, 'mfe_pct': 2, 'max_drawdown': 0, 'holding_hours': 1}
    summary = generator._generate_summary([closed], [closed])
    open_trade = dict(closed, exit_price=None, pnl_net=None, realized_pnl=-500)
    summary = generator._generate_summary([closed, open_trade], [closed])
    assert summary['total_trades'] == 2
    assert summary['closed_trades'] == 1
    assert summary['total_pnl_net'] == 0.5
    assert summary['trades'] == [closed]
    assert all(trade['exit_price'] > 0 for trade in summary['trades'])


@pytest.mark.parametrize('trades', [
    [],
    [{'entry_time': '2026-11-20T15:00:00-05:00', 'exit_price': 101, 'pnl_net': 20}],
    [{'entry_time': '2026-11-20T08:30:00-05:00', 'exit_price': None, 'pnl_net': 20}],
    [{'entry_time': 'bad-time', 'exit_price': None, 'pnl_net': -20}],
])
def test_unobserved_or_unsettled_does_not_change_lesson(injector, trades):
    assert injector.validate_lessons(trades) == {'validated': 0, 'invalidated': 0, 'updated': 0}
    assert row(injector) == (0.5, '2099-01-01', 0)


def test_closed_premarket_uses_net_not_gross(injector):
    result = injector.validate_lessons([
        {'entry_time': '2026-11-20T08:30:00-05:00', 'exit_price': 101,
         'realized_pnl': 1, 'pnl_net': -0.5}
    ])
    assert result == {'validated': 1, 'invalidated': 0, 'updated': 1}
    assert row(injector)[0] == 0.6


def high_signal_injector(injector):
    conn = injector._get_conn()
    conn.execute(
        "INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory, "
        "confidence, expiry_date) VALUES ('high signal', 'signal', 'high_signal_loss', 0.5, '2099-01-01')")
    conn.commit()
    conn.close()
    return injector


def test_high_signal_requires_closed_trade(injector):
    injector = high_signal_injector(injector)
    assert injector.validate_lessons([
        {'signal_strength': 80, 'exit_price': None, 'realized_pnl': -10}
    ]) == {'validated': 0, 'invalidated': 0, 'updated': 0}
    conn = injector._get_conn()
    stored = conn.execute(
        "SELECT confidence, expiry_date, validation_count FROM lessons "
        "WHERE lesson_subcategory='high_signal_loss'").fetchone()
    conn.close()
    assert tuple(stored) == (0.5, '2099-01-01', 0)


def test_high_signal_closed_uses_net_not_gross(injector):
    injector = high_signal_injector(injector)
    result = injector.validate_lessons([
        {'signal_strength': 80, 'exit_price': 101, 'realized_pnl': 1, 'pnl_net': -0.5}
    ])
    assert result == {'validated': 1, 'invalidated': 0, 'updated': 1}
    conn = injector._get_conn()
    stored = conn.execute(
        "SELECT confidence, expiry_date, validation_count FROM lessons "
        "WHERE lesson_subcategory='high_signal_loss'").fetchone()
    conn.close()
    assert tuple(stored) == (0.55, stored['expiry_date'], 1)
    assert stored['expiry_date'] != '2099-01-01'


def test_net_fallback_subtracts_estimated_fees(injector):
    assert injector._trade_net_pnl({'realized_pnl': 10, 'fees_est': 2, 'pnl_net': None}) == 8


@pytest.mark.parametrize('timestamp,expected', [
    ('2026-11-20T08:30:00-05:00', True),
    ('2026-11-20T22:15:00+08:00', True),
    ('2026-07-20T21:30:00+08:00', False),
    ('2026-11-20T09:30:00-05:00', False),
    ('2026-11-20T04:00:00-05:00', True),
    ('2026-11-20T03:59:00-05:00', False),
    ('2026-11-20T08:30:00', True),
    ('2026-11-20T09:30:00', False),
    ('2026-03-08T02:30:00', None),
    ('not-a-time', None),
    ('', None),
])
def test_premarket_boundaries_and_unknown_time(injector, timestamp, expected):
    assert injector._is_premarket(timestamp) is expected
