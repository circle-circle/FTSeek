"""Isolated broker exit lifecycle contracts (shares, absolute USD, epoch seconds)."""
import logging
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from app.execution.dual_broker import DualBrokerAdapter


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    violations = []
    original_connect = sqlite3.connect
    # 允许的临时根必须与 pytest 同轴: pytest 的 tmp_path/basetemp 跟随 TMPDIR, 本机
    # TMPDIR=/root/.hermes/cache/scratch (不在 /tmp 下)。原写法硬编码 '/tmp/' 前缀 ⇒
    # 该环境下**全部 112 个退出链用例恒红** (violations=[('sqlite',
    # '.../pytest-of-root/pytest-N/test_x_0/exits.db')]), 隔离守卫实际失效 —— 真回归会被
    # 淹没在环境噪音里 (2026-09-27 实测: 默认 TMPDIR 下 102 failed/110 errors,
    # TMPDIR=/tmp 下同一 worktree 112 passed ⇒ 与代码无关)。
    # 守卫强度不变: 生产路径 (/opt/ftseek/FTSeek-US/data/*.db) 不在任何临时根下 ⇒ 照样拦。
    _tmp_roots = (str(Path(tempfile.gettempdir()).resolve()).rstrip('/'), '/tmp')

    def sql_connect(path, *args, **kwargs):
        _p = str(Path(path).resolve())
        if str(path) != ':memory:' and not any(_p.startswith(r + '/') for r in _tmp_roots):
            violations.append(('sqlite', str(path)))
            raise AssertionError('non-test database')
        return original_connect(path, *args, **kwargs)

    def blocked(*args, **kwargs):
        violations.append(('external', str(args)))
        raise AssertionError('external operation')

    monkeypatch.setattr(sqlite3, 'connect', sql_connect)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    import futu
    monkeypatch.setattr(futu, 'OpenSecTradeContext', blocked)
    monkeypatch.setattr(futu, 'OpenQuoteContext', blocked)
    yield violations
    assert violations == []


class Clock:
    def __init__(self):
        self.now = 1800000000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Context:
    def __init__(self):
        self.orders = []
        self.rows = {}
        self.history = []
        self.cancel_calls = []
        self.position_qty = 10
        self.cancel_status = None
        self.fill_market = False
        self.hide_current = False
        self.lose_submit = False

    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame([{'code': 'US.TEST', 'qty': self.position_qty,
                                 'position_side': 'LONG' if self.position_qty > 0 else 'SHORT'}])

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        order_id = str(len(self.orders))
        row = dict(kwargs, order_id=order_id, order_status='SUBMITTED', dealt_qty=0,
                   dealt_avg_price=0)
        if str(kwargs['order_type']) == 'MARKET' and self.fill_market:
            row.update(order_status='FILLED_ALL', dealt_qty=kwargs['qty'], dealt_avg_price=110)
        self.rows[order_id] = row
        if self.lose_submit:
            raise TimeoutError('response lost')
        return 0, pd.DataFrame([row])

    def order_list_query(self, order_id=None, **kwargs):
        if self.hide_current:
            return 0, pd.DataFrame()
        rows = [r for k, r in self.rows.items() if not order_id or k == order_id]
        return 0, pd.DataFrame(rows)

    def history_order_list_query(self, **kwargs):
        return 0, pd.DataFrame(self.history)

    def modify_order(self, order_id, **kwargs):
        self.cancel_calls.append(order_id)
        if self.cancel_status:
            self.rows[order_id].update(self.cancel_status)
        return -1, 'cancel rejected'

    def history_deal_list_query(self, **kwargs):
        return -1, 'not supported'


def owner(entry='entry-1', **changes):
    value = {'close_key': f'entry:{entry}', 'kind': 'trade', 'db_path': '/tmp/owned.db',
             'trade_id': 1, 'instruction_id': 2, 'entry_order_id': entry,
             'entry_time': '2026-09-10 10:00:00', 'entry_price': 100,
             'quantity': 10, 'source_type': 'PLAYBOOK_SIM',
             'symbol': 'US.TEST', 'direction': 'LONG'}
    value.update(changes)
    return value


def adapter(tmp_path, ctx=None, clock=None):
    broker = DualBrokerAdapter.__new__(DualBrokerAdapter)
    broker._exit_ledger_path = tmp_path / 'exits.db'
    broker._sim_ctx = ctx or Context()
    broker._real_ctx = None
    broker.sim_account_id = '42'
    broker.real_account_id = '99'
    broker._sim_lock = threading.Lock()
    broker._real_lock = threading.Lock()
    broker._exit_clock = clock or Clock()
    return broker


def test_history_deal_fill_price_is_quantity_weighted(tmp_path):
    ctx = Context()
    ctx.history_deal_list_query = lambda **kwargs: (
        0,
        pd.DataFrame([
            {"order_id": "42", "price": 100.0, "qty": 2.0},
            {"order_id": "42", "price": 103.0, "qty": 1.0},
            {"order_id": "other", "price": 999.0, "qty": 100.0},
        ]),
    )
    broker = adapter(tmp_path, ctx)

    assert broker._query_fill(ctx, "42", "SIMULATE", threading.Lock()) == (101.0, 3.0)


def close(broker, **kwargs):
    return broker.close_position('US.TEST', 10, 'LONG',
                                 exit_owner=kwargs.pop('exit_owner', owner()),
                                 exit_decision={'reason': 'SL', 'reference_price': 100, 'metadata': {}},
                                 **kwargs)


