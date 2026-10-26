"""实盘写路径服务端闸门契约 (RFC-168 方案 A/B, 2026-10-26)

钉住三件最容易回归的事:

  1. **方向性**: 收紧只作用在「开新仓」与「降低保护」; 平仓 / 撤单 / 增强止损
     必须保持可达 —— 拦住减仓才是真正的危险 (RFC-168 §提议方案 A 核心原则)。
  2. **归属校验**: 撤单/改止损须确认标的或订单属本账户。
  3. **开关**: 一行环境变量即可关闸回滚, 不必改 yaml (避开热重载 60s 延迟窗)。
"""
import sqlite3

import pytest


# ── 测试替身 ────────────────────────────────────────────────────────────────

class FakeService:
    """最小 ManualOrderService 替身 (只提供闸门用到的接口)。"""

    def __init__(self, position_rows=(), audit_order_ids=(), *, db_error=False):
        self._position_rows = list(position_rows)
        self._audit_order_ids = list(audit_order_ids)
        self._db_error = db_error
        self._broker_ctx = FakeBroker()
        self._order_lock = __import__("threading").Lock()

    def _db(self):
        if self._db_error:
            raise RuntimeError("db unavailable")
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL, "
            "current_sl REAL, avg_entry_price REAL)"
        )
        conn.execute("CREATE TABLE manual_order_audit (order_id TEXT)")
        for row in self._position_rows:
            conn.execute(
                "INSERT INTO open_positions VALUES (?,?,?,?,?)", row
            )
        for oid in self._audit_order_ids:
            conn.execute("INSERT INTO manual_order_audit VALUES (?)", (oid,))
        return conn

    def _is_live(self):
        return True

    def _trd_env(self):
        return "REAL"

    def _acc_id(self):
        return 1

    def connect(self):
        return True


class FakeBroker:
    def __init__(self, *, acc_ok=True, pos_ok=True, order_hit=False, order_exc=None):
        self.acc_ok = acc_ok
        self.pos_ok = pos_ok
        self.order_hit = order_hit
        self.order_exc = order_exc

    def accinfo_query(self, **kw):
        if not self.acc_ok:
            return 1, None
        return 0, _fake_df({"power": [100000]})

    def position_list_query(self, **kw):
        if not self.pos_ok:
            return 1, None
        return 0, _fake_df({"code": []})

    def order_list_query(self, **kw):
        if self.order_exc is not None:
            raise self.order_exc
        if self.order_hit:
            return 0, _fake_df({"order_id": ["X"]})
        return 0, _fake_df({"order_id": []})


class _fake_df:
    """极简 DataFrame 替身 (只需 empty / iloc / get)。"""

    def __init__(self, data):
        self._data = data
        self.empty = not any(data.values())

    def iloc(self, idx):
        return self

    def get(self, key, default=None):
        vals = self._data.get(key, [])
        return vals[0] if vals else default


@pytest.fixture(autouse=True)
def _no_push_out(monkeypatch):
    """闸门拒绝会推 hub; 测试里封住通道 (conftest 已封, 这里再封 push hub 本体)。"""
    from app.push import hub as hub_mod
    monkeypatch.setattr(hub_mod.PushHub, "push", lambda self, *a, **k: True, raising=False)


# ── 开关 ────────────────────────────────────────────────────────────────────

def test_gates_enabled_by_default(monkeypatch):
    from app.web_dashboard.order.write_gates import gate_enabled
    monkeypatch.delenv("FTSEEK_DASHBOARD_WRITE_GATES", raising=False)
    monkeypatch.delenv("FTSEEK_DASHBOARD_GATE_PLACE", raising=False)
    assert gate_enabled("PLACE") is True


def test_master_switch_disables_all(monkeypatch):
    from app.web_dashboard.order.write_gates import gate_enabled
    monkeypatch.setenv("FTSEEK_DASHBOARD_WRITE_GATES", "off")
    assert gate_enabled("PLACE") is False
    assert gate_enabled("CANCEL") is False


