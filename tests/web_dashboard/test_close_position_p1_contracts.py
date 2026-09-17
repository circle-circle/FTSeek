"""DEB-20260816-fullchain 批次A 契约测试: P1-7 / P1-8 / P1-9 / P2-15

P1-7: 部分平仓成功后 _pending_close 永久残留 → 组合级止损永久跳过
P1-8: 限价平仓未成交即写假 exit_price/realized_pnl → 污染日回撤熔断
P1-9: modify_stop_loss 撤旧单失败仍下新 STOP → 裸空头/双 STOP
P2-15: SQLite UPDATE...ORDER BY...LIMIT 1 语法错误 → 归因 metadata 永不落库
"""

import sqlite3
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from app.web_dashboard.order import executor as ex


# ── 测试基建 ──────────────────────────────────────────────────

class FakeBrokerCtx:
    def __init__(self, positions_df, fill_status="FILLED_ALL", fill_qty=0.0,
                 cancel_ok=True, place_ret_ok=True):
        self._positions = positions_df
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.cancel_ok = cancel_ok
        self.place_ret_ok = place_ret_ok
        self.place_calls = []
        self.cancel_calls = []

    def position_list_query(self, trd_env=None, acc_id=None):
        return 0, self._positions

    def place_order(self, **kw):
        self.place_calls.append(kw)
        if not self.place_ret_ok:
            return -1, "place failed"
        return 0, pd.DataFrame([{"order_id": "OID-1"}])

    def order_list_query(self, order_id=None, trd_env=None, acc_id=None):
        # 平仓单成交状态查询; 撤旧单复查也走这里 (P1-9)
        status = self.fill_status
        return 0, pd.DataFrame([{"order_id": order_id, "order_status": status,
                                 "dealt_qty": self.fill_qty}])


class FakeService:
    MAX_CLOSE_VALUE_USD = 10_000_000

    def __init__(self, broker):
        self._broker_ctx = broker
        self._connected = True
        self._close_lock = __import__("threading").Lock()
        self.calls = {"record": [], "mark_manual": [], "audit": []}

    def connect(self):
        return self._connected

    def _is_live(self):
        return False

    def _read_env(self, k, d=""):
        return d

    def _get_mode_label(self):
        return "SIM"

    def _fetch_dealt_avg_price(self, order_id, trd_env=None, acc_id=None, retries=0, delay=0):
        return 0.0

    def _calc_exit_price(self, row, raw_qty):
        return float(row.get("cur_price", 0) or 0)

    def _cancel_broker_order(self, order_id):
        self._broker_ctx.cancel_calls.append(order_id)
        return self._broker_ctx.cancel_ok

    def _record_manual_close(self, **kw):
        self.calls["record"].append(kw)

    def _mark_position_manual(self, symbol, direction):
        self.calls["mark_manual"].append(symbol)

    def _write_audit(self, order):
        self.calls["audit"].append(order.status)

    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE open_positions (symbol TEXT, thesis TEXT, "
                     "entry_context_json TEXT, entry_time TEXT)")
        return conn


def _pos_df(symbol="US.TEST", qty=100, side="LONG", cur_price=50.0):
    return pd.DataFrame([{"code": symbol, "qty": qty, "position_side": side,
                          "cost_price": 48.0, "cur_price": cur_price}])


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _no_scheduler(monkeypatch):
    import app.core.scheduler_core as _sc
    monkeypatch.setattr(_sc, "_scheduler_instance", None, raising=False)