def test_cancel_rejection_pending_must_not_submit_market(tmp_path, monkeypatch):
    broker = adapter(tmp_path)
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    # Also runs against the original method to reproduce its unsafe second order.
    import inspect
    if 'exit_owner' in inspect.signature(broker.close_position).parameters:
        result = close(broker, limit_price=100, limit_timeout_s=0)
    else:
        result = broker.close_position('US.TEST', 10, 'LONG', limit_price=100, limit_timeout_s=0)
    assert len(broker._sim_ctx.orders) == 1
    assert not result['success']


def test_partial_cancel_terminal_only_remainder_weighted_fill(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.cancel_status = {'order_status': 'CANCELLED_PART', 'dealt_qty': 4,
                                     'dealt_avg_price': 100}
    broker._sim_ctx.fill_market = True
    result = close(broker, limit_price=100, limit_timeout_s=0)
    assert result['success'] and result['order_filled']
    assert [order['qty'] for order in broker._sim_ctx.orders] == [10, 6]
    assert result['dealt_qty'] == 10
    assert result['filled_price'] == 106
    assert result['price_source'] == 'broker_weighted_deal'
    assert result['exit_state'] == 'FILLED_UNBOOKED'
    persisted = broker.pending_closes()[0]
    assert persisted['result'] == result
    assert persisted['decision'] == {'reason': 'SL', 'reference_price': 100, 'metadata': {}}


def test_cancel_rejection_racing_full_fill_never_sends_second_leg(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.cancel_status = {'order_status': 'FILLED_ALL', 'dealt_qty': 10,
                                     'dealt_avg_price': 101}
    result = close(broker, limit_price=100, limit_timeout_s=0)
    assert result['success'] and result['filled_price'] == 101
    assert result['price_source'] == 'broker_limit_deal'
    assert len(broker._sim_ctx.orders) == 1


@pytest.mark.parametrize('status', ['SUBMITTED', 'CANCELLING_PART', 'CANCELLING_ALL', 'UNKNOWN',
                                    'FAKE_FILLED_ALL', 'FAKE_CANCELLED_PART'])
def test_only_exact_terminal_status_allows_replacement(tmp_path, status):
    broker = adapter(tmp_path)
    broker._sim_ctx.cancel_status = {'order_status': status, 'dealt_qty': 4, 'dealt_avg_price': 100}
    first = close(broker, limit_price=100, limit_timeout_s=0)
    second = close(broker, limit_price=100, limit_timeout_s=0)
    assert not first['success'] and not second['success']
    assert len(broker._sim_ctx.orders) == 1
    assert 'broker_no_position' not in second['error'] and 'insufficient' not in second['error']


@pytest.mark.parametrize('qty', [None, float('nan'), float('inf'), -1, True, 'bad', 11])
def test_invalid_terminal_cumulative_quantity_is_not_zero(tmp_path, qty):
    broker = adapter(tmp_path)
    broker._sim_ctx.cancel_status = {'order_status': 'CANCELLED_PART', 'dealt_qty': qty,
                                     'dealt_avg_price': 100}
    result = close(broker, limit_price=100, limit_timeout_s=0)
    assert not result['success']
    assert len(broker._sim_ctx.orders) == 1


def test_filled_status_without_known_full_quantity_is_held(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker)
    broker._sim_ctx.rows['1'].update(order_status='FILLED_ALL', dealt_qty=None, dealt_avg_price=100)
    result = broker.resume_close(first['exit_intent_id'])
    assert not result['success'] and not result['order_filled']
    assert len(broker._sim_ctx.orders) == 1


@pytest.mark.parametrize('price', [None, 0, float('nan'), float('inf'), -10, True, 'bad'])
def test_filled_without_real_price_awaits_evidence(tmp_path, price):
    broker = adapter(tmp_path)
    first = close(broker)
    broker._sim_ctx.rows['1'].update(order_status='FILLED_ALL', dealt_qty=10, dealt_avg_price=price)
    result = broker.resume_close(first['exit_intent_id'])
    assert not result['success'] and not result['order_filled']
    assert result['exit_state'] == 'HELD'
    broker._sim_ctx.rows['1']['dealt_avg_price'] = 102
    assert broker.resume_close(first['exit_intent_id'], allow_submit=False)['filled_price'] == 102
    assert len(broker._sim_ctx.orders) == 1


def test_partial_limit_missing_price_recovers_without_more_orders(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.cancel_status = {'order_status': 'CANCELLED_PART', 'dealt_qty': 4,
                                     'dealt_avg_price': 0}
    broker._sim_ctx.fill_market = True
    first = close(broker, limit_price=100, limit_timeout_s=0)
    assert not first['success']
    broker._sim_ctx.rows['1']['dealt_avg_price'] = 100
    result = broker.resume_close(first['exit_intent_id'])
    assert result['success'] and result['filled_price'] == 106
    assert len(broker._sim_ctx.orders) == 2


def test_market_timeout_repeated_call_and_rebuilt_adapter_do_not_reissue(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker)
    assert not first['success']
    old_deadline = broker._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]['deadline']
    rebuilt = adapter(tmp_path, broker._sim_ctx, broker._exit_clock)
    second = close(rebuilt)
    assert first['exit_intent_id'] == second['exit_intent_id']
    assert len(broker._sim_ctx.orders) == 1
    assert rebuilt._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]['deadline'] == old_deadline
    broker._sim_ctx.rows['1'].update(order_status='FILLED_ALL', dealt_qty=10, dealt_avg_price=105)
    result = rebuilt.resume_close(first['exit_intent_id'])
    assert result['success']


def test_expired_limit_resume_cancel_query_then_remainder(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker, limit_price=100, limit_timeout_s=8)
    deadline = broker._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]['deadline']
    assert deadline == 1800000008
    broker._sim_ctx.cancel_status = {'order_status': 'CANCELLED_PART', 'dealt_qty': 4,
                                     'dealt_avg_price': 100}
    broker._sim_ctx.fill_market = True
    rebuilt = adapter(tmp_path, broker._sim_ctx, broker._exit_clock)
    result = rebuilt.resume_close(first['exit_intent_id'])
    assert result['success'] and result['filled_price'] == 106
    assert broker._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]['deadline'] == deadline


