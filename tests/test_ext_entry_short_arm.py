#!/usr/bin/env python3
"""延长时段**空侧方向参数** (RFC-099, 2026-09-23 用户令「先把代码写好」).

设计原则 (回答用户「做空和做多为什么还需要单独写? 能否复用已有链路」): **不新开链路**。
共用: 候选/事件源 · 价/市值/换手/价差/深度/费用/漂移闸 · 档位 · 仓位算法 · 日内帽/在仓帽 ·
      在途单门 · 券商对账 · 成交等待与撤单 · 孤儿单 · 09:25 收口 · 离场窗 · 退出链 (已有 _dir_sign)。
方向耦合只 4 处: ① 下单口 trd_side ② 落库 direction ③ 带判据 ④ SL 方向 —— 本文件逐个钉住。

安全边界 (默认 OFF): `EXT_ENTRY_ALLOW_SHORT=false` ⇒ `_direction` 恒 LONG、`place_order` 恒 BUY。
本文件所有用例均打桩, **永不触达券商** (2026-09-22 真下单事故纪律)。
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
from futu import RET_OK, Session, TrdSide  # noqa: E402
#   券商桩必须返回真 RET_OK=0 (否则 ret != RET_OK 被判失败); TrdSide/Session 在 _execute 内局部导入,
#   不挂在模块上 ⇒ 用例直接引 futu 常量。


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Row:
    def __init__(self, d):
        self._d = d or {}

    def get(self, k, default=None):
        return self._d.get(k, default)


class _DF:
    """最小 DataFrame 替身: 只需 `.iloc[0].get(...)` 与 `.empty`。"""

    def __init__(self, row):
        self.empty = row is None
        self._row = row

    @property
    def iloc(self):
        return [_Row(self._row)]


class _Series:
    def __init__(self, v):
        self.values = [v]


class FakeTradeContext:
    """记录型券商桩: 任何 place_order 都被记下, 但不出网。"""

    def __init__(self, short_able=5, permit=True, ret_ok=RET_OK):
        self.orders = []
        self.closed = False
        self.ret_ok = ret_ok
        self.short_able, self.permit = short_able, permit

    def place_order(self, **kw):
        self.orders.append(kw)
        return (self.ret_ok, {"order_id": _Series(12345)})

    def acctradinginfo_query(self, **kw):
        return (self.ret_ok, _DF({"max_sell_short": self.short_able}))

    def get_margin_ratio(self, codes):
        return (self.ret_ok, _DF({"is_short_permit": self.permit}))

    def close(self):
        self.closed = True


def _stub(mod, monkeypatch, tmp_path, ctx, *, short_allow=False, opened=None):
    """把 _execute 的**非方向**依赖全部打桩 (券商口用记录桩), 只留方向逻辑被真正执行。"""
    monkeypatch.setattr(mod, "SHORT_ALLOW", short_allow)
    monkeypatch.setattr(mod, "DB", tmp_path / "t.db")
    monkeypatch.setattr(mod, "_PUSH_DEDUP", tmp_path / "dedup.json")
    monkeypatch.setattr(mod, "_open_trade_context", lambda: ctx)
    monkeypatch.setattr(mod, "_reconcile_broker_entry_orders", lambda *a, **k: ([], []))
    monkeypatch.setattr(mod, "_probe_env_only", lambda: False)
    monkeypatch.setattr(mod, "_probe_mode", lambda: False)
    monkeypatch.setattr(mod, "_audit", lambda rec: None)
    monkeypatch.setattr(mod, "_push", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_push_filled", lambda *a, **k: None)
    monkeypatch.setattr(mod, "wait_order_fill", lambda *a, **k: {
        "filled": True, "filled_qty": 10, "avg_fill_price": 100.0,
        "cancel_ok": True, "status": "FILLED_ALL"})

    def _fake_write_open_position(symbol, qty, entry_px, decision_px, session, earn, chg, vol,
                                 buy_order_id="", position_usd=None, grade="B", structure=None,
                                 gate_mode="strict", side="LONG"):
        if opened is not None:
            opened.append({"symbol": symbol, "side": side, "qty": qty, "entry_px": entry_px})
        return "EXT-testpos"

    monkeypatch.setattr(mod, "_write_open_position", _fake_write_open_position)

    class _Mode:
        is_live = False

        def account_id(self):
            return 19930823

        def futu_trd_env(self):
            return "SIMULATE"

    monkeypatch.setattr(mod, "_mode", lambda: _Mode())


def _exec(mod, side="LONG"):
    return mod._execute("US.PTN", 10, 100.0, "premarket", 4.0, 2_000_000,
                        {"surprise_pct": 3.0}, False, side=side)


# ── ① 默认 OFF: 空侧不可达 ────────────────────────────────────────────────────
def test_short_disabled_never_reaches_place_order(tmp_path, monkeypatch):
    """`EXT_ENTRY_ALLOW_SHORT` 默认 false ⇒ 即使被显式要求做空也**不得**下单。"""
    mod = _load()
    ctx = FakeTradeContext()
    _stub(mod, monkeypatch, tmp_path, ctx, short_allow=False)
    r = _exec(mod, side="SHORT")
    assert r["skip"] == "short_not_enabled", r
    assert ctx.orders == [], f"默认 OFF 竟下了单: {ctx.orders}"


def test_default_long_path_still_buy(tmp_path, monkeypatch):
    """回归钉: 默认配置下多侧仍是 BUY (行为 byte 级不变)。"""
    mod = _load()
    ctx = FakeTradeContext()
    opened = []
    _stub(mod, monkeypatch, tmp_path, ctx, short_allow=False, opened=opened)
    r = _exec(mod, side="LONG")
    assert r.get("ok") is True, r
    assert ctx.orders and ctx.orders[0]["trd_side"] == TrdSide.BUY
    assert ctx.orders[0]["remark"] == "ext_entry"
    assert opened and opened[0]["side"] == "LONG"


# ── ② 开了也要券源过关 ────────────────────────────────────────────────────────
def test_short_blocks_when_borrow_unavailable(tmp_path, monkeypatch):
    """允许做空但券源不足 (max_sell_short=0) ⇒ 拦下, 不下单。"""
    mod = _load()
    ctx = FakeTradeContext(short_able=0, permit=True)
    _stub(mod, monkeypatch, tmp_path, ctx, short_allow=True)
    r = _exec(mod, side="SHORT")
    assert r["skip"] == "short_borrow_unavailable", r
    assert r["max_sell_short"] == 0
    assert ctx.orders == []


def test_short_blocks_when_no_short_permit(tmp_path, monkeypatch):
    """券源够但 `is_short_permit=False` ⇒ 同样拦下 (与探针同一判据)。"""
    mod = _load()
    ctx = FakeTradeContext(short_able=5, permit=False)
    _stub(mod, monkeypatch, tmp_path, ctx, short_allow=True)
    r = _exec(mod, side="SHORT")
    assert r["skip"] == "short_borrow_unavailable", r
    assert r["is_short_permit"] is False
    assert ctx.orders == []


# ── ③ 开 + 券源过关 ⇒ 复用同一条下单/落库链, 只是方向不同 ─────────────────────
def test_short_reuses_same_chain_with_sell_short(tmp_path, monkeypatch):
    mod = _load()
    ctx = FakeTradeContext(short_able=5, permit=True)
    opened = []
    _stub(mod, monkeypatch, tmp_path, ctx, short_allow=True, opened=opened)
    r = _exec(mod, side="SHORT")
    assert r.get("ok") is True, r
    o = ctx.orders[0]
    assert o["trd_side"] == TrdSide.SELL_SHORT
    assert o["remark"] == "ext_entry"           # 与多侧同一命名空间 ⇒ 在途单门照旧保护
    assert o["session"] == Session.ETH
    assert o["qty"] == 10 and o["price"] == 100.0
    assert r["side"] == "SHORT"
    assert opened and opened[0]["side"] == "SHORT", "落库方向必须随侧走"
    # 空头 SL 在上方 (STOP_PCT=-5 ⇒ ×1.05), 多侧才是下方
    assert r["initial_sl"] == round(100.0 * 1.05, 2), r["initial_sl"]


def test_long_initial_sl_below_entry(tmp_path, monkeypatch):
    mod = _load()
    ctx = FakeTradeContext()
    _stub(mod, monkeypatch, tmp_path, ctx, short_allow=False)
    r = _exec(mod, side="LONG")
    assert r["initial_sl"] == round(100.0 * 0.95, 2), r["initial_sl"]


# ── ④ 规则契约钉 (防回退到"用财报好坏定方向") ────────────────────────────────
def test_direction_comes_from_magnitude_not_surprise():
    """RFC-099 §8 禁项 1: 方向只能来自**幅度**, 不得来自 beat/miss。"""
    import inspect

    mod = _load()
    src = inspect.getsource(mod.collect)
    assert 'change_pct >= SHORT_GAP_MIN' in src, "方向判据必须读幅度 (跳空)"
    assert 'if _direction == "LONG" and (' in src, "财报正负门必须只作用于多侧"
    assert "surprise_pct\"]] > 0 and" not in src.replace(" ", ""), "不得把 surprise 用于定方向"


def test_inserts_are_direction_parameterized():
    """落库契约: 两处 INSERT 的 direction 必须是绑定参数, 不得再把 'LONG' 写死。"""
    mod = _load()
    import inspect

    src = inspect.getsource(mod._write_open_position)
    assert "'LONG', ?" not in src, "open_positions/trade_records 的 direction 仍写死"
    assert "side" in inspect.signature(mod._write_open_position).parameters