@pytest.fixture
def _no_cancel_group(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order_group", lambda service, symbol: None)


# ── P1-8: 未成交平仓单不写假记录 ───────────────────────────────

def test_p1_8_unfilled_close_no_fake_record(monkeypatch):
    """限价平仓未成交 (SUBMITTED, dealt=0): 撤单 + 不写 trade_records + 返回失败"""
    broker = FakeBrokerCtx(_pos_df(), fill_status="SUBMITTED", fill_qty=0.0)
    svc = FakeService(broker)
    ret = ex.close_position(svc, "US.TEST", reason="test", close_qty=0, price=49.0)
    assert ret["success"] is False
    assert "未成交" in ret["error"]
    assert svc.calls["record"] == []          # 不写假 realized_pnl
    assert "OID-1" in broker.cancel_calls      # 残留挂单已撤 (防双通道平仓)
    assert svc.calls["mark_manual"] == []      # 仓位继续受策略监控


def test_p1_8_cancelled_close_no_record():
    broker = FakeBrokerCtx(_pos_df(), fill_status="CANCELLED", fill_qty=0.0)
    svc = FakeService(broker)
    ret = ex.close_position(svc, "US.TEST", reason="test", close_qty=0, price=49.0)
    assert ret["success"] is False
    assert svc.calls["record"] == []


def test_p1_8_filled_close_records_dealt_qty():
    broker = FakeBrokerCtx(_pos_df(qty=100), fill_status="FILLED_PART", fill_qty=40.0)
    svc = FakeService(broker)
    ret = ex.close_position(svc, "US.TEST", reason="test", close_qty=0)
    assert ret["success"] is True
    assert len(svc.calls["record"]) == 1
    assert svc.calls["record"][0]["quantity"] == 40   # 按实际成交数量记账
    assert svc.calls["mark_manual"] == []             # 部分成交 ≠ 全平, 不改判 manual


def test_p1_8_full_fill_marks_manual():
    broker = FakeBrokerCtx(_pos_df(qty=100), fill_status="FILLED_ALL", fill_qty=100.0)
    svc = FakeService(broker)
    ret = ex.close_position(svc, "US.TEST", reason="test", close_qty=0)
    assert ret["success"] is True
    assert svc.calls["mark_manual"] == ["US.TEST"]    # 实际全平才改判


# ── P1-7: 部分平仓后锁释放 ────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self.pos = SimpleNamespace(metadata={})
        self._has = True

    def has(self, s):
        return self._has

    def get(self, s):
        return self.pos


def test_p1_7_release_close_locks_clears_all_keys(monkeypatch):
    store = _FakeStore()
    store.pos.metadata = {"_close_in_progress": True, "_close_in_progress_ts": 1.0,
                          "_pending_close": True}
    sched = SimpleNamespace(position_store=store)
    monkeypatch.setattr("app.core.scheduler_core._scheduler_instance", sched)
    ex._release_close_locks("US.TEST")
    assert store.pos.metadata == {}          # 三键全清 → 组合止损恢复覆盖


def test_p1_7_partial_close_releases_locks(monkeypatch):
    """部分平仓成功路径必须释放锁 (旧行为: _pending_close 永久残留)"""
    store = _FakeStore()
    sched = SimpleNamespace(position_store=store)
    monkeypatch.setattr("app.core.scheduler_core._scheduler_instance", sched)
    broker = FakeBrokerCtx(_pos_df(qty=100), fill_status="FILLED_ALL", fill_qty=30.0)
    svc = FakeService(broker)
    ret = ex.close_position(svc, "US.TEST", reason="tp", close_qty=30)
    assert ret["success"] is True
    assert store.pos.metadata.get("_pending_close") is None
    assert store.pos.metadata.get("_close_in_progress") is None


# ── P1-9: modify_stop_loss 撤旧单失败 → fail-safe 中止 ─────────

def _mk_modify_service(old_sl_oid="SL-OLD"):
    broker = FakeBrokerCtx(_pos_df(qty=100), cancel_ok=False)
    svc = FakeService(broker)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE open_positions (symbol TEXT, current_sl REAL, "
                 "sl_broker_order_id TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO open_positions VALUES ('US.TEST', 45.0, ?, 'x')",
                 (old_sl_oid,))
    conn.commit()
    svc._db_real = conn
    svc._db_closed = False

    def _db():
        return _ConnWrapper(conn)

    svc._db = _db
    svc._ensure_sl_column = lambda: None
    return svc, broker


class _ConnWrapper:
    """close() 时不真关内存库 (多次 _db() 调用共享)"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a, **kw):
        return self._conn.execute(*a, **kw)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass


def _mk_live_partial_service(*, fill_qty=30.0, levels=(45.0, 55.0)):
    broker = FakeBrokerCtx(
        _pos_df(qty=100), fill_status="FILLED_PART", fill_qty=fill_qty
    )
    svc = FakeService(broker)
    svc._is_live = lambda: True
    svc._read_env = lambda key, default="": (
        "true" if key == "ALLOW_REAL_TRADING" else default
    )
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE open_positions (symbol TEXT, current_sl REAL, initial_tp REAL, "
        "sl_broker_order_id TEXT, tp_broker_order_id TEXT, thesis TEXT, "
        "entry_context_json TEXT, entry_time TEXT)"
    )
    if levels is not None:
        conn.execute(
            "INSERT INTO open_positions VALUES "
            "('US.TEST', ?, ?, 'SL-OLD', 'TP-OLD', 'thesis', '{}', '2026-09-16')",
            levels,
        )
    conn.commit()
    svc._db = lambda: _ConnWrapper(conn)
    svc._ensure_sl_column = lambda: None
    svc._trd_env = lambda: "REAL"
    svc._acc_id = lambda: 123
    return svc, broker


def test_live_partial_close_requires_db_protection_snapshot():
    svc, broker = _mk_live_partial_service(levels=None)
    ret = ex.close_position(
        svc, "US.TEST", reason="partial", close_qty=30
    )
    assert ret["success"] is False
    assert "SL/TP" in ret["error"]
    assert broker.place_calls == []


def test_live_partial_close_requires_positive_db_protection_levels():
    svc, broker = _mk_live_partial_service(levels=(0.0, 55.0))
    ret = ex.close_position(
        svc, "US.TEST", reason="partial", close_qty=30
    )
    assert ret["success"] is False
    assert "SL/TP" in ret["error"]
    assert broker.place_calls == []


def test_live_partial_close_rebuilds_from_open_positions_levels(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order_group", lambda service, symbol: None)
    svc, broker = _mk_live_partial_service(fill_qty=30.0)
    ret = ex.close_position(
        svc, "US.TEST", reason="partial", close_qty=30
    )
    assert ret["success"] is True
    assert len(broker.place_calls) == 3
    close_call, sl_call, tp_call = broker.place_calls
    assert close_call["qty"] == 30
    assert sl_call["qty"] == 70 and sl_call["aux_price"] == 45.0
    assert tp_call["qty"] == 70 and tp_call["price"] == 55.0


def test_p1_9_cancel_fail_unknown_status_aborts(monkeypatch):
    """撤旧 STOP 失败且旧单状态非 CANCEL → 不下新 STOP (防裸空头/双 STOP)"""
    svc, broker = _mk_modify_service()
    broker.fill_status = "FILLED_ALL"        # 旧单复查: 已成交 → 必须中止
    ret = ex.modify_stop_loss(svc, "US.TEST", 44.0)
    assert ret["success"] is False
    assert broker.place_calls == []          # 未下新 STOP


def test_p1_9_cancel_fail_confirmed_cancelled_proceeds(monkeypatch):
    """撤旧失败但复查确认已撤 (状态 CANCELLED) → 允许下新 STOP"""
    svc, broker = _mk_modify_service()
    broker.fill_status = "CANCELLED"
    ret = ex.modify_stop_loss(svc, "US.TEST", 44.0)
    # SIMULATE 环境不下 broker STOP, 但流程应走通且 success
    assert ret["success"] is True


def test_p1_9_cancel_ok_proceeds():
    svc, broker = _mk_modify_service()
    broker.cancel_ok = True
    ret = ex.modify_stop_loss(svc, "US.TEST", 44.0)
    assert ret["success"] is True


# ── P2-15: 归因 UPDATE 语句在 SQLite 下可执行 ─────────────────

def test_p2_15_attribution_update_executes_on_sqlite():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, "
                 "close_futu_order_id TEXT, metadata_json TEXT)")
    conn.execute("INSERT INTO trade_records (close_futu_order_id, metadata_json) "
                 "VALUES ('C1', NULL), ('C1', NULL), ('C2', 'keep')")
    conn.execute(
        "UPDATE trade_records SET metadata_json=? "
        "WHERE close_futu_order_id=? AND (metadata_json IS NULL OR metadata_json='')",
        ('{"attr":1}', 'C1'),
    )
    rows = conn.execute(
        "SELECT close_futu_order_id, metadata_json FROM trade_records ORDER BY id"
    ).fetchall()
    assert rows[0] == ("C1", '{"attr":1}')
    assert rows[1] == ("C1", '{"attr":1}')
    assert rows[2] == ("C2", "keep")          # 不覆盖已写行
    conn.close()


def test_p2_15_old_syntax_still_invalid_on_sqlite():
    """回归锚: 旧语法在本机 SQLite 仍抛错 (证明旧行为从不落库)"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER, k TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE t SET k=? WHERE id=? ORDER BY id DESC LIMIT 1", ("x", "y"))
    conn.close()