def test_per_gate_switch(monkeypatch):
    from app.web_dashboard.order.write_gates import gate_enabled
    monkeypatch.delenv("FTSEEK_DASHBOARD_WRITE_GATES", raising=False)
    monkeypatch.setenv("FTSEEK_DASHBOARD_GATE_PLACE", "off")
    assert gate_enabled("PLACE") is False
    assert gate_enabled("CANCEL") is True


# ── 归属校验 ────────────────────────────────────────────────────────────────

def test_symbol_held_allows_known_position():
    from app.web_dashboard.order.write_gates import check_symbol_held
    svc = FakeService(position_rows=[("US.AAPL", "LONG", 10, 100.0, 110.0)])
    ok, _ = check_symbol_held(svc, "US.AAPL")
    assert ok is True


def test_symbol_held_blocks_unknown_symbol():
    from app.web_dashboard.order.write_gates import check_symbol_held
    svc = FakeService(position_rows=[("US.AAPL", "LONG", 10, 100.0, 110.0)])
    ok, msg = check_symbol_held(svc, "US.NVDA")
    assert ok is False
    assert "US.NVDA" in msg


def test_symbol_held_fails_open_when_db_unreadable():
    """台账读不到 ⇒ 判据不可用。保护方向必须放行 (拦住改止损才是真危险)。"""
    from app.web_dashboard.order.write_gates import check_symbol_held
    svc = FakeService(db_error=True)
    ok, _ = check_symbol_held(svc, "US.AAPL")
    assert ok is True


# ── 止损方向校验 (核心: 只拦降低保护) ─────────────────────────────────────

def test_long_stop_moved_up_is_allowed():
    """多头止损上移 = 收紧保护 ⇒ 放行。"""
    from app.web_dashboard.order.write_gates import check_stop_loss_direction
    svc = FakeService(position_rows=[("US.AAPL", "LONG", 10, 100.0, 110.0)])
    ok, _ = check_stop_loss_direction(svc, "US.AAPL", 105.0)
    assert ok is True


def test_long_stop_moved_down_is_blocked():
    """多头止损下移 = 降低保护 ⇒ 拒绝。"""
    from app.web_dashboard.order.write_gates import check_stop_loss_direction
    svc = FakeService(position_rows=[("US.AAPL", "LONG", 10, 100.0, 110.0)])
    ok, msg = check_stop_loss_direction(svc, "US.AAPL", 95.0)
    assert ok is False
    assert "放宽保护" in msg


def test_short_stop_moved_up_is_blocked():
    """空头止损上移 = 降低保护 ⇒ 拒绝。"""
    from app.web_dashboard.order.write_gates import check_stop_loss_direction
    svc = FakeService(position_rows=[("US.TSLA", "SHORT", 10, 400.0, 390.0)])
    ok, msg = check_stop_loss_direction(svc, "US.TSLA", 405.0)
    assert ok is False
    assert "放宽保护" in msg


def test_short_stop_moved_down_is_allowed():
    """空头止损下移 = 收紧保护 ⇒ 放行。"""
    from app.web_dashboard.order.write_gates import check_stop_loss_direction
    svc = FakeService(position_rows=[("US.TSLA", "SHORT", 10, 400.0, 390.0)])
    ok, _ = check_stop_loss_direction(svc, "US.TSLA", 395.0)
    assert ok is True


def test_stop_beyond_entry_price_is_blocked():
    """止损越过入场价 = 触发即亏损离场 = 极端的降低保护。"""
    from app.web_dashboard.order.write_gates import check_stop_loss_direction
    svc = FakeService(position_rows=[("US.AAPL", "LONG", 10, 100.0, 110.0)])
    ok, msg = check_stop_loss_direction(svc, "US.AAPL", 112.0)
    assert ok is False
    assert "越过入场价" in msg