def test_query_only_outside_session_never_cancels_or_submits(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker, limit_price=100, limit_timeout_s=0)
    cancels = list(broker._sim_ctx.cancel_calls)
    broker._sim_ctx.cancel_status = {'order_status': 'CANCELLED_ALL', 'dealt_qty': 0}
    assert not broker.resume_close(first['exit_intent_id'], allow_submit=False)['success']
    assert broker._sim_ctx.cancel_calls == cancels
    assert len(broker._sim_ctx.orders) == 1
    broker._sim_ctx.rows['1'].update(order_status='CANCELLED_ALL', dealt_qty=0)
    broker.resume_close(first['exit_intent_id'], allow_submit=False)
    assert len(broker._sim_ctx.orders) == 1


def test_durable_close_defaults_to_explicit_rth_session(tmp_path):
    from futu import Session

    broker = adapter(tmp_path)
    close(broker, limit_price=100, limit_timeout_s=0)
    assert broker._sim_ctx.orders[0]['session'] == Session.RTH
    assert broker.pending_closes()[0]['session'] == 'RTH'


def test_eth_close_requires_sim_limit_and_persists_session(tmp_path):
    from futu import Session

    broker = adapter(tmp_path)
    result = close(broker, session='ETH', limit_price=100, limit_timeout_s=0)
    assert not result['success']
    assert broker._sim_ctx.orders[0]['session'] == Session.ETH
    assert broker.pending_closes()[0]['session'] == 'ETH'


def test_eth_close_preserves_extended_hours_broker_remark_prefix(tmp_path):
    broker = adapter(tmp_path)
    result = broker.close_position(
        'US.TEST', 10, 'LONG', limit_price=100, limit_timeout_s=0, session='ETH',
        exit_owner=owner(),
        exit_decision={
            'reason': 'BREAKEVEN_LOCK',
            'reference_price': 100,
            'metadata': {'broker_remark_prefix': 'ext_exit_breakeven_lock'},
        })
    legs = broker._exit_lifecycle().ledger.legs(result['exit_intent_id'])
    assert legs[0]['remark'].startswith('ext_exit_breakeven_lock')


