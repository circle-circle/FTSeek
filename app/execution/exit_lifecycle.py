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


def _error_text(error, limit):
    # 不可写 `error or ''`: 券商失败路径今日返回 str, 但 `place_order` 成功路径返回
    # DataFrame, 而 `bool(DataFrame)` 抛 "truth value is ambiguous" ⇒ 会在拒单处置里
    # 二次抛错, 把"明确拒单"退回成永久认领 (RFC-214 要消除的那个死锁本身)。
    if error is None:
        return ''
    return str(error)[:limit]


def _push_submit_rejected(intent, error):
    """RFC-214 可观测输出（走 push hub，禁止静默终态化）。"""
    try:
        from app.push import get_push_hub
        from app.push.message import Category, Level
        get_push_hub().push_text(
            f"⚠️ [出场提交拒单] {intent['symbol']} {intent['direction']} "
            f"qty={intent['quantity']}：券商明确拒单且未创建订单，"
            f"已按 RFC-214 标记 FAILED/dealt=0，等待 RFC-157 复核。{_error_text(error, 240)}",
            level=Level.HIGH,
            category=Category.RISK_ALERT,
        )
    except Exception as exc:
        logger.warning('exit_submit_reject_push_failed intent=%s: %s',
                       intent.get('intent_id'), exc, exc_info=True)