def test_weakened_stop_allowed_with_acknowledge():
    """用户显式确认后放行 —— 硬拒会挡住"规避噪音扫出"的正当诉求。"""
    from app.web_dashboard.order.write_gates import check_stop_loss_direction
    svc = FakeService(position_rows=[("US.AAPL", "LONG", 10, 100.0, 110.0)])
    ok, _ = check_stop_loss_direction(svc, "US.AAPL", 95.0, acknowledge=True)
    assert ok is True


# ── 撤单归属 ────────────────────────────────────────────────────────────────

def test_cancel_allowed_when_in_local_audit():
    from app.web_dashboard.order.write_gates import check_cancel_ownership
    svc = FakeService(audit_order_ids=["ORD-1"])
    ok, _ = check_cancel_ownership(svc, "ORD-1")
    assert ok is True


def test_cancel_allowed_when_broker_confirms_ownership():
    from app.web_dashboard.order.write_gates import check_cancel_ownership
    svc = FakeService()
    svc._broker_ctx.order_hit = True
    ok, _ = check_cancel_ownership(svc, "ORD-2")
    assert ok is True


def test_cancel_blocked_when_not_owned():
    from app.web_dashboard.order.write_gates import check_cancel_ownership
    svc = FakeService()
    svc._broker_ctx.order_hit = False
    ok, msg = check_cancel_ownership(svc, "ORD-X")
    assert ok is False
    assert "不属于本账户" in msg


def test_cancel_fails_open_when_broker_unverifiable():
    """broker 不可达时归属不可判定 ⇒ 放行 (撤单属减风险方向);
    且此时 cancel 本身的 RPC 同样会失败, 无实质风险。"""
    from app.web_dashboard.order.write_gates import check_cancel_ownership
    svc = FakeService()
    svc._broker_ctx.order_exc = RuntimeError("broker down")
    ok, _ = check_cancel_ownership(svc, "ORD-3")
    assert ok is True


# ── 开仓前置 (fail-close 方向) ────────────────────────────────────────────

def test_place_blocked_when_account_unavailable(monkeypatch):
    from app.web_dashboard.order.write_gates import precheck_place
    monkeypatch.delenv("FTSEEK_DASHBOARD_WRITE_GATES", raising=False)
    svc = FakeService()
    svc._broker_ctx.acc_ok = False
    ok, msg = precheck_place(svc)
    assert ok is False
    assert "账户" in msg


def test_place_blocked_when_positions_not_authoritative(monkeypatch):
    from app.web_dashboard.order.write_gates import precheck_place
    monkeypatch.delenv("FTSEEK_DASHBOARD_WRITE_GATES", raising=False)
    svc = FakeService()
    svc._broker_ctx.pos_ok = False
    ok, msg = precheck_place(svc)
    assert ok is False
    assert "持仓" in msg


def test_place_allowed_when_both_authoritative(monkeypatch):
    from app.web_dashboard.order.write_gates import precheck_place
    monkeypatch.delenv("FTSEEK_DASHBOARD_WRITE_GATES", raising=False)
    svc = FakeService()
    ok, _ = precheck_place(svc)
    assert ok is True


def test_place_disabled_by_switch(monkeypatch):
    from app.web_dashboard.order.write_gates import precheck_place
    monkeypatch.setenv("FTSEEK_DASHBOARD_WRITE_GATES", "off")
    svc = FakeService()
    svc._broker_ctx.acc_ok = False
    ok, _ = precheck_place(svc)
    assert ok is True


# ── 港股闸 ──────────────────────────────────────────────────────────────────

def test_hk_place_blocked_by_kill_switch(monkeypatch):
    from app.web_dashboard.order import write_gates
    monkeypatch.delenv("FTSEEK_DASHBOARD_WRITE_GATES", raising=False)
    import app.us_pipeline.emergency_interceptor as interceptor
    monkeypatch.setattr(interceptor, "check_kill_switch", lambda: True)
    ok, msg = write_gates.precheck_hk_place()
    assert ok is False
    assert "紧急停止" in msg