def test_eth_partial_terminal_fill_never_sends_market_remainder(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.cancel_status = {'order_status': 'CANCELLED_PART', 'dealt_qty': 4,
                                      'dealt_avg_price': 100}
    result = close(broker, session='ETH', limit_price=100, limit_timeout_s=0)
    assert not result['success'] and not result['order_filled']
    assert result['exit_state'] == 'HELD'
    assert result['error'] == 'exit_eth_partial_fill_held'
    assert result['dealt_qty'] == 4
    assert [order['qty'] for order in broker._sim_ctx.orders] == [10]


@pytest.mark.parametrize('target_env,limit_price,expected_error', [
    ('real', 100, 'exit_eth_requires_sim_limit'),
    ('sim', 0, 'exit_eth_requires_sim_limit'),
])
def test_eth_close_rejects_real_or_market_semantics(tmp_path, target_env, limit_price, expected_error):
    broker = adapter(tmp_path)
    result = close(broker, target_env=target_env, session='ETH', limit_price=limit_price,
                   limit_timeout_s=0)
    assert not result['success']
    assert result['error'] == expected_error
    assert not broker._sim_ctx.orders
    assert not broker.pending_closes()


def test_existing_exit_ledger_migrates_session_to_rth(tmp_path):
    from app.execution.exit_lifecycle import ExitLedger

    path = tmp_path / 'legacy-exits.db'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE exit_intents (
            intent_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            target_env TEXT NOT NULL, symbol TEXT NOT NULL, close_key TEXT NOT NULL,
            direction TEXT NOT NULL, quantity REAL NOT NULL, owner_json TEXT NOT NULL,
            decision_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
            result_json TEXT, receipt_json TEXT, last_error TEXT NOT NULL DEFAULT '',
            limit_price REAL NOT NULL, limit_timeout_s REAL NOT NULL,
            broker_qty_before REAL, version INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(account_id, target_env, close_key))''')
    ExitLedger(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(exit_intents)')}
        default = db.execute(
            "SELECT dflt_value FROM pragma_table_info('exit_intents') WHERE name='session'"
        ).fetchone()[0]
    assert 'session' in columns
    assert default == "'RTH'"


def test_terminal_market_shortfall_is_held_not_replaced(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker)
    broker._sim_ctx.rows['1'].update(order_status='CANCELLED_PART', dealt_qty=4, dealt_avg_price=100)
    result = broker.resume_close(first['exit_intent_id'])
    assert result['exit_state'] == 'HELD'
    assert result['dealt_qty'] == 4
    assert len(broker._sim_ctx.orders) == 1


def test_unknown_submit_empty_queries_never_unlock_then_exact_history_recovers(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.lose_submit = True
    broker._sim_ctx.hide_current = True
    first = close(broker)
    assert first['exit_state'] == 'HELD'
    for _ in range(3):
        assert not close(adapter(tmp_path, broker._sim_ctx, broker._exit_clock))['success']
    leg = broker._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]
    assert leg['state'] == 'PREPARED' and leg['order_id'] is None and leg['dealt_qty'] is None
    broker._sim_ctx.history = [dict(broker._sim_ctx.rows['1'], order_status='FILLED_ALL',
                                    dealt_qty=10, dealt_avg_price=104)]
    result = broker.resume_close(first['exit_intent_id'])
    assert result['success'] and result['filled_price'] == 104
    assert len(broker._sim_ctx.orders) == 1


def test_unknown_submit_current_exact_remark_recovers(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.lose_submit = True
    broker._sim_ctx.fill_market = True
    assert close(broker)['success']
    assert len(broker._sim_ctx.orders) == 1


def test_unknown_submit_ambiguous_remark_and_prefix_are_not_matches(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.lose_submit = True
    broker._sim_ctx.hide_current = True
    first = close(broker)
    row = dict(broker._sim_ctx.rows['1'], order_status='FILLED_ALL', dealt_qty=10, dealt_avg_price=100)
    broker._sim_ctx.history = [dict(row, remark=row['remark'] + '-not-exact')]
    assert not broker.resume_close(first['exit_intent_id'])['success']
    broker._sim_ctx.history = [row, dict(row, order_id='duplicate')]
    assert not broker.resume_close(first['exit_intent_id'])['success']
    assert len(broker._sim_ctx.orders) == 1


@pytest.mark.parametrize('field,value', [('code', 'US.OTHER'), ('trd_side', 'BUY'), ('qty', 9),
                                        ('acc_id', 999), ('trd_env', 'REAL')])
@pytest.mark.parametrize('history', [False, True])
def test_exact_order_id_does_not_bypass_identity_validation(tmp_path, field, value, history):
    broker = adapter(tmp_path)
    first = close(broker)
    row = dict(broker._sim_ctx.rows['1'], order_status='FILLED_ALL', dealt_qty=10, dealt_avg_price=100)
    row[field] = value
    if history:
        broker._sim_ctx.hide_current = True
        broker._sim_ctx.history = [row]
    else:
        broker._sim_ctx.rows['1'] = row
    assert not broker.resume_close(first['exit_intent_id'])['success']
    assert len(broker._sim_ctx.orders) == 1


def test_owned_quantity_does_not_expand_to_account_position(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.position_qty = 15
    broker._sim_ctx.fill_market = True
    result = close(broker)
    assert result['success']
    assert result['price_source'] == 'broker_deal'
    assert broker._sim_ctx.orders[0]['qty'] == 10
    assert result['broker_qty_before'] == 15 and result['broker_qty_after'] == 5


@pytest.mark.parametrize('qty', [0, 4, -10, None, float('nan')])
def test_position_discrepancy_holds_without_ghost_close(tmp_path, qty):
    broker = adapter(tmp_path)
    broker._sim_ctx.position_qty = qty
    result = close(broker)
    assert not result['success']
    assert result['exit_state'] == 'HELD'
    assert len(broker._sim_ctx.orders) == 0
    assert 'broker_no_position' not in result['error']


def test_legacy_active_exit_blocks_without_cancelling_other_orders(tmp_path, caplog):
    broker = adapter(tmp_path)
    broker._sim_ctx.rows['legacy'] = {'code': 'US.TEST', 'qty': 10, 'trd_side': 'SELL',
                                     'order_status': 'SUBMITTED', 'order_id': 'legacy'}
    with caplog.at_level(logging.WARNING, logger='app.execution.exit_lifecycle'):
        result = close(broker)
    assert result['error'] == 'exit_unowned_active_order'
    assert not broker._sim_ctx.orders and not broker._sim_ctx.cancel_calls
    assert 'order_id=legacy' in caplog.text


def test_result_pending_booking_replay_and_new_owner_isolation(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    first = close(broker)
    replay = close(broker)
    assert replay == first and len(broker._sim_ctx.orders) == 1
    blocked = close(broker, exit_owner=owner('entry-2', trade_id=2))
    assert blocked['exit_state'] == 'BLOCKED' and not blocked['success']
    assert blocked['futu_order_id'] == '' and blocked['exit_intent_id'] == ''
    receipt = {'exit_intent_id': first['exit_intent_id'], 'trade_id': 1}
    assert broker.acknowledge_close(first['exit_intent_id'], receipt)
    assert broker.acknowledge_close(first['exit_intent_id'], receipt)
    assert not broker.acknowledge_close(first['exit_intent_id'], {'trade_id': 2})
    assert broker.pending_closes() == []
    assert close(broker)['exit_state'] == 'BOOKED'
    fresh = close(broker, exit_owner=owner('entry-2', trade_id=2))
    assert fresh['exit_intent_id'] != first['exit_intent_id']
    assert len(broker._sim_ctx.orders) == 2


def test_compensation_same_entry_blocks_trade_owner_and_recovers_without_trade_db(tmp_path):
    broker = adapter(tmp_path)
    compensation = owner(kind='compensation', trade_id=None, execution_attempt=3)
    first = close(broker, exit_owner=compensation)
    assert not first['success']
    assert close(broker)['exit_state'] == 'BLOCKED'
    broker._sim_ctx.rows['1'].update(order_status='FILLED_ALL', dealt_qty=10, dealt_avg_price=100)
    result = broker.resume_close(first['exit_intent_id'])
    assert result['success'] and result['owner']['kind'] == 'compensation'


def test_account_and_environment_are_isolated_and_pending_list_is_global(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker)
    other_account = adapter(tmp_path)
    other_account.sim_account_id = '43'
    second = close(other_account)
    real = adapter(tmp_path)
    real._real_ctx = Context()
    third = close(real, target_env='real')
    assert len(broker.pending_closes()) == 3
    assert not other_account.resume_close(first['exit_intent_id'])['success']
    assert first['exit_intent_id'] != second['exit_intent_id'] != third['exit_intent_id']
    assert real._real_ctx.orders[0]['acc_id'] == 99
    assert str(real._real_ctx.orders[0]['trd_env']) == 'REAL'


def test_completed_replay_cannot_leak_to_wrong_account_and_needs_no_context(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    first = close(broker)
    broker._sim_ctx = None
    assert broker.resume_close(first['exit_intent_id'], allow_submit=False)['success']
    broker.sim_account_id = '43'
    assert not broker.resume_close(first['exit_intent_id'])['success']


def test_cumulative_evidence_monotonic_and_terminal_never_reopens(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker, limit_price=100, limit_timeout_s=0)
    ctx = broker._sim_ctx
    ctx.rows['1'].update(order_status='CANCELLED_PART', dealt_qty=4, dealt_avg_price=100)
    broker.resume_close(first['exit_intent_id'], allow_submit=False)
    ctx.rows['1'].update(order_status='SUBMITTED', dealt_qty=0, dealt_avg_price=0)
    broker.resume_close(first['exit_intent_id'], allow_submit=False)
    leg = broker._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]
    assert leg['state'] == 'TERMINAL' and leg['dealt_qty'] == 4 and leg['dealt_amount'] == 400
    ctx.rows['1'].update(order_status='SUBMITTED', dealt_qty=4, dealt_avg_price=100)
    broker.resume_close(first['exit_intent_id'], allow_submit=False)
    assert broker._exit_lifecycle().ledger.legs(first['exit_intent_id'])[0]['state'] == 'TERMINAL'


def test_before_send_ledger_commit_and_no_sql_writer_lock_during_sdk(tmp_path):
    broker = adapter(tmp_path)
    original = broker._sim_ctx.place_order

    def place(**kwargs):
        with sqlite3.connect(broker._exit_ledger_path, timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,remark,dealt_qty,deadline FROM exit_legs').fetchone()
            assert row == ('PREPARED', kwargs['remark'], None, 1800000030.0)
        return original(**kwargs)

    broker._sim_ctx.place_order = place
    close(broker)
    assert len(broker._sim_ctx.orders) == 1


def test_write_failure_before_reservation_forbids_broker_order(tmp_path, monkeypatch):
    from app.execution.exit_lifecycle import ExitLedger
    broker = adapter(tmp_path)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected disk failure')

    monkeypatch.setattr(ExitLedger, 'reserve_leg', fail)
    assert not close(broker)['success']
    assert not broker._sim_ctx.orders


def test_write_failure_after_send_recovers_without_resend(tmp_path, monkeypatch):
    from app.execution.exit_lifecycle import ExitLedger
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    original = ExitLedger.attach_order

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected disk failure')

    monkeypatch.setattr(ExitLedger, 'attach_order', fail)
    first = close(broker)
    assert not first['success'] and len(broker._sim_ctx.orders) == 1
    monkeypatch.setattr(ExitLedger, 'attach_order', original)
    assert broker.resume_close(first['exit_intent_id'])['success']
    assert len(broker._sim_ctx.orders) == 1


def _process_submit(path, barrier, counter, results, violations):
    broker = adapter(Path(path))
    original = broker._sim_ctx.place_order

    def place(**kwargs):
        with counter.get_lock():
            counter.value += 1
        return original(**kwargs)

    broker._sim_ctx.place_order = place
    barrier.wait(timeout=10)
    results.put((close(broker), violations))


def test_two_processes_only_one_durable_submitter(tmp_path, isolated):
    import multiprocessing
    from app.execution.exit_lifecycle import ExitLedger
    ExitLedger(tmp_path / 'exits.db')
    context = multiprocessing.get_context('fork')
    barrier, counter, results = context.Barrier(2), context.Value('i', 0), context.Queue()
    processes = [context.Process(target=_process_submit, args=(str(tmp_path), barrier, counter, results, isolated))
                 for _ in range(2)]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert counter.value == 1
    assert all(not result['success'] and violations == [] for result, violations in outcomes)
    assert len(ExitLedger(tmp_path / 'exits.db').list_unsettled()) == 1


def test_missing_identity_never_uses_legacy_unsafe_fallback(tmp_path):
    broker = adapter(tmp_path)
    result = broker.close_position('US.TEST', 10, 'LONG')
    assert result['error'] == 'exit_identity_required'
    assert not broker._exit_ledger_path.exists() and not broker._sim_ctx.orders


def test_path_contract_constructor_lazy_and_state_units(tmp_path, monkeypatch):
    from app.core import paths
    from app.execution.exit_lifecycle import INTENT_STATES, LEG_STATES
    monkeypatch.setattr(paths, 'get_data_dir', lambda: tmp_path)
    assert paths.get_exit_ledger_db() == tmp_path / 'playbook_exits.db'
    broker = DualBrokerAdapter(exit_ledger_path=tmp_path / 'not-created.db')
    assert not Path(broker._exit_ledger_path).exists()
    assert INTENT_STATES == {'PENDING', 'HELD', 'FILLED_UNBOOKED', 'BOOKED'}
    assert LEG_STATES == {'PREPARED', 'ACTIVE', 'CANCEL_REQUESTED', 'TERMINAL'}


def test_crash_before_send_remains_durably_claimed(tmp_path, monkeypatch):
    from app.execution.exit_lifecycle import ExitLifecycle
    broker = adapter(tmp_path)
    original = ExitLifecycle.send
    monkeypatch.setattr(ExitLifecycle, 'send', lambda *args, **kwargs: None)
    first = close(broker)
    assert first['exit_state'] == 'HELD'
    monkeypatch.setattr(ExitLifecycle, 'send', original)
    for _ in range(2):
        result = close(adapter(tmp_path, broker._sim_ctx, broker._exit_clock))
        assert result['exit_intent_id'] == first['exit_intent_id']
        assert not result['success']
    assert not broker._sim_ctx.orders


def test_pending_enumeration_has_no_age_or_first_25_starvation(tmp_path):
    from app.execution.exit_lifecycle import ExitLedger
    ledger = ExitLedger(tmp_path / 'exits.db')
    for index in range(31):
        ledger.request(account_id='42', target_env='sim', symbol=f'US.T{index}', direction='LONG',
            quantity=10, owner=owner(f'entry-{index}', symbol=f'US.T{index}'),
            decision={'reason': 'SL', 'metadata': {}, 'reference_price': 100},
            limit_price=0, limit_timeout_s=8, now=1700000000 + index)
    broker = adapter(tmp_path)
    broker._sim_ctx = None
    pending = broker.pending_closes()
    assert len(pending) == 31
    assert all(row['created_at'] < broker._exit_clock.time() - 86400 for row in pending)


@pytest.mark.parametrize('history', [True, False])
def test_unknown_submit_requires_successful_current_and_history_queries(tmp_path, history):
    broker = adapter(tmp_path)
    broker._sim_ctx.lose_submit = True
    broker._sim_ctx.hide_current = True
    first = close(broker)
    broker._sim_ctx.hide_current = False
    broker._sim_ctx.history = [dict(broker._sim_ctx.rows['1'], order_status='FILLED_ALL',
                                    dealt_qty=10, dealt_avg_price=100)]
    method = 'history_order_list_query' if history else 'order_list_query'
    setattr(broker._sim_ctx, method, lambda **kwargs: (-1, 'query unavailable'))
    assert not broker.resume_close(first['exit_intent_id'])['success']
    assert len(broker._sim_ctx.orders) == 1


def test_cancel_followup_empty_query_cannot_authorize_market(tmp_path):
    broker = adapter(tmp_path)
    original = broker._sim_ctx.modify_order

    def cancel(**kwargs):
        result = original(**kwargs)
        broker._sim_ctx.hide_current = True
        return result

    broker._sim_ctx.modify_order = cancel
    result = close(broker, limit_price=100, limit_timeout_s=0)
    assert not result['success']
    assert len(broker._sim_ctx.orders) == 1


def test_account_mismatch_cannot_acknowledge_other_account_fill(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    first = close(broker)
    broker.sim_account_id = '43'
    assert not broker.acknowledge_close(first['exit_intent_id'], {'trade_id': 1})
    assert broker.pending_closes()[0]['state'] == 'FILLED_UNBOOKED'


def test_query_valueerror_cannot_become_legacy_ghost_close(tmp_path):
    broker = adapter(tmp_path)

    def query(**kwargs):
        raise ValueError('broker_no_position insufficient')

    broker._sim_ctx.position_list_query = query
    result = close(broker)
    assert result['error'] == 'exit_evidence_invalid'
    assert not broker._sim_ctx.orders


def test_positive_limit_rounding_to_zero_cannot_turn_into_market(tmp_path):
    broker = adapter(tmp_path)
    assert not close(broker, limit_price=0.001)['success']
    assert not broker._sim_ctx.orders


def test_short_owned_exit_uses_buy_and_preserves_other_quantity(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.position_qty = -15
    broker._sim_ctx.fill_market = True
    result = broker.close_position('US.TEST', 10, 'SHORT', exit_owner=owner(direction='SHORT'),
        exit_decision={'reason': 'SL', 'metadata': {}, 'reference_price': 100})
    assert result['success'] and result['broker_qty_after'] == 5
    assert broker._sim_ctx.orders[0]['trd_side'] == 'BUY'
    assert broker._sim_ctx.orders[0]['qty'] == 10


def test_fallback_key_replays_only_immutable_original_trade(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    owned = owner(entry_order_id=None, close_key='trade:/tmp/owned.db:1:2026-09-10 10:00:00')
    first = close(broker, exit_owner=owned)
    assert first['success']
    assert close(broker, exit_owner=owned)['exit_intent_id'] == first['exit_intent_id']
    changed = dict(owned, entry_time='2026-09-11 10:00:00')
    assert not close(broker, exit_owner=changed)['success']
    assert len(broker._sim_ctx.orders) == 1


def test_recovered_order_id_cas_cannot_record_competing_order_fill(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker)
    ledger = broker._exit_lifecycle().ledger
    leg = ledger.legs(first['exit_intent_id'])[0]
    with pytest.raises(ValueError, match='exit_order_id_conflict'):
        ledger.attach_order(leg, 'other', broker._exit_clock.time())
    wrong = dict(broker._sim_ctx.rows['1'], order_id='other', order_status='FILLED_ALL',
                 dealt_qty=10, dealt_avg_price=100)
    with pytest.raises(ValueError, match='exit_order_id_conflict'):
        ledger.observe(leg, wrong, broker._exit_clock.time())
    assert ledger.legs(first['exit_intent_id'])[0]['dealt_qty'] == 0


def test_market_remainder_rechecks_position_direction_and_quantity(tmp_path):
    broker = adapter(tmp_path)
    first = close(broker, limit_price=100, limit_timeout_s=0)
    broker._sim_ctx.rows['1'].update(order_status='CANCELLED_PART', dealt_qty=4, dealt_avg_price=100)
    broker._sim_ctx.position_qty = 3
    result = broker.resume_close(first['exit_intent_id'])
    assert not result['success'] and len(broker._sim_ctx.orders) == 1
    broker._sim_ctx.position_qty = -6
    assert not broker.resume_close(first['exit_intent_id'])['success']
    assert len(broker._sim_ctx.orders) == 1
    broker._sim_ctx.position_qty = 6
    broker._sim_ctx.fill_market = True
    assert broker.resume_close(first['exit_intent_id'])['success']
    assert [row['qty'] for row in broker._sim_ctx.orders] == [10, 6]


def test_confirmation_timestamp_is_durable_not_booking_retry_time(tmp_path):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    first = close(broker)
    assert first['confirmed_at'] == 1800000000.0
    broker._exit_clock.now += 86400
    replay = broker.resume_close(first['exit_intent_id'], allow_submit=False)
    assert replay['confirmed_at'] == first['confirmed_at']
    assert broker.pending_closes()[0]['result']['confirmed_at'] == first['confirmed_at']


@pytest.mark.parametrize('field,value', [('symbol', 'US.OTHER'), ('symbol', 'us.test'),
                                        ('symbol', None), ('direction', 'SHORT'),
                                        ('direction', 'long'), ('direction', None)])
def test_owner_symbol_direction_must_exactly_match_execution(tmp_path, field, value):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    result = close(broker, exit_owner=owner(**{field: value}))
    assert not result['success'] and result['exit_state'] == 'HELD'
    assert not broker._sim_ctx.orders
    assert broker.pending_closes() == []


@pytest.mark.parametrize('field', ['symbol', 'direction'])
def test_missing_owner_execution_identity_is_held_before_send(tmp_path, field):
    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    owned = owner()
    del owned[field]
    result = close(broker, exit_owner=owned)
    assert not result['success'] and result['exit_state'] == 'HELD'
    assert not broker._sim_ctx.orders
    assert broker.pending_closes() == []


@pytest.mark.parametrize('snapshot_column', ['owner_json', 'decision_json', 'result_json', 'receipt_json'])
@pytest.mark.parametrize('raw_snapshot', ['{broken', '[]'], ids=['malformed-json', 'non-dict-json'])
@pytest.mark.parametrize('bad_index', [0, 1, 2], ids=['first', 'middle', 'last'])
def test_corrupt_snapshot_does_not_starve_healthy_exits_or_release_evidence(
        tmp_path, monkeypatch, snapshot_column, raw_snapshot, bad_index):
    from app.execution.exit_lifecycle import ExitLedger

    broker = adapter(tmp_path)
    broker._sim_ctx.fill_market = True
    ledger = broker._exit_lifecycle().ledger
    now = broker._exit_clock.time()
    intents = []
    for index in range(3):
        symbol = f'US.SNAPSHOT{index}'
        intent, blocked_result = ledger.request(
            account_id='42', target_env='sim', symbol=symbol, direction='LONG', quantity=10,
            owner=owner(f'snapshot-entry-{index}', symbol=symbol),
            decision={'reason': 'SL', 'reference_price': 100, 'metadata': {}},
            limit_price=0, limit_timeout_s=8, now=now + index)
        assert blocked_result is None
        intents.append(intent)
    bad = intents[bad_index]
    bad_id = bad['intent_id']
    leg = ledger.reserve_leg(bad, 'LIMIT', 10, 100, now - 1, now, broker_qty_before=10)
    ledger.attach_order(leg, 'corrupt-order', now)
    broker._sim_ctx.rows['corrupt-order'] = {
        'order_id': 'corrupt-order', 'code': bad['symbol'], 'trd_side': 'SELL',
        'qty': 10, 'order_status': 'SUBMITTED', 'dealt_qty': 0, 'dealt_avg_price': 0,
        'acc_id': 42, 'trd_env': 'SIMULATE', 'remark': leg['remark'],
    }
    positions = [{'code': intent['symbol'], 'qty': 10, 'position_side': 'LONG'} for intent in intents]
    monkeypatch.setattr(broker._sim_ctx, 'position_list_query',
                        lambda **kwargs: (0, pd.DataFrame(positions)))
    with sqlite3.connect(broker._exit_ledger_path) as db:
        db.execute(f'UPDATE exit_intents SET {snapshot_column}=? WHERE intent_id=?',
                   (raw_snapshot, bad_id))
        raw_intent_before = db.execute('SELECT * FROM exit_intents WHERE intent_id=?',
                                       (bad_id,)).fetchone()
        raw_legs_before = db.execute('SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence',
                                     (bad_id,)).fetchall()

    routes = []
    original_route = broker._ctx_and_acc

    def route(target_env):
        routes.append(target_env)
        return original_route(target_env)

    ack_attempts = []
    original_ack = ExitLedger.ack_booked

    def acknowledge(store, intent_id, receipt, timestamp):
        ack_attempts.append(intent_id)
        return original_ack(store, intent_id, receipt, timestamp)

    monkeypatch.setattr(broker, '_ctx_and_acc', route)
    monkeypatch.setattr(ExitLedger, 'ack_booked', acknowledge)
    pending = broker.pending_closes()
    assert [item['intent_id'] for item in pending] == [item['intent_id'] for item in intents]
    descriptor_keys = ('intent_id', 'account_id', 'target_env', 'symbol', 'state', 'created_at')
    assert pending[bad_index] == {key: bad[key] for key in descriptor_keys}
    assert not routes

    outcomes = {}
    for item in pending:
        routes_before = list(routes)
        orders_before = list(broker._sim_ctx.orders)
        cancels_before = list(broker._sim_ctx.cancel_calls)
        result = broker.resume_close(item['intent_id'])
        outcomes[item['intent_id']] = result
        if item['intent_id'] == bad_id:
            assert result['exit_intent_id'] == bad_id
            assert not result['success'] and not result['order_filled']
            assert result['exit_state'] == 'HELD'
            assert routes == routes_before
            assert broker._sim_ctx.orders == orders_before
            assert broker._sim_ctx.cancel_calls == cancels_before
        else:
            assert result['success'] and result['order_filled']
            assert result['owner']['symbol'] == item['symbol']
            assert broker.acknowledge_close(item['intent_id'], {'exit_intent_id': item['intent_id']})

    healthy_ids = [intent['intent_id'] for intent in intents if intent['intent_id'] != bad_id]
    assert all(outcomes[intent_id]['success'] for intent_id in healthy_ids)
    assert ack_attempts == healthy_ids
    assert not broker.acknowledge_close(bad_id, {'exit_intent_id': bad_id})
    assert ack_attempts == healthy_ids
    routes_before = list(routes)
    blocked = broker.close_position(
        bad['symbol'], 10, 'LONG', exit_owner=owner('replacement-entry', symbol=bad['symbol']),
        exit_decision={'reason': 'SL', 'reference_price': 100, 'metadata': {}})
    assert not blocked['success'] and blocked['exit_state'] == 'BLOCKED'
    assert blocked['error'] == 'exit_symbol_busy'
    assert routes == routes_before
    assert [order['code'] for order in broker._sim_ctx.orders] == [
        intent['symbol'] for intent in intents if intent['intent_id'] != bad_id]
    assert not broker._sim_ctx.cancel_calls
    assert broker.pending_closes() == [{key: bad[key] for key in descriptor_keys}]
    with sqlite3.connect(broker._exit_ledger_path) as db:
        assert db.execute('SELECT * FROM exit_intents WHERE intent_id=?',
                          (bad_id,)).fetchone() == raw_intent_before
        assert db.execute('SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence',
                          (bad_id,)).fetchall() == raw_legs_before


@pytest.mark.parametrize('session,metadata,expected_prefix,visible_to_reconcile', [
    # ① ETH + 顶层键 (RFC-107 §2.2 修后形态): remark 落 ext_exit_*, 对账环/孤儿监测可见
    ('ETH',
     {'broker_remark_prefix': 'ext_exit_SL_BREAKEVEN',
      'extended_hours_exit': {'session': 'premarket',
                              'broker_remark_prefix': 'ext_exit_SL_BREAKEVEN'}},
     'ext_exit_SL_BREAKEVEN', True),
    # ② ETH + 仅嵌套键 (修前形态, MU 9/25 04:04 案的实物): reserve_leg 读不到 ⇒ 回退 pbe_
    ('ETH',
     {'extended_hours_exit': {'session': 'premarket',
                              'broker_remark_prefix': 'ext_exit_SL_BREAKEVEN'}},
     'pbe_', False),
    # ③ RTH + 顶层键: session 前置条件未动 ⇒ 仍 pbe_ (证明修复未扩大 RTH 行为)
    ('RTH', {'broker_remark_prefix': 'ext_exit_SL_BREAKEVEN'}, 'pbe_', False),
], ids=['eth_top_level_fixed', 'eth_nested_only_pre_fix', 'rth_top_level_unchanged'])
def test_reserve_leg_remark_prefix_reads_top_level_metadata_only(
        tmp_path, session, metadata, expected_prefix, visible_to_reconcile):
    """RFC-107 §2.2 契约: remark 前缀**只**认 decision.metadata 的顶层 broker_remark_prefix。

    为什么值得钉 (2026-09-27 实测): 修前 executor 把前缀写在嵌套
    metadata["extended_hours_exit"]["broker_remark_prefix"], reserve_leg 读不到 ⇒ ETH 单
    实际落 pbe_*。后果不是纯观测字段: reconcile_extended_hours_exits.py 与
    orphan_ext_order_watch.py 均按 remark.startswith("ext_exit_") 过滤 ⇒ ETH 单对
    对账环与孤儿单监测**失明** (MU 9/25 04:04 盘前 BE 案实物: 顶层键缺失, 券商单 remark=pbe_)。

    生产侧 (executor 真写顶层键) 钉在
    tests/test_extended_hours_order_execution.py::test_exit_full_fill_updates_records_and_removes_position。
    本测试钉契约侧 (reserve_leg 真读它), 两侧合起来才是闭环: 只钉一侧时,
    另一侧回归仍会静默回到失明态。
    """
    from app.execution.exit_lifecycle import ExitLedger

    ledger = ExitLedger(tmp_path / 'exits.db')
    now = 1700000000
    symbol = f'US.REMARK{session}'
    intent, blocked = ledger.request(
        account_id='42', target_env='sim', symbol=symbol, direction='LONG', quantity=10,
        owner=owner(f'remark-{session}', symbol=symbol),
        decision={'reason': 'SL_BREAKEVEN', 'reference_price': 100, 'metadata': metadata},
        limit_price=101.5, limit_timeout_s=20, now=now, session=session)
    assert blocked is None

    leg = ledger.reserve_leg(intent, 'LIMIT', 10, 101.5, now + 20, now, broker_qty_before=10)
    assert leg is not None
    remark = str(leg['remark'])
    assert remark.startswith(expected_prefix), (
        f'{session} + metadata={sorted(metadata)} ⇒ remark={remark!r}, '
        f'期望前缀 {expected_prefix!r}')
    # 对账环可见性 = 两个消费者的实际过滤谓词 (startswith), 不是"有没有前缀"
    assert remark.startswith('ext_exit_') is visible_to_reconcile