class ExitLedger:
    def __init__(self, path):
        self.path = str(path)
        self._archive_dir = Path(self.path).resolve().parent / 'exit_intents_archive'
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

    def mark_submit_rejected(self, leg, now, *, error):
        """RFC-214: broker explicitly rejected a never-accepted submit (zero fill).

        Narrow CAS only: PREPARED + NULL order_id + NULL dealt_qty + matching version.
        Any other state/evidence must remain claimed (UNKNOWN), never forced terminal.
        """
        message = _error_text(error, 500)
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE exit_legs SET state='TERMINAL',broker_status='FAILED',"
                "dealt_qty=0,dealt_amount=0.0,last_error=?,version=version+1,updated_at=? "
                "WHERE intent_id=? AND sequence=? AND version=? "
                "AND state='PREPARED' AND order_id IS NULL AND dealt_qty IS NULL",
                (message, now, leg['intent_id'], leg['sequence'], leg['version'])).rowcount
            if changed:
                db.execute('UPDATE exit_intents SET version=version+1,updated_at=? WHERE intent_id=?',
                           (now, leg['intent_id']))
            return bool(changed)

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

    # ── INCIDENT-002: 归档并删除 = 释放语义 SSOT ────────────────────────────
    REFUSE_NOT_FOUND = 'exit_release_intent_not_found'
    REFUSE_UNSAFE_LEG = 'exit_release_unsafe_leg'
    REFUSE_UNBOOKED = 'exit_release_filled_unbooked'
    REFUSE_RACE = 'exit_release_intent_changed'
    REFUSE_VERIFY = 'exit_release_verification_failed'

    @property
    def archive_dir(self) -> Path:
        return self._archive_dir

    @staticmethod
    def _unsafe_legs(legs):
        """非终态 / 成交量未知的腿 — 删意图会让活跃出场单变孤儿。"""
        return [leg for leg in legs
                if leg['state'] != 'TERMINAL' or leg['dealt_qty'] is None]

    def _snapshot(self, db, intent_id):
        intent = db.execute('SELECT * FROM exit_intents WHERE intent_id=?',
                            (intent_id,)).fetchone()
        if intent is None:
            return None, None
        legs = [dict(row) for row in db.execute(
            'SELECT * FROM exit_legs WHERE intent_id=? ORDER BY sequence', (intent_id,))]
        return dict(intent), legs

    def _unsafe_leg_detail(self, intent_id):
        """拒释放时给回调/日志用的腿明细 (罕见路径, 单独一次只读查询)。"""
        with self.transaction() as db:
            rows = db.execute(
                'SELECT sequence,state,order_id,dealt_qty FROM exit_legs '
                'WHERE intent_id=? ORDER BY sequence', (intent_id,)).fetchall()
        return [{'sequence': r['sequence'], 'state': r['state'],
                 'order_id': r['order_id'], 'dealt_qty': r['dealt_qty']}
                for r in rows
                if r['state'] != 'TERMINAL' or r['dealt_qty'] is None]

    def _write_archive(self, intent, legs, *, reason, now, kind, evidence) -> Path:
        """整行意图 (含 receipt_json/result_json 原文) + 全部腿 → 归档 JSON。

        写盘在事务**外** (文件 I/O 不得占 BEGIN IMMEDIATE 写锁 — 与 RFC-129
        推送同理, 否则与 fast loop 10s 节奏抢锁)。tmp + replace 原子落盘。
        """
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        path = self._archive_dir / (
            '%s_%d_%s.json' % (intent['intent_id'], int(now), uuid.uuid4().hex[:8]))
        payload = {
            'archived_at': now,
            'archived_at_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='seconds'),
            'kind': kind,
            # RFC-128 §3.1 的机读契约位保留 (原写在 receipt_json, 现随档保存)
            'external_close_release': kind == 'external_close_release',
            'reason': str(reason or ''),
            'broker_qty_evidence': evidence if isinstance(evidence, dict) else {},
            'intent': intent,
            'legs': legs,
        }
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
        return path

    def archive_and_delete(self, intent_id, *, reason, now, evidence=None,
                           kind='manual_release'):
        """归档并删除意图+腿 ⇒ **close_key 真正空出** (INCIDENT-002 释放语义 SSOT).

        为什么不是改 `state='BOOKED'` (RFC-128 原语义, 已被 10/2 ACN 实证证伪):
        `UNIQUE(account_id, target_env, close_key)` **无 state 谓词** ⇒ BOOKED 行
        永久占用 close_key。于是 ①ETH 链 `request()` 原样返回旧 BOOKED 行,
        而 `reserve_leg` 对 BOOKED/FILLED_UNBOOKED 直接 None ⇒ 一单发不出;
        ②RTH 链 `close_position` 默认 `session='RTH'` ≠ 旧行 'ETH' ⇒
        `exit_session_conflict` BLOCKED ⇒ SL 兜底同样封死。检验 C 证明改 session
        也不够。BOOKED 只解除 `list_unsettled()` 枚举与 symbol busy,
        **恢复评估 ≠ 恢复执行能力** ⇒ 把可见告警换成静默零保护。

        返回 `(archive_path, None)` 成功; `(None, refuse_code)` 被守卫拒绝。
        守卫 (fail-closed, 宁可不释放也不制造孤儿/丢账):
          · 非终态腿或成交量未知 ⇒ REFUSE_UNSAFE_LEG (活跃出场单会变孤儿);
          · state=FILLED_UNBOOKED ⇒ REFUSE_UNBOOKED (成交已发生簿记未完成,
            删行会掐断 `acknowledge_close`/`ack_booked` 的落账路径)。
        归档写盘失败 ⇒ 异常上抛, **不删行** (证据保全优先于释放)。
        读档与删除之间意图发生变化 (如成交回流) ⇒ 回滚删除 + 撤销刚写的档。
        """
        with self.transaction() as db:
            intent, legs = self._snapshot(db, intent_id)
        if intent is None:
            return None, self.REFUSE_NOT_FOUND
        if intent['state'] == 'FILLED_UNBOOKED':
            return None, self.REFUSE_UNBOOKED
        if self._unsafe_legs(legs or []):
            return None, self.REFUSE_UNSAFE_LEG

        path = self._write_archive(intent, legs or [], reason=reason, now=now,
                                   kind=kind, evidence=evidence)

        with self.transaction() as db:
            # 竞态复核: 读档后意图可能已被并发进程推进 (成交回流 → FILLED_UNBOOKED,
            # 或新腿变 ACTIVE)。任一守卫在此刻不成立 ⇒ 不删, 并撤销刚写的档。
            current, cur_legs = self._snapshot(db, intent_id)
            if current is None or current['state'] == 'FILLED_UNBOOKED' \
                    or self._unsafe_legs(cur_legs or []):
                try:
                    path.unlink()
                except OSError:
                    logger.warning('exit_release_archive_cleanup_failed path=%s',
                                   path, exc_info=True)
                return None, (self.REFUSE_NOT_FOUND if current is None
                              else self.REFUSE_UNBOOKED if current['state'] == 'FILLED_UNBOOKED'
                              else self.REFUSE_RACE)
            db.execute('DELETE FROM exit_legs WHERE intent_id=?', (intent_id,))
            db.execute('DELETE FROM exit_intents WHERE intent_id=?', (intent_id,))
            gone = db.execute('SELECT 1 FROM exit_intents WHERE intent_id=?',
                              (intent_id,)).fetchone() is None
            key_free = db.execute(
                'SELECT 1 FROM exit_intents WHERE account_id=? AND target_env=? AND close_key=?',
                (intent['account_id'], intent['target_env'], intent['close_key'])).fetchone() is None
            legs_gone = db.execute('SELECT 1 FROM exit_legs WHERE intent_id=?',
                                   (intent_id,)).fetchone() is None
        if not (gone and key_free and legs_gone):
            # 回读不成立 = 不声称成功 (档已写, 行可能仍在 ⇒ 人工介入, 不静默)。
            # 注: 此处**不在 except 块内** (是回读断言失败, 非异常) ⇒ 不加 exc_info,
            # 否则日志暗示有栈可读, 而 sys.exc_info() 实为 None ⇒ 误导值班人。
            logger.error('exit_release_verification_failed intent=%s gone=%s key_free=%s '
                         'legs_gone=%s archive=%s', intent_id, gone, key_free, legs_gone,
                         path)
            return None, self.REFUSE_VERIFY
        return path, None

    def restore_archived(self, intent_id):
        """从归档回插 intent+legs (人工释放的回滚路径).

        close_key 已被现役意图占用 ⇒ 拒 (不覆盖现役账目)。
        返回 `(archive_path, None)` 成功; `(None, refuse_code)` 失败。
        """
        if not self._archive_dir.is_dir():
            return None, 'exit_restore_archive_missing'
        archives = sorted(self._archive_dir.glob('%s_*.json' % intent_id))
        if not archives:
            return None, 'exit_restore_archive_missing'
        try:
            payload = json.loads(archives[-1].read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            logger.error('exit_restore_archive_unreadable path=%s: %s', archives[-1], exc,
                         exc_info=True)
            return None, 'exit_restore_archive_invalid'
        intent = payload.get('intent') or {}
        legs = payload.get('legs') or []
        if intent.get('intent_id') != intent_id:
            return None, 'exit_restore_archive_invalid'
        with self.transaction() as db:
            conflict = db.execute(
                'SELECT intent_id FROM exit_intents WHERE account_id=? AND target_env=? '
                'AND close_key=?',
                (intent.get('account_id'), intent.get('target_env'),
                 intent.get('close_key'))).fetchone()
            if conflict:
                return None, 'exit_restore_close_key_busy'
            icols = list(intent.keys())
            db.execute('INSERT OR IGNORE INTO exit_intents (%s) VALUES (%s)'
                       % (','.join(icols), ','.join('?' * len(icols))),
                       tuple(intent[c] for c in icols))
            for leg in legs:
                lcols = list(leg.keys())
                db.execute('INSERT OR IGNORE INTO exit_legs (%s) VALUES (%s)'
                           % (','.join(lcols), ','.join('?' * len(lcols))),
                           tuple(leg[c] for c in lcols))
            back = db.execute('SELECT 1 FROM exit_intents WHERE intent_id=?',
                              (intent_id,)).fetchone() is not None
            n_legs = db.execute('SELECT COUNT(*) FROM exit_legs WHERE intent_id=?',
                                (intent_id,)).fetchone()[0]
        if back and n_legs == len(legs):
            return archives[-1], None
        return None, 'exit_restore_verification_failed'

    def release_unsettled(self, *, account_id, target_env, symbol, direction, reason, now,
                          evidence=None, on_blocked=None):
        """外部渠道平掉仓位后释放该仓的未决出场意图 (RFC-128; 语义见 INCIDENT-002).

        病灶: `position_sync` 的 external_close / 真幽灵仓对账只写 `trade_records`
        + DELETE `open_positions`, **完全不碰 `exit_intents`** ⇒ 意图永久卡
        PENDING/HELD, 后果链 (2026-10-01 US.ACN 首例, 三方实证):
          ① `_resume_playbook_exits` 把该 symbol 加进 `blocked_exits`
             ⇒ 主退出循环 `continue`, **SL 评估被整体跳过** (当日 SL=223.16
             被越过 2.0pp 而未平, 多暴露 42%);
          ② fast loop 每 ~10s 告警一次 (当日 2029 条 ERROR);
          ③ RFC-127 影子按 `result.dealt_qty` (系统自己那条已撤腿的 0) 落
             「剩余 N 股可升级」幽灵样本 ⇒ 阶段1 判据2 被顶成假 PASS。

        释放语义 (2026-10-02 INCIDENT-002 订正): **归档并删除** (`archive_and_delete`
        SSOT), 不是改 BOOKED。BOOKED 虽能同时解除 `list_unsettled()` 枚举与
        partial unique index 的 symbol busy, 但 `UNIQUE(account_id,target_env,close_key)`
        无 state 谓词 ⇒ 它**永久占用 close_key**, ETH 重放与 RTH SL 兜底双双发不出单
        (详见 `archive_and_delete` docstring 的检验 A/B/C)。删除才真正空出身份键。
        **释放不会自动平仓** — 纯账目, 平仓职责交回兜底链。

        匹配键四元组 `(account_id, target_env, symbol, direction)`:
        partial unique index 是 `(account_id, target_env, symbol)` ⇒ 意图**按账户隔离**,
        而"券商侧无持仓"也是按账户查的 ⇒ 只按 symbol 匹配会误释放另一账户
        (仓位可能还在) 的意图。方向不符同样不释放 (同 symbol 双向意图互不误伤)。

        幂等: `WHERE state != 'BOOKED'` 天然保证重复调用返回空列表; 已 BOOKED 的
        历史意图**不在释放面内** (它们是正常平仓的账目, 不是卡死意图)。
        """
        want_dir = token(direction)
        if not (account_id and target_env and symbol and want_dir):
            return []
        with self.transaction() as db:
            rows = db.execute(
                "SELECT intent_id,direction FROM exit_intents "
                "WHERE account_id=? AND target_env=? AND symbol=? AND state!='BOOKED'",
                (str(account_id), str(target_env), str(symbol))).fetchall()
            candidates = [(row['intent_id'], row['direction']) for row in rows]
        released, blocked_events = [], []
        for intent_id, row_direction in candidates:
            if token(row_direction) != want_dir:
                logger.warning(
                    'exit_intent_release_direction_mismatch symbol=%s intent=%s '
                    'intent_direction=%s closed_direction=%s (不释放)',
                    symbol, intent_id, row_direction, want_dir)
                continue
            path, refuse = self.archive_and_delete(
                intent_id, reason=reason, now=now, evidence=evidence,
                kind='external_close_release')
            if path is not None:
                released.append(intent_id)
                continue
            if refuse == self.REFUSE_UNSAFE_LEG:
                unsafe_legs = self._unsafe_leg_detail(intent_id)
                logger.warning(
                    'exit_intent_release_blocked_by_unsettled_leg symbol=%s intent=%s '
                    'legs=%s — 先撤销/等待该腿终态, 再释放意图 (避免活跃单变孤儿)',
                    symbol, intent_id, unsafe_legs)
                # [RFC-129] 缺口可见化: 仓已不在券商侧 (external_close 前置保证) 而
                # 我方 pbe_* 腿仍非终态 ⇒ 释放被拒 ⇒ 该 symbol 继续进 blocked_exits
                # ⇒ **SL 评估仍被跳过** (即 10/1 ACN 病灶的变体)。守卫本身是对的
                # (意图删除后无人看护活跃单 = 孤儿, 且 orphan_ext_order_watch 只认
                # ext_exit_ 前缀扫不到 pbe_*), 但它把"人工先撤腿"变成隐性前提。
                # 缺口罕见 (生产 183/183 腿 TERMINAL, 该形态 0 先例) 且高危 ⇒
                # 正确处置是"看得见 + 人工", 不是自动撤单 (撤单有成交竞态风险)。
                # ⚠️ 只在此**收集**事件, 回调在事务外触发 (推送网络 I/O 不占写锁)。
                blocked_events.append({
                    'intent_id': intent_id,
                    'symbol': symbol,
                    'direction': row_direction,
                    'reason': reason,
                    'unsafe_legs': unsafe_legs,
                })
                continue
            # 其余拒绝码 (UNBOOKED/NOT_FOUND/RACE/VERIFY) 都是 fail-closed 留痕:
            # 不静默, 但也不进 blocked_events (不是"人工撤腿就能解"的同一形态)
            logger.warning(
                'exit_intent_release_refused symbol=%s intent=%s refuse=%s '
                '(fail-closed: 意图保留, 需人工核查)', symbol, intent_id, refuse)
        if released:
            logger.warning(
                'exit_intent_released_on_external_close symbol=%s env=%s account=%s '
                'direction=%s intents=%s reason=%s — 意图已归档删除, close_key 空出, '
                '该仓出场职责交回 SL/BE/trailing/EOD 兜底',
                symbol, target_env, account_id, want_dir, released, reason)
        # [RFC-129] 回调在事务**外**触发 (写锁已释放, 推送 I/O 不与 fast loop 抢锁)。
        # 单个回调故障只 WARNING, 不影响其余事件与已完成的释放。
        for _ev in blocked_events:
            if on_blocked is None:
                break
            try:
                on_blocked(_ev)
            except Exception as _cb_err:
                logger.warning(
                    'exit_intent_release_blocked_callback_failed symbol=%s intent=%s: %s',
                    symbol, _ev.get('intent_id'), _cb_err, exc_info=True)
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

    def _position_qty_or_unknown(self, intent, ctx, scope, lock):
        """Return broker quantity, or None when evidence is unavailable/conflicting."""
        try:
            with lock:
                ret, positions = ctx.position_list_query(**scope)
            if ret != 0 or positions is None:
                return None
            matches = [r for r in positions.to_dict('records')
                       if r.get('code') == intent['symbol']
                       and str(r.get('code', '')).startswith('US.')]
            if not matches:
                return 0.0
            if len(matches) != 1:
                return None
            row = matches[0]
            signed = float(row.get('qty'))
            if not math.isfinite(signed):
                return None
            side = token(row.get('position_side', 'SHORT' if signed < 0 else 'LONG'))
            if (side != intent['direction'] or (side == 'LONG' and signed < 0)
                    or ('acc_id' in row and str(row['acc_id']) != intent['account_id'])
                    or ('trd_env' in row and token(row['trd_env']) != token(scope['trd_env']))):
                return None
            return abs(signed)
        except Exception:
            return None

    def send(self, intent, leg, ctx, scope, lock):
        from futu import TrdSide, OrderType, TimeInForce, Session, RET_OK
        order_session = Session.RTH if intent['session'] == 'RTH' else Session.ETH
        try:
            # R04: durable exit orders explicitly declare the claimed session and DAY TIF.
            with lock:
                ret, data = ctx.place_order(price=leg['price'], qty=leg['quantity'],
                    code=intent['symbol'], trd_side=TrdSide.SELL if intent['direction'] == 'LONG' else TrdSide.BUY,
                    order_type=OrderType.NORMAL if leg['kind'] == 'LIMIT' else OrderType.MARKET,
                    time_in_force=TimeInForce.DAY, session=order_session,
                    remark=leg['remark'], **scope)
        except Exception:
            # The committed PREPARED leg also represents a lost send response.
            logger.warning('Exit submit response unknown intent=%s', intent['intent_id'], exc_info=True)
            return
        order_id = ''
        if data is not None and hasattr(data, 'to_dict'):
            for row in data.to_dict('records'):
                candidate = str(row.get('order_id') or '').strip()
                if candidate.lower() in ('', 'none', 'nan'):
                    continue
                if not self.identity_matches(intent, leg, row):
                    raise ValueError('exit_submit_identity_conflict')
                order_id = candidate
                self.ledger.attach_order(leg, order_id, self.clock.time())
                break
        # RFC-214: only a definitive rejection with no order id and no fill evidence
        # may become TERMINAL/FAILED. Unknown responses remain claimed as before.
        if ret != RET_OK and not order_id:
            broker_qty = self._position_qty_or_unknown(intent, ctx, scope, lock)
            if broker_qty is not None and broker_qty >= intent['quantity']:
                if self.ledger.mark_submit_rejected(leg, self.clock.time(), error=data):
                    _push_submit_rejected(intent, data)
            else:
                # Position dropped or query failed ⇒ possible fill/unknown ⇒ fail closed.
                # 无 exc_info: 此分支不在异常上下文里, 带上会多打一行 "NoneType: None"。
                logger.warning(
                    'Exit submit reject downgraded to UNKNOWN intent=%s broker_qty=%s',
                    intent['intent_id'], broker_qty)
        # UNKNOWN stays claimed until order evidence proves its outcome.

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
            # 2026-10-09 (用户裁「请优化调整」): *_pending 瞬态族 (成交证据/回报在途)
            #   降为 INFO 不打栈 — 抛出点恒已知 (上方 query 守卫), 宽限与超窗升级由
            #   playbook 监控层持有 (「平仓待回报」行 + 120s 宽限), 此处每条 10s 一轮的
            #   堆栈是纯噪音 (10-09 实证: 5 笔平仓 30min 内 14 条 Traceback)。
            #   下方 exc_info=True 保留给非瞬态守卫拒绝 (9/13 理由不变: 栈=抛出点,
            #   是值班人定位"哪个守卫拦的"所需)。W456 口径: INFO 不计两维违例。
            if error.endswith('_pending'):
                logger.info('Exit held intent=%s reason=%s (transient, 在途)', intent_id, error)
            else:
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
