"""Durable Playbook exits. Quantities are shares, prices USD, times epoch seconds."""
import json
import logging
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

INTENT_STATES = frozenset({'PENDING', 'HELD', 'FILLED_UNBOOKED', 'BOOKED'})
LEG_STATES = frozenset({'PREPARED', 'ACTIVE', 'CANCEL_REQUESTED', 'TERMINAL'})
# Futu's CANCELLING_PART / CANCELLING_ALL are deliberately not terminal.
TERMINAL_STATUSES = frozenset({'FILLED_ALL', 'CANCELLED_ALL', 'CANCELLED_PART',
                               'FAILED', 'DISABLED', 'DELETED'})
ACTIVE_STATUSES = frozenset({'UNSUBMITTED', 'WAITING_SUBMIT', 'SUBMITTING', 'SUBMITTED',
                             'FILLED_PART', 'CANCELLING_PART', 'CANCELLING_ALL'})


def number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None


def token(value):
    return str(value).rsplit('.', 1)[-1].upper()


def json_text(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def held(error, *, intent=None, owner=None, decision=None, state='HELD'):
    return {'success': False, 'order_filled': False, 'error': error,
            'exit_intent_id': intent['intent_id'] if intent else '',
            'exit_state': state, 'owner': intent['owner'] if intent else owner,
            'decision': intent['decision'] if intent else decision,
            'futu_order_id': '', 'filled_price': 0.0, 'dealt_qty': 0.0,
            'price_source': 'exit_evidence_pending', 'broker_qty_before': None,
            'broker_qty_after': None}


class ExitLedger:
    def __init__(self, path):
        self.path = str(path)
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS exit_intents (
                    intent_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    target_env TEXT NOT NULL CHECK(target_env IN ('sim','real')),
                    symbol TEXT NOT NULL, close_key TEXT NOT NULL, direction TEXT NOT NULL,
                    quantity REAL NOT NULL CHECK(quantity > 0), owner_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                    result_json TEXT, receipt_json TEXT, last_error TEXT NOT NULL DEFAULT '',
                    session TEXT NOT NULL DEFAULT 'RTH' CHECK(session IN ('RTH','ETH')),
                    limit_price REAL NOT NULL, limit_timeout_s REAL NOT NULL,
                    broker_qty_before REAL, version INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(account_id, target_env, close_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS exit_active_symbol
                    ON exit_intents(account_id, target_env, symbol) WHERE state != 'BOOKED';
                CREATE TABLE IF NOT EXISTS exit_legs (
                    intent_id TEXT NOT NULL REFERENCES exit_intents(intent_id),
                    sequence INTEGER NOT NULL, kind TEXT NOT NULL, quantity REAL NOT NULL,
                    price REAL NOT NULL, remark TEXT NOT NULL UNIQUE, order_id TEXT,
                    state TEXT NOT NULL, broker_status TEXT NOT NULL DEFAULT '',
                    dealt_qty REAL, dealt_amount REAL, deadline REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(intent_id, sequence)
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(exit_intents)')}
            if 'session' not in columns:
                db.execute("ALTER TABLE exit_intents ADD COLUMN session TEXT NOT NULL DEFAULT 'RTH'")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        for name in ('owner', 'decision', 'result', 'receipt'):
            raw = value.pop(name + '_json')
            decoded = json.loads(raw) if raw else None
            if not isinstance(decoded, dict) and (decoded is not None or name in ('owner', 'decision')):
                raise ValueError(f'exit_{name}_snapshot_invalid')
            value[name] = decoded
        return value

    def get(self, intent_id):
        with self.transaction() as db:
            return self.decode(db.execute('SELECT * FROM exit_intents WHERE intent_id=?',
                                          (intent_id,)).fetchone())

    def legs(self, intent_id):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence', (intent_id,))]

    def list_unsettled(self):
        with self.transaction() as db:
            unsettled = []
            for row in db.execute(
                    "SELECT * FROM exit_intents WHERE state!='BOOKED' ORDER BY created_at,intent_id"):
                try:
                    unsettled.append(self.decode(row))
                except (ValueError, TypeError):
                    logger.exception('exit snapshot unreadable intent=%s account=%s env=%s symbol=%s',
                                     row['intent_id'], row['account_id'], row['target_env'], row['symbol'])
                    # Keep the symbol reserved; resume re-reads this row and fails without broker I/O.
                    unsettled.append({key: row[key] for key in
                                      ('intent_id', 'account_id', 'target_env', 'symbol', 'state', 'created_at')})
            return unsettled

    def request(self, *, account_id, target_env, symbol, direction, quantity, owner,
                decision, limit_price, limit_timeout_s, now, session='RTH'):
        owner = dict(owner) if isinstance(owner, dict) else {}
        quantity = number(quantity, positive=True)
        if (not quantity or quantity != number(owner.get('quantity'), positive=True)
                or target_env not in ('sim', 'real') or direction not in ('LONG', 'SHORT')
                or owner.get('direction') != direction or owner.get('symbol') != symbol
                or not symbol or not str(account_id).isdigit() or int(account_id) <= 0
                or owner.get('kind') not in ('trade', 'compensation')
                or not owner.get('db_path') or not owner.get('entry_time')
                or not number(owner.get('entry_price'), positive=True)
                or not owner.get('source_type') or not isinstance(decision, dict)
                or not decision.get('reason') or not isinstance(decision.get('metadata'), dict)):
            raise ValueError('exit_identity_invalid')
        owner['db_path'] = str(Path(owner['db_path']).resolve())
        entry_id = str(owner.get('entry_order_id') or '').strip()
        if entry_id:
            close_key = 'entry:' + entry_id
        elif owner.get('kind') == 'trade' and owner.get('trade_id') is not None:
            close_key = f"trade:{owner['db_path']}:{owner['trade_id']}:{owner['entry_time']}"
        elif (owner.get('kind') == 'compensation' and owner.get('instruction_id') is not None
              and owner.get('execution_attempt') is not None):
            close_key = (f"compensation:{owner['db_path']}:{owner['instruction_id']}:"
                         f"{owner['execution_attempt']}:{owner['entry_time']}")
        else:
            raise ValueError('exit_identity_invalid')
        if owner.get('close_key') != close_key:
            raise ValueError('exit_identity_key_mismatch')
        owner['quantity'] = quantity
        limit_price = number(limit_price)
        limit_timeout_s = number(limit_timeout_s)
        if (limit_price is None or limit_timeout_s is None
                or (limit_price > 0 and round(limit_price, 2) <= 0)):
            raise ValueError('exit_order_parameters_invalid')
        if session not in ('RTH', 'ETH') or (session == 'ETH' and (target_env != 'sim' or limit_price <= 0)):
            raise ValueError('exit_session_invalid')
        # Serialize before claiming; malformed snapshots must never reach the broker.
        owner_json, decision_json = json_text(owner), json_text(decision)
        with self.transaction() as db:
            old = self.decode(db.execute(
                'SELECT * FROM exit_intents WHERE account_id=? AND target_env=? AND close_key=?',
                (str(account_id), target_env, close_key)).fetchone())
            if old:
                if (old['owner'] != owner or old['direction'] != direction or old['symbol'] != symbol):
                    return None, held('exit_owner_conflict', owner=owner, decision=decision, state='BLOCKED')
                if old['session'] != session:
                    return None, held('exit_session_conflict', owner=owner, decision=decision, state='BLOCKED')
                return old, None
            busy = db.execute("SELECT 1 FROM exit_intents WHERE account_id=? AND target_env=? "
                              "AND symbol=? AND state!='BOOKED'",
                              (str(account_id), target_env, symbol)).fetchone()
            if busy:
                return None, held('exit_symbol_busy', owner=owner, decision=decision, state='BLOCKED')
            intent_id = uuid.uuid4().hex
            db.execute('''INSERT INTO exit_intents
                (intent_id,account_id,target_env,symbol,close_key,direction,quantity,owner_json,
                 decision_json,session,limit_price,limit_timeout_s,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (intent_id, str(account_id), target_env, symbol, close_key, direction, quantity,
                 owner_json, decision_json, session, round(limit_price, 2), limit_timeout_s, now, now))
        return self.get(intent_id), None

    def reserve_leg(self, intent, kind, quantity, price, deadline, now, broker_qty_before=None):
        """Commit the send gate before SDK I/O; it is never leased or auto-unlocked."""
        with self.transaction() as db:
            current = db.execute('SELECT * FROM exit_intents WHERE intent_id=?',
                                 (intent['intent_id'],)).fetchone()
            if current['version'] != intent['version'] or current['state'] in ('BOOKED', 'FILLED_UNBOOKED'):
                return None
            previous = db.execute('SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence DESC LIMIT 1',
                                  (intent['intent_id'],)).fetchone()
            if previous and (previous['sequence'] != 1 or previous['kind'] != 'LIMIT'
                             or previous['state'] != 'TERMINAL' or previous['dealt_qty'] is None
                             or quantity != current['quantity'] - previous['dealt_qty']):
                return None
            if not previous and quantity != current['quantity']:
                return None
            sequence = 2 if previous else 1
            remark_prefix = 'pbe_'
            candidate = str((intent.get('decision') or {}).get('metadata', {}).get(
                'broker_remark_prefix', ''))
            if (intent['session'] == 'ETH' and candidate.startswith('ext_exit_')
                    and 0 < len(candidate) <= 64 and candidate == candidate.strip()):
                remark_prefix = candidate
            remark = remark_prefix + uuid.uuid4().hex[:24]
            db.execute('''INSERT INTO exit_legs
                (intent_id,sequence,kind,quantity,price,remark,state,deadline,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'PREPARED',?,?,?)''',
                (intent['intent_id'], sequence, kind, quantity, price, remark, deadline, now, now))
            db.execute("UPDATE exit_intents SET state='PENDING',version=version+1,updated_at=?,"
                       "broker_qty_before=COALESCE(broker_qty_before,?) WHERE intent_id=?",
                       (now, broker_qty_before, intent['intent_id']))
        return self.legs(intent['intent_id'])[-1]

    def attach_order(self, leg, order_id, now):
        with self.transaction() as db:
            changed = db.execute("UPDATE exit_legs SET order_id=?,state=CASE WHEN state='PREPARED' THEN 'ACTIVE' "
                                 "ELSE state END,version=version+1,updated_at=? WHERE intent_id=? AND sequence=? "
                                 "AND (order_id IS NULL OR order_id=?)",
                                 (order_id, now, leg['intent_id'], leg['sequence'], order_id)).rowcount
            if not changed:
                raise ValueError('exit_order_id_conflict')

    def observe(self, leg, row, now):
        status = token(row.get('order_status', ''))
        qty = number(row.get('dealt_qty'))
        avg = number(row.get('dealt_avg_price'), positive=True)
        if status not in TERMINAL_STATUSES | ACTIVE_STATUSES or qty is None or qty > leg['quantity']:
            raise ValueError('exit_order_evidence_invalid')
        if status == 'FILLED_ALL' and qty != leg['quantity']:
            raise ValueError('exit_fill_quantity_conflict')
        amount = qty * avg if avg is not None else (0.0 if qty == 0 else None)
        if amount is not None and not math.isfinite(amount):
            raise ValueError('exit_fill_amount_invalid')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM exit_legs WHERE intent_id=? AND sequence=?',
                             (leg['intent_id'], leg['sequence'])).fetchone()
            if str(row.get('order_id', '')) != old['order_id']:
                raise ValueError('exit_order_id_conflict')
            # Late cache snapshots cannot lower cumulative evidence or reopen terminal orders.
            if old['dealt_qty'] is not None and qty < old['dealt_qty']:
                return
            if old['state'] == 'TERMINAL' and status not in TERMINAL_STATUSES:
                return
            if (old['dealt_amount'] is not None and amount is not None
                    and amount < old['dealt_amount']):
                raise ValueError('exit_fill_amount_regression')
            if old['dealt_qty'] == qty and amount is None:
                amount = old['dealt_amount']
            state = 'TERMINAL' if status in TERMINAL_STATUSES else 'ACTIVE'
            if old['state'] == 'CANCEL_REQUESTED' and state == 'ACTIVE':
                state = old['state']
            db.execute('''UPDATE exit_legs SET state=?,broker_status=?,dealt_qty=?,dealt_amount=?,
                version=version+1,updated_at=? WHERE intent_id=? AND sequence=?''',
                (state, status, qty, amount, now, leg['intent_id'], leg['sequence']))
            db.execute('UPDATE exit_intents SET version=version+1,updated_at=? WHERE intent_id=?',
                       (now, leg['intent_id']))

    def mark_cancel(self, leg, now):
        with self.transaction() as db:
            return bool(db.execute("UPDATE exit_legs SET state='CANCEL_REQUESTED',version=version+1,"
                                   "updated_at=? WHERE intent_id=? AND sequence=? AND version=? "
                                   "AND state IN ('ACTIVE','CANCEL_REQUESTED')",
                                   (now, leg['intent_id'], leg['sequence'], leg['version'])).rowcount)

    def result(self, intent_id, now, error='exit_order_pending', state='PENDING'):
        with self.transaction() as db:
            intent = self.decode(db.execute('SELECT * FROM exit_intents WHERE intent_id=?',
                                            (intent_id,)).fetchone())
            if intent['state'] in ('FILLED_UNBOOKED', 'BOOKED'):
                return dict(intent['result'], exit_state=intent['state'])
            legs = [dict(row) for row in db.execute(
                'SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence', (intent_id,))]
            result = held(error, intent=intent, state=state)
            known = all(leg['dealt_qty'] is not None for leg in legs)
            qty = sum(leg['dealt_qty'] or 0 for leg in legs)
            prices = all(leg['dealt_amount'] is not None for leg in legs)
            amount = sum(leg['dealt_amount'] or 0 for leg in legs)
            before = intent['broker_qty_before']
            result.update(dealt_qty=qty if known else None,
                          broker_qty_before=before,
                          broker_qty_after=before - qty if before is not None and known else None,
                          futu_order_id=legs[-1]['order_id'] or '' if legs else '')
            weighted_price = number(amount / qty, positive=True) if qty > 0 else None
            if legs and known and qty == intent['quantity'] and prices and weighted_price is not None:
                state = 'FILLED_UNBOOKED'
                result.update(success=True, order_filled=True, error='', exit_state=state,
                              filled_price=weighted_price, confirmed_at=now,
                              price_source=('broker_weighted_deal' if len(legs) > 1 else
                                            'broker_limit_deal' if legs[0]['kind'] == 'LIMIT' else 'broker_deal'))
            elif qty == intent['quantity'] and legs:
                state = 'HELD'
                result.update(exit_state=state, error='exit_fill_price_pending')
            db.execute('''UPDATE exit_intents SET state=?,result_json=?,last_error=?,
                version=version+1,updated_at=? WHERE intent_id=?''',
                (state, json_text(result), result['error'], now, intent_id))
            return result

    def ack_booked(self, intent_id, receipt, now):
        if not isinstance(receipt, dict) or not receipt:
            return False
        encoded = json_text(receipt)
        with self.transaction() as db:
            row = db.execute('SELECT state,receipt_json FROM exit_intents WHERE intent_id=?',
                             (intent_id,)).fetchone()
            if not row:
                return False
            if row['state'] == 'BOOKED':
                return row['receipt_json'] == encoded
            if row['state'] != 'FILLED_UNBOOKED':
                return False
            return bool(db.execute("UPDATE exit_intents SET state='BOOKED',receipt_json=?,"
                                   "version=version+1,updated_at=? WHERE intent_id=? AND state='FILLED_UNBOOKED'",
                                   (encoded, now, intent_id)).rowcount)

    def release_unsettled(self, *, account_id, target_env, symbol, direction, reason, now,
                          evidence=None):
        """外部渠道平掉仓位后释放该仓的未决出场意图 (RFC-128).

        病灶: `position_sync` 的 external_close / 真幽灵仓对账只写 `trade_records`
        + DELETE `open_positions`, **完全不碰 `exit_intents`** ⇒ 意图永久卡
        PENDING/HELD, 后果链 (2026-10-01 US.ACN 首例, 三方实证):
          ① `_resume_playbook_exits` 把该 symbol 加进 `blocked_exits`
             ⇒ 主退出循环 `continue`, **SL 评估被整体跳过** (当日 SL=223.16
             被越过 2.0pp 而未平, 多暴露 42%);
          ② fast loop 每 ~10s 告警一次 (当日 2029 条 ERROR);
          ③ RFC-127 影子按 `result.dealt_qty` (系统自己那条已撤腿的 0) 落
             「剩余 N 股可升级」幽灵样本 ⇒ 阶段1 判据2 被顶成假 PASS。

        语义 (与 `ack_booked` 同一终态, **不新造状态值**): `state → 'BOOKED'`
        同时做到 ①退出 `list_unsettled()` 枚举 (`WHERE state != 'BOOKED'`)
        ⇒ 不再进 `blocked_exits` ⇒ SL/BE/trailing/EOD 兜底重新接管;
        ②解除 partial unique index `exit_active_symbol … WHERE state != 'BOOKED'`
        的 symbol busy ⇒ 该标的新意图可再建。
        **释放不会自动平仓** — 纯账目, 平仓职责交回兜底链。

        匹配键四元组 `(account_id, target_env, symbol, direction)`:
        partial unique index 是 `(account_id, target_env, symbol)` ⇒ 意图**按账户隔离**,
        而"券商侧无持仓"也是按账户查的 ⇒ 只按 symbol 匹配会误释放另一账户
        (仓位可能还在) 的意图。方向不符同样不释放 (同 symbol 双向意图互不误伤)。

        幂等: `WHERE state != 'BOOKED'` 天然保证重复调用返回空列表。
        """
        want_dir = token(direction)
        if not (account_id and target_env and symbol and want_dir):
            return []
        receipt = {
            'kind': 'external_close_release',
            'external_close_release': True,
            'reason': str(reason or ''),
            'released_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'broker_qty_evidence': evidence if isinstance(evidence, dict) else {},
        }
        encoded = json_text(receipt)
        with self.transaction() as db:
            rows = db.execute(
                "SELECT intent_id,direction,state FROM exit_intents "
                "WHERE account_id=? AND target_env=? AND symbol=? AND state!='BOOKED'",
                (str(account_id), str(target_env), str(symbol))).fetchall()
            released = []
            for row in rows:
                if token(row['direction']) != want_dir:
                    logger.warning(
                        'exit_intent_release_direction_mismatch symbol=%s intent=%s '
                        'intent_direction=%s closed_direction=%s (不释放)',
                        symbol, row['intent_id'], row['direction'], want_dir)
                    continue
                legs = db.execute(
                    "SELECT sequence,state,order_id,dealt_qty FROM exit_legs "
                    "WHERE intent_id=? ORDER BY sequence",
                    (row['intent_id'],)).fetchall()
                unsafe_legs = [leg for leg in legs
                               if leg['state'] != 'TERMINAL' or leg['dealt_qty'] is None]
                if unsafe_legs:
                    logger.warning(
                        'exit_intent_release_blocked_by_unsettled_leg symbol=%s intent=%s '
                        'legs=%s — 先撤销/等待该出场腿终态, 再释放意图 (避免活跃单变孤儿)',
                        symbol, row['intent_id'], [
                            {'sequence': leg['sequence'], 'state': leg['state'],
                             'order_id': leg['order_id'], 'dealt_qty': leg['dealt_qty']}
                            for leg in unsafe_legs])
                    continue
                if db.execute(
                        "UPDATE exit_intents SET state='BOOKED',receipt_json=?,last_error='',"
                        "version=version+1,updated_at=? "
                        "WHERE intent_id=? AND state!='BOOKED'",
                        (encoded, now, row['intent_id'])).rowcount:
                    released.append(row['intent_id'])
            if released:
                logger.warning(
                    'exit_intent_released_on_external_close symbol=%s env=%s account=%s '
                    'direction=%s intents=%s reason=%s — 该仓出场职责交回 SL/BE/trailing/EOD 兜底',
                    symbol, target_env, account_id, want_dir, released, reason)
            return released


class ExitLifecycle:
    def __init__(self, adapter, ledger, clock=None):
        self.adapter, self.ledger, self.clock = adapter, ledger, clock or time

    def request(self, symbol, quantity, direction, target_env, limit_price, timeout, owner, decision,
                session='RTH'):
        if target_env not in ('sim', 'real'):
            return held('exit_environment_invalid', owner=owner, decision=decision)
        if session not in ('RTH', 'ETH'):
            return held('exit_session_invalid', owner=owner, decision=decision)
        if session == 'ETH' and (target_env != 'sim' or number(limit_price, positive=True) is None):
            return held('exit_eth_requires_sim_limit', owner=owner, decision=decision)
        account = getattr(self.adapter, 'real_account_id' if target_env == 'real' else 'sim_account_id', '')
        intent, blocked = self.ledger.request(account_id=account, target_env=target_env, symbol=symbol,
            direction=direction, quantity=quantity, owner=owner, decision=decision,
            limit_price=limit_price, limit_timeout_s=timeout, now=self.clock.time(), session=session)
        return blocked if blocked else self.resume(intent['intent_id'])

    def route(self, intent):
        ctx, account, env, lock = self.adapter._ctx_and_acc(intent['target_env'])
        if str(account) != intent['account_id']:
            raise ValueError('exit_account_mismatch')
        return ctx, {'trd_env': env, 'acc_id': int(account)}, lock

    def identity_matches(self, intent, leg, row, *, recover=False):
        if (str(row.get('code', '')) != intent['symbol']
                or token(row.get('trd_side', '')) != ('SELL' if intent['direction'] == 'LONG' else 'BUY')
                or number(row.get('qty'), positive=True) != leg['quantity']):
            return False
        # SDK tables omit account/environment; every query is explicitly account/env scoped.
        if 'acc_id' in row and str(row['acc_id']) != intent['account_id']:
            return False
        if 'trd_env' in row and token(row['trd_env']) != ('REAL' if intent['target_env'] == 'real' else 'SIMULATE'):
            return False
        if recover and str(row.get('remark', '')) != leg['remark']:
            return False
        return True

    def query(self, intent, leg, ctx, scope, lock):
        recovered = not leg['order_id']
        candidates = []
        failures = 0
        start = (datetime.fromtimestamp(intent['created_at'], timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
        end = (datetime.fromtimestamp(self.clock.time(), timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d')
        for history in (False, True):
            try:
                with lock:
                    if history:
                        ret, data = ctx.history_order_list_query(code=intent['symbol'], start=start, end=end, **scope)
                    else:
                        ret, data = ctx.order_list_query(order_id=leg['order_id'] or '',
                                                        refresh_cache=True, **scope)
                if ret != 0 or data is None:
                    failures += 1
                    continue
                for row in data.to_dict('records'):
                    key_matches = (str(row.get('remark', '')) == leg['remark'] if recovered
                                   else str(row.get('order_id', '')) == leg['order_id'])
                    if not key_matches:
                        continue
                    if not self.identity_matches(intent, leg, row, recover=recovered):
                        raise ValueError('exit_order_identity_conflict')
                    order_id = str(row.get('order_id') or '').strip()
                    if order_id.lower() in ('', 'none', 'nan'):
                        raise ValueError('exit_order_identity_missing')
                    candidates.append(row)
            except ValueError:
                raise
            except Exception:
                failures += 1
        ids = {str(row['order_id']) for row in candidates}
        if not candidates or len(ids) != 1 or (recovered and failures):
            raise ValueError('exit_order_evidence_pending')
        if recovered:
            self.ledger.attach_order(leg, next(iter(ids)), self.clock.time())
        # Apply low cumulative snapshots first; the store prevents regression across calls.
        for row in sorted(candidates, key=lambda row: number(row.get('dealt_qty')) or 0):
            self.ledger.observe(leg, row, self.clock.time())

    def preflight(self, intent, ctx, scope, lock, *, required_quantity=None):
        required_quantity = intent['quantity'] if required_quantity is None else required_quantity
        with lock:
            ret, positions = ctx.position_list_query(**scope)
        if ret != 0 or positions is None:
            raise ValueError('exit_position_evidence_pending')
        # 2026-09-21 全链路复查加固 (P1-4): 美股退出意图不得匹配综合账户下的跨市场行
        # (若 intent.symbol 被污染为非 US. 前缀, 宁可 ownership_conflict 拒退也不误碰他市场仓位)。
        matches = [
            r for r in positions.to_dict('records')
            if r.get('code') == intent['symbol']
            and str(r.get('code', '')).startswith('US.')
        ]
        if len(matches) != 1:
            raise ValueError('exit_position_ownership_conflict')
        row = matches[0]
        raw_qty = row.get('qty')
        try:
            signed = float(raw_qty)
        except (TypeError, ValueError):
            raise ValueError('exit_position_ownership_conflict')
        if isinstance(raw_qty, bool) or not math.isfinite(signed):
            raise ValueError('exit_position_ownership_conflict')
        side = token(row.get('position_side', 'SHORT' if signed < 0 else 'LONG'))
        if (side != intent['direction'] or abs(signed) < required_quantity
                or (side == 'LONG' and signed < 0)
                or ('acc_id' in row and str(row['acc_id']) != intent['account_id'])
                or ('trd_env' in row and token(row['trd_env']) != token(scope['trd_env']))):
            raise ValueError('exit_position_ownership_conflict')
        with lock:
            ret, orders = ctx.order_list_query(refresh_cache=True, **scope)
        if ret != 0 or orders is None:
            raise ValueError('exit_legacy_order_evidence_pending')
        close_side = 'SELL' if intent['direction'] == 'LONG' else 'BUY'
        for order in orders.to_dict('records'):
            side = token(order.get('trd_side', ''))
            if (order.get('code') == intent['symbol'] and (side == close_side or side not in ('BUY', 'SELL'))
                    and token(order.get('order_status', '')) not in TERMINAL_STATUSES):
                logger.warning(
                    'Exit blocked by active order intent=%s symbol=%s order_id=%s status=%s remark=%s',
                    intent['intent_id'], intent['symbol'], order.get('order_id', ''),
                    order.get('order_status', ''), order.get('remark', ''),
                )
                raise ValueError('exit_unowned_active_order')
        return abs(signed)

    def send(self, intent, leg, ctx, scope, lock):
        from futu import TrdSide, OrderType, TimeInForce, Session
        order_session = Session.RTH if intent['session'] == 'RTH' else Session.ETH
        try:
            # R04: durable exit orders explicitly declare the claimed session and DAY TIF.
            with lock:
                _ret, data = ctx.place_order(price=leg['price'], qty=leg['quantity'],
                    code=intent['symbol'], trd_side=TrdSide.SELL if intent['direction'] == 'LONG' else TrdSide.BUY,
                    order_type=OrderType.NORMAL if leg['kind'] == 'LIMIT' else OrderType.MARKET,
                    time_in_force=TimeInForce.DAY, session=order_session,
                    remark=leg['remark'], **scope)
        except Exception:
            # The committed PREPARED leg also represents a lost send response.
            logger.warning('Exit submit response unknown intent=%s', intent['intent_id'], exc_info=True)
            return
        if data is not None and hasattr(data, 'to_dict'):
            rows = data.to_dict('records')
            if len(rows) == 1:
                row = rows[0]
                order_id = str(row.get('order_id') or '').strip()
                if order_id.lower() not in ('', 'none', 'nan'):
                    if not self.identity_matches(intent, leg, row):
                        raise ValueError('exit_submit_identity_conflict')
                    self.ledger.attach_order(leg, order_id, self.clock.time())
        # Even explicit rejection stays claimed until order evidence proves its outcome.

    def resume(self, intent_id, *, allow_submit=True):
        intent = self.ledger.get(intent_id)
        if not intent:
            return held('exit_intent_not_found')
        account = getattr(self.adapter, 'real_account_id' if intent['target_env'] == 'real' else 'sim_account_id', '')
        if str(account) != intent['account_id']:
            return held('exit_account_mismatch', intent=intent)
        if intent['state'] in ('FILLED_UNBOOKED', 'BOOKED'):
            return dict(intent['result'], exit_state=intent['state'])
        try:
            ctx, scope, lock = self.route(intent)
            legs = self.ledger.legs(intent_id)
            if not legs:
                if not allow_submit:
                    return self.ledger.result(intent_id, self.clock.time())
                before = self.preflight(intent, ctx, scope, lock)
                kind = 'LIMIT' if intent['limit_price'] > 0 else 'MARKET'
                timeout = intent['limit_timeout_s'] if kind == 'LIMIT' else 30.0
                leg = self.ledger.reserve_leg(intent, kind, intent['quantity'], intent['limit_price'],
                    self.clock.time() + timeout, self.clock.time(), broker_qty_before=before)
                if not leg:
                    return held('exit_concurrent_owner', intent=intent, state='PENDING')
                self.send(intent, leg, ctx, scope, lock)
            while True:
                legs = self.ledger.legs(intent_id)
                # Revisit earlier legs when fill quantities arrived before their prices.
                for leg in legs:
                    if (leg == legs[-1] or leg['dealt_qty'] is None or leg['dealt_amount'] is None):
                        self.query(intent, leg, ctx, scope, lock)
                result = self.ledger.result(intent_id, self.clock.time())
                if result['success']:
                    return result
                leg = self.ledger.legs(intent_id)[-1]
                if leg['dealt_qty'] == leg['quantity']:
                    return result
                if not allow_submit:
                    return result
                if leg['state'] == 'TERMINAL':
                    if leg['kind'] != 'LIMIT':
                        return self.ledger.result(intent_id, self.clock.time(), 'exit_terminal_shortfall', 'HELD')
                    if intent['session'] == 'ETH':
                        return self.ledger.result(intent_id, self.clock.time(),
                                                  'exit_eth_partial_fill_held', 'HELD')
                    if leg['dealt_qty'] is None:
                        return self.ledger.result(intent_id, self.clock.time(), 'exit_final_quantity_unknown', 'HELD')
                    remaining = intent['quantity'] - leg['dealt_qty']
                    intent = self.ledger.get(intent_id)
                    self.preflight(intent, ctx, scope, lock, required_quantity=remaining)
                    market = self.ledger.reserve_leg(intent, 'MARKET', remaining, 0.0,
                        self.clock.time() + 30.0, self.clock.time())
                    if not market:
                        return held('exit_concurrent_owner', intent=intent, state='PENDING')
                    self.send(intent, market, ctx, scope, lock)
                    continue
                if self.clock.time() >= leg['deadline']:
                    if leg['kind'] == 'MARKET':
                        return result
                    if self.ledger.mark_cancel(leg, self.clock.time()):
                        from futu import ModifyOrderOp
                        try:
                            with lock:
                                ctx.modify_order(modify_order_op=ModifyOrderOp.CANCEL,
                                                 order_id=leg['order_id'], qty=0, price=0, **scope)
                        except Exception:
                            logger.warning('Exit cancel response unknown intent=%s', intent_id, exc_info=True)
                        self.query(intent, leg, ctx, scope, lock)
                        leg = self.ledger.legs(intent_id)[-1]
                        if leg['state'] == 'TERMINAL' or leg['dealt_qty'] == leg['quantity']:
                            continue
                    return self.ledger.result(intent_id, self.clock.time(), 'exit_cancel_pending')
                self.clock.sleep(min(0.5, leg['deadline'] - self.clock.time()))
        except ValueError as exc:
            error = str(exc)
            if (not error.startswith('exit_') or ' ' in error
                    or 'broker_no_position' in error or 'insufficient' in error):
                error = 'exit_evidence_invalid'
            # exc_info=True (2026-09-13, W456 棘轮 W6_except_stack 处置①): 本 ValueError 是
            #   守卫 fail-closed 的控制流信号, 栈=抛出点, 正是值班人定位"哪个守卫拦的"所需
            #   (9/11 exit_identity_required 事故若有栈可即刻定位 dual_broker 守卫而非猜)。
            #   与同函数上方 cancel-response warning 的 exc_info=True 保持一致 (原为漏写)。
            logger.warning('Exit held intent=%s reason=%s', intent_id, error, exc_info=True)
            return self.ledger.result(intent_id, self.clock.time(), error, 'HELD')
        except Exception:
            logger.exception('Exit reconciliation failed intent=%s', intent_id)
            # Ledger errors propagate rather than allowing any further broker operation.
            return self.ledger.result(intent_id, self.clock.time(), 'exit_reconciliation_pending', 'HELD')
