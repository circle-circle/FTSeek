r"""RFC-078 批B · 实盘券商成交流水层 + 持仓快照层契约 (2026-09-08).

背景: 实盘账本此前有两处洞 —— ①美股 `trade_records` 因 `_record_manual_close` 委派签名
漂移断链 2026-06-19~09-08 (券商已平仓、账本零记录, 已于 33d5568c 修但历史不可本地回补);
②港股**完全不进 DB** (只有 jsonl 的 submit/cancel 留痕, 无成交价/量/盈亏), 而券商侧
港股是真钱真有仓 (2026-09-08 实测总资产 HK$715,975.88)。批B 补一层**券商真值账本**。

本文件钉的是**实现过程中真踩过的坑**, 不是想象中的风险:
  · 坑1 跨市场污染: 首版建 `hk_trade_records` 并假定"港股 ctx 只返回港股成交"。实测为假
    —— 综合账户下一次 `history_deal_list_query` 返回 99 笔里 HK 47 + **US 52**, 且首版把
    `currency` 硬编码 HKD ⇒ 美股成交被记进港股表且币种错记。
  · 坑2 deal_id 精度: 实测形如 4657813206881206341 (19 位 > 2^53), 经 float 一转静默变位
    ⇒ 幂等键失效 ⇒ 同一笔反复入账。
  · 坑3 读错契约: `get_positions_with_status()` 返回 `{"rows":…,"error":…}` 而不是
    `{"ok":…,"positions":…}`; 首版按后者读, **拿着 3 个真持仓报 UNAVAILABLE**。
  · 坑4 进程不退出: futu OpenD 上下文持非守护线程, 不 disconnect 则 cron 跑完也不退
    (实测被 timeout 杀, exit=124)。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.web_dashboard import live_ledger  # noqa: E402

_HK_MOD = "app.web_dashboard.hk_order_service.get_hk_order_service"
_US_MOD = "app.web_dashboard.manual_order_service.get_manual_order_service"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把台账指到临时库 —— 本测试绝不得写生产 live 库."""
    db = tmp_path / "live_ledger_test.db"
    monkeypatch.setattr(live_ledger, "_db_path", lambda: db)
    live_ledger.ensure_tables(db)
    return db


def _deal(deal_id, code, side, qty, price, when, market=None):
    """构造一行富途 deal (13 列实测形状; market 缺省时按 code 前缀, 用于制造"无 deal_market"场景)."""
    row = {"code": code, "stock_name": "X", "deal_market": market,
           "deal_id": deal_id, "order_id": f"O{deal_id}", "qty": float(qty),
           "price": float(price), "trd_side": side, "create_time": when,
           "counter_broker_id": 6997, "counter_broker_name": "中国投资(沪港通)",
           "status": "OK", "jp_acc_type": "N/A"}
    if market is None:
        row["deal_market"] = "HK" if code.startswith("HK.") else "US"
    return row


class _Svc:
    """替身券商服务: deals=成交查询返回, positions=持仓返回, 记录 disconnect 调用."""

    def __init__(self, deals=None, deals_error=None, connect_ok=True,
                 positions=None, positions_error=None, track=None,
                 disconnect_error=None):
        self._deals, self._deals_error = deals, deals_error
        self._connect_ok = connect_ok
        self._positions, self._positions_error = positions, positions_error
        self._track = track if track is not None else []
        self._disconnect_error = disconnect_error
        # 记录每次成交查询的 start/end: 用来钉"窗口按各市场本地日取"
        self.calls: list = []
        outer = self

        class _Ctx:
            def history_deal_list_query(self, **kw):
                outer.calls.append(kw)
                if outer._deals_error:
                    return 1, outer._deals_error
                return 0, pd.DataFrame(outer._deals or [])

            def position_list_query(self, **kw):
                if outer._positions_error:
                    return 1, outer._positions_error
                return 0, pd.DataFrame(outer._positions or [])

        self._broker_ctx = _Ctx()

    def connect(self):
        return self._connect_ok

    def disconnect(self):
        self._track.append("disconnect")
        if self._disconnect_error:
            raise self._disconnect_error

    def _acc_id(self):
        return 1

    def _trd_env(self):
        from futu import TrdEnv
        return TrdEnv.REAL

    # 港股服务契约: {"rows": [...], "error": None|str}
    def get_positions_with_status(self):
        if self._positions_error:
            return {"rows": [], "error": self._positions_error}
        return {"rows": self._positions or [], "error": None}


def _wire(monkeypatch, hk=None, us=None):
    monkeypatch.setattr(_HK_MOD, lambda: hk if hk is not None else _Svc())
    monkeypatch.setattr(_US_MOD, lambda: us if us is not None else _Svc())


# ══════════════════════════════════════════════════════════════════
#  1. 建表 / 归属库
# ══════════════════════════════════════════════════════════════════
def test_ensure_tables_is_idempotent(tmp_db):
    live_ledger.ensure_tables(tmp_db)
    live_ledger.ensure_tables(tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"live_deal_records", "live_position_snapshots"} <= names


def test_ledger_lives_in_dashboard_db_not_sim_db():
    """实盘台账必须与美股实盘**同库**(live), 与 sim 数据收集仓物理隔离.

    证伪: 把 _db_path 改回 get_main_db() ⇒ TRADE_ENV=1 下指向 sim 库 ⇒ 本条转红。
    """
    from app.core.db_utils import get_dashboard_db, get_main_db
    assert live_ledger._db_path() == get_dashboard_db()
    if get_dashboard_db() != get_main_db():
        assert live_ledger._db_path().name == "ftseek_us_live.db"


def test_tables_are_market_neutral_not_hk_named():
    """表名必须市场中立 (坑1): 叫 hk_* 的表装进美股成交 = 命名即谎言."""
    assert "hk_trade_records" not in live_ledger._DDL_DEALS
    assert "market" in live_ledger._DDL_DEALS
    pk = [c.strip() for c in live_ledger._DDL_SNAPSHOTS.split("PRIMARY KEY (")[1].split(")")[0].split(",")]
    assert [p.strip() for p in pk] == ["snapshot_date", "market", "symbol"]


# ══════════════════════════════════════════════════════════════════
#  2. 坑1: 跨市场污染 + 币种
# ══════════════════════════════════════════════════════════════════
def test_hk_ctx_returning_us_deals_is_tagged_us_not_hk(tmp_db, monkeypatch):
    """**坑1 回归钉**: 港股 ctx 返回的美股成交必须标 market='US' + currency='USD'.

    实测: 综合账户下 HK ctx 一次返回 99 笔 = HK 47 + US 52。首版全记成港股且币种
    硬编码 HKD ⇒ 跨市场污染 + 金额跨币种相加。
    证伪: market/currency 改成由"哪个腿返回的"决定 ⇒ 本条转红。
    """
    mixed = [
        _deal("1111111111111111111", "HK.02513", "BUY", 100, 998.0, "2026-09-08 09:45:00.839"),
        _deal("2222222222222222222", "US.TSLA", "BUY", 50, 360.0, "2026-09-04 21:32:27.062"),
    ]
    _wire(monkeypatch, hk=_Svc(deals=mixed), us=_Svc(deals=[]))
    r = live_ledger.sync_deals(days=60)
    assert r["status"] in ("OK", "PARTIAL"), r
    conn = sqlite3.connect(str(tmp_db))
    try:
        got = dict(conn.execute("SELECT symbol, market || '/' || currency FROM live_deal_records"))
    finally:
        conn.close()
    assert got == {"HK.02513": "HK/HKD", "US.TSLA": "US/USD"}, f"跨市场污染或币种错记: {got}"


def test_market_derived_from_row_not_from_leg(tmp_db, monkeypatch):
    """market 由**每行自带的 deal_market** 推导; 两腿返回重叠集合只落一行 (幂等键 deal_id)."""
    same = _deal("3333333333333333333", "HK.00700", "BUY", 100, 440.0, "2026-09-08 10:00:00")
    _wire(monkeypatch, hk=_Svc(deals=[same]), us=_Svc(deals=[dict(same)]))
    r = live_ledger.sync_deals(days=60)
    assert r["fetched_unique"] == 1, "两腿重叠必须去重成一笔"
    assert r["total_rows"] == 1
    assert live_ledger.market_of("HK", "US.X") == "HK", "deal_market 优先于 symbol 前缀"
    assert live_ledger.market_of(None, "US.X") == "US"
    assert live_ledger.market_of(None, "JP.7203") == "OTHER"


def test_unknown_market_leaves_currency_null(tmp_db, monkeypatch):
    """未知市场 ⇒ currency 留 NULL, 不按 HKD/USD 猜 (猜错会让金额跨币种相加)."""
    odd = _deal("4444444444444444444", "JP.7203", "BUY", 100, 3000.0,
                "2026-09-08 10:00:00", market="JP")
    _wire(monkeypatch, hk=_Svc(deals=[odd]), us=_Svc(deals=[]))
    live_ledger.sync_deals(days=60)
    conn = sqlite3.connect(str(tmp_db))
    try:
        cur = conn.execute("SELECT currency FROM live_deal_records").fetchone()[0]
    finally:
        conn.close()
    assert cur is None


# ══════════════════════════════════════════════════════════════════
#  3. 坑2: deal_id 19 位精度 + 幂等
# ══════════════════════════════════════════════════════════════════
def test_deal_id_is_text_and_preserves_19_digits(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    try:
        typ = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(live_deal_records)")}["deal_id"]
        assert typ.upper() == "TEXT", f"deal_id 必须是 TEXT, 实测 {typ}"
        real_id = "4657813206881206341"
        assert str(int(float(real_id))) != real_id, "样例已失去证伪力 (需换 >2^53 的 id)"
        pk = [r[1] for r in conn.execute("PRAGMA table_info(live_deal_records)") if r[5]]
        assert pk == ["deal_id"]
    finally:
        conn.close()


def test_upsert_by_deal_id_is_idempotent(tmp_db, monkeypatch):
    """同一 deal_id 回补两次 ⇒ 仍 1 行, 且逐位保真 (不是 float 变位值)."""
    row = _deal(4657813206881206341, "HK.00100", "BUY", 20, 361.0, "2026-09-07 13:10:46.606")
    _wire(monkeypatch, hk=_Svc(deals=[row]), us=_Svc(deals=[]))
    r1, r2 = live_ledger.sync_deals(days=7), live_ledger.sync_deals(days=7)
    assert r1["status"] == "OK" and r1["inserted"] == 1, r1
    assert r2["status"] == "OK" and r2["inserted"] == 0 and r2["updated"] == 1, r2
    assert r2["total_rows"] == 1, "重复入账 ⇒ 幂等键失效"
    conn = sqlite3.connect(str(tmp_db))
    try:
        got = conn.execute("SELECT deal_id FROM live_deal_records").fetchone()[0]
    finally:
        conn.close()
    assert got == "4657813206881206341", f"deal_id 变位: {got}"


def test_fee_is_null_not_zero(tmp_db):
    """富途 deal 级**不返回手续费** ⇒ fee 必须 NULL, 不得伪造 0.

    伪造 0 会让周报把"未知费用"读成"零费用", 直接高估净收益 (本仓已有
    "毛 edge 修到 0 仍净亏" 的费用判决书, 费用口径不能含糊)。
    """
    conn = sqlite3.connect(str(tmp_db))
    try:
        conn.execute("INSERT INTO live_deal_records (deal_id, market, symbol, qty, price, amount,"
                     " fee) VALUES ('d1','HK','HK.00700',100,440.0,44000.0,NULL)")
        conn.commit()
        fee = conn.execute("SELECT fee FROM live_deal_records WHERE deal_id='d1'").fetchone()[0]
    finally:
        conn.close()
    assert fee is None
    assert live_ledger.fifo_realized([])["fee_included"] is False


# ══════════════════════════════════════════════════════════════════
#  4. 三态: 绝不把"没连上"伪装成"零成交/空仓"
# ══════════════════════════════════════════════════════════════════
def test_sync_deals_unavailable_when_both_legs_down(tmp_db, monkeypatch):
    _wire(monkeypatch, hk=_Svc(connect_ok=False), us=_Svc(connect_ok=False))
    r = live_ledger.sync_deals(days=7)
    assert r["status"] == "UNAVAILABLE"
    assert sorted(r["unavailable_legs"]) == ["HK", "US"]
    assert "不是" in r["detail"]


def test_no_deals_is_a_distinct_state(tmp_db, monkeypatch):
    """窗口内真空窗 ⇒ NO_DEALS (与 UNAVAILABLE 必须可区分)."""
    _wire(monkeypatch, hk=_Svc(deals=[]), us=_Svc(deals=[]))
    assert live_ledger.sync_deals(days=7)["status"] == "NO_DEALS"


def test_one_leg_down_is_partial_and_other_leg_still_lands(tmp_db, monkeypatch):
    """一条腿挂掉不得掩盖另一条: PARTIAL + 好腿数据照常入库."""
    _wire(monkeypatch,
          hk=_Svc(deals=[_deal("5555555555555555555", "HK.00700", "BUY", 100, 440.0,
                               "2026-09-08 10:00:00")]),
          us=_Svc(connect_ok=False))
    r = live_ledger.sync_deals(days=7)
    assert r["status"] == "PARTIAL"
    assert r["unavailable_legs"] == ["US"]
    assert r["total_rows"] == 1 and r["per_market"]["HK"] == 1


def test_query_error_is_unavailable_not_empty(tmp_db, monkeypatch):
    """券商返回 RET_ERROR ⇒ UNAVAILABLE 带原始 detail, 不得吞成空列表."""
    _wire(monkeypatch, hk=_Svc(deals_error="connection lost"), us=_Svc(deals=[]))
    r = live_ledger.sync_deals(days=7)
    assert r["legs"]["HK"]["status"] == "UNAVAILABLE"
    assert "connection lost" in r["legs"]["HK"]["detail"]


# ══════════════════════════════════════════════════════════════════
#  5. 坑3/坑4: 持仓快照契约 + 必须断开
# ══════════════════════════════════════════════════════════════════
_HK_POS = [{"symbol": "HK.02513", "name": "智谱", "qty": 400.0, "can_sell_qty": 400.0,
            "cost_price": 1000.75, "cur_price": 916.0, "market_val": 366400.0,
            "pl_val": -33900.0, "pl_ratio": -8.47, "position_side": "LONG"}]
_US_POS = [{"code": "US.TSLA", "stock_name": "Tesla", "qty": 50.0, "can_sell_qty": 50.0,
            "cost_price": 360.0, "nominal_price": 355.55, "market_val": 17777.5,
            "pl_val": -222.5, "pl_ratio": -1.24, "position_side": "LONG"},
           {"code": "HK.00700", "stock_name": "腾讯", "qty": 200.0, "can_sell_qty": 200.0,
            "cost_price": 442.9, "nominal_price": 435.4, "market_val": 87080.0,
            "pl_val": -1500.0, "pl_ratio": -1.69, "position_side": "LONG"}]


def test_snapshot_reads_rows_error_contract(tmp_db, monkeypatch):
    """**坑3 回归钉**: 拿到真持仓时不得报 UNAVAILABLE.

    证伪: 把实现改回读 res.get("ok") / res.get("positions") ⇒ 本条转红。
    """
    _wire(monkeypatch, hk=_Svc(positions=_HK_POS), us=_Svc(positions=_US_POS))
    r = live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
    assert r["status"] == "OK", f"拿着真持仓却报失败: {r}"
    assert r["per_market"] == {"US": 1, "HK": 1}, "美股腿必须只认 US.* (综合账户返回全市场)"
    conn = sqlite3.connect(str(tmp_db))
    try:
        got = conn.execute("SELECT market, symbol, currency, pl_val "
                           "FROM live_position_snapshots ORDER BY market").fetchall()
    finally:
        conn.close()
    assert got == [("HK", "HK.02513", "HKD", -33900.0), ("US", "US.TSLA", "USD", -222.5)]


def test_snapshot_unavailable_propagates_error(tmp_db, monkeypatch):
    _wire(monkeypatch, hk=_Svc(positions_error="broker_not_connected"),
          us=_Svc(positions_error="broker_not_connected"))
    r = live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
    assert r["status"] == "UNAVAILABLE"
    assert "不是" in r["detail"]


def test_snapshot_distinguishes_leg_down_from_flat(tmp_db, monkeypatch):
    """`per_market[m] = -1` (本腿未更新) 必须与 `0` (真的空仓) 严格区分."""
    _wire(monkeypatch, hk=_Svc(positions=[]), us=_Svc(positions_error="down"))
    r = live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
    assert r["status"] == "PARTIAL"
    assert r["per_market"] == {"US": -1, "HK": 0}


def test_snapshot_always_disconnects(tmp_db, monkeypatch):
    """**坑4 回归钉**: 无论成败都必须 disconnect, 否则 futu 非守护线程让 cron 跑完不退出."""
    for kwargs in ({"positions": _HK_POS}, {"positions_error": "broker_not_connected"}):
        track: list = []
        svc = _Svc(track=track, **kwargs)
        _wire(monkeypatch, hk=svc, us=svc)
        live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
        assert "disconnect" in track, f"未断开连接: {kwargs}"


def test_snapshot_upsert_idempotent_per_day_and_market(tmp_db, monkeypatch):
    _wire(monkeypatch, hk=_Svc(positions=_HK_POS), us=_Svc(positions=_US_POS))
    live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
    live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
    conn = sqlite3.connect(str(tmp_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM live_position_snapshots "
                         "WHERE snapshot_date='2026-09-08'").fetchone()[0]
    finally:
        conn.close()
    assert n == 2, "同日重复快照不得堆行"


# ══════════════════════════════════════════════════════════════════
#  6. FIFO 已实现盈亏: 宁缺毋假
# ══════════════════════════════════════════════════════════════════
def _d(sym, side, qty, price, when):
    return {"symbol": sym, "trd_side": side, "qty": qty, "price": price,
            "deal_time": when, "deal_id": f"{sym}-{when}"}


def test_fifo_long_round_trip_complete_basis():
    """完整回合: 买 100@10 → 卖 100@12 ⇒ 毛已实现 +200 (不含费)."""
    r = live_ledger.fifo_realized([
        _d("HK.00700", "BUY", 100, 10.0, "2026-09-01 10:00:00"),
        _d("HK.00700", "SELL", 100, 12.0, "2026-09-03 10:00:00"),
    ])
    s = r["per_symbol"]["HK.00700"]
    assert s["realized_gross"] == 200.0 and s["basis"] == "COMPLETE"
    assert r["realized_gross_complete_basis_only"] == 200.0
    assert r["incomplete_basis_symbols"] == []


def test_fifo_short_round_trip_sign_is_correct():
    """空头: 卖空 100@12 → 买回 100@10 ⇒ +200 (成本−买回价, 符号不得与多头搞反)."""
    r = live_ledger.fifo_realized([
        _d("US.MU", "SELL_SHORT", 100, 12.0, "2026-09-01 10:00:00"),
        _d("US.MU", "BUY_BACK", 100, 10.0, "2026-09-03 10:00:00"),
    ])
    assert r["per_symbol"]["US.MU"]["realized_gross"] == 200.0
    assert r["realized_gross_complete_basis_only"] == 200.0


def test_fifo_partial_fill_uses_earliest_lot_first():
    """分批建仓分批出: FIFO 必须按最早批次配对, 不是按均价."""
    r = live_ledger.fifo_realized([
        _d("HK.02513", "BUY", 100, 1000.0, "2026-09-01 10:00:00"),
        _d("HK.02513", "BUY", 100, 1100.0, "2026-09-02 10:00:00"),
        _d("HK.02513", "SELL", 150, 1200.0, "2026-09-03 10:00:00"),
    ])
    s = r["per_symbol"]["HK.02513"]
    # 先配 100@1000 (+20000), 再配 50@1100 (+5000) ⇒ +25000; 均价法会给 +22500
    assert s["realized_gross"] == 25000.0, s
    assert s["basis"] == "COMPLETE"
    assert s["open_qty_remaining"] == 50.0


def test_fifo_incomplete_cost_basis_is_excluded_not_fabricated():
    """**核心诚实性钉桩**: 平仓量超过台账可见开仓量 ⇒ 标 INCOMPLETE_COST_BASIS 且
    **不计入合计** —— 不得用现价/均价编一个成本基准 (编出来的数会直接进复盘结论).

    场景真实存在: 台账首日 2026-07-10, 而 HK.02513 的 400 股是更早建的仓。
    """
    r = live_ledger.fifo_realized([
        _d("HK.02513", "BUY", 100, 1000.0, "2026-09-01 10:00:00"),
        _d("HK.02513", "SELL", 300, 1200.0, "2026-09-03 10:00:00"),
        _d("US.TSLA", "BUY", 50, 360.0, "2026-09-01 10:00:00"),
        _d("US.TSLA", "SELL", 50, 370.0, "2026-09-04 10:00:00"),
    ])
    assert r["incomplete_basis_symbols"] == ["HK.02513"]
    hk = r["per_symbol"]["HK.02513"]
    assert hk["basis"] == "INCOMPLETE_COST_BASIS"
    assert hk["unmatched"] == 200.0 and hk["matched"] == 100.0
    # 合计**只**含成本基准完整的 US.TSLA (+500), 不含 HK.02513 的任何数
    assert r["realized_gross_complete_basis_only"] == 500.0
    assert r["fee_included"] is False
    assert "毛" in r["caveat"]


def test_fifo_reports_unknown_sides_instead_of_dropping_them():
    """出现未见过的 trd_side ⇒ 计数上报, 不静默丢弃 (静默丢 = 少算成交)."""
    r = live_ledger.fifo_realized([_d("HK.00700", "SELL_SHORT_CFD", 10, 1.0, "2026-09-01 10:00:00")])
    assert r["unknown_sides"] == {"SELL_SHORT_CFD": 1}


# ══════════════════════════════════════════════════════════════════
#  7. 只读查询 + 覆盖面自检
# ══════════════════════════════════════════════════════════════════
def _seed(tmp_db, rows):
    conn = sqlite3.connect(str(tmp_db))
    try:
        for mkt, sym, d in rows:
            conn.execute("INSERT INTO live_deal_records (deal_id, market, currency, symbol,"
                         " deal_date, deal_time, trd_side, qty, price, amount)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (f"{sym}-{d}", mkt, "HKD" if mkt == "HK" else "USD", sym, d,
                          f"{d} 10:00:00", "BUY", 100, 10.0, 1000.0))
        conn.commit()
    finally:
        conn.close()


def test_read_deals_window_inclusive_and_market_filtered(tmp_db):
    """闭区间 + 市场过滤; 纯本地 ⇒ 券商挂了周报照样能出 (只标注口径)."""
    _seed(tmp_db, [("HK", "HK.00700", "2026-09-01"), ("HK", "HK.00700", "2026-09-05"),
                   ("HK", "HK.00700", "2026-09-08"), ("US", "US.TSLA", "2026-09-03")])
    assert [r["deal_date"] for r in live_ledger.read_deals("2026-09-01", "2026-09-05")] == \
        ["2026-09-01", "2026-09-03", "2026-09-05"]
    hk = live_ledger.read_deals("2026-09-01", "2026-09-08", market="HK")
    assert {r["symbol"] for r in hk} == {"HK.00700"} and len(hk) == 3


def test_latest_snapshot_uses_most_recent_day_not_today(tmp_db):
    """取"最近一个有快照的日"而非"今天" ⇒ 周末/假日不会误报空仓."""
    conn = sqlite3.connect(str(tmp_db))
    try:
        for day in ("2026-09-04", "2026-09-05"):
            conn.execute("INSERT INTO live_position_snapshots (snapshot_date, market, symbol,"
                         " qty) VALUES (?,?,?,?)", (day, "HK", "HK.00700", 200.0))
        conn.commit()
    finally:
        conn.close()
    got = live_ledger.latest_snapshot("HK")
    assert [r["snapshot_date"] for r in got] == ["2026-09-05"]
    assert live_ledger.latest_snapshot("HK", on_or_before="2026-09-04")[0]["snapshot_date"] == "2026-09-04"
    assert live_ledger.latest_snapshot("US") == []


def test_coverage_reports_empty_ledger_honestly(tmp_db):
    """空台账必须报 0 行 + None 跨度, 不得抛错也不得伪造日期."""
    cov = live_ledger.ledger_coverage()
    assert cov["deals"]["HK"]["rows"] == 0 and cov["deals"]["HK"]["first"] is None
    assert cov["snapshots"]["US"]["days"] == 0
    assert cov["deals"]["fee_null_rows"] == 0


def test_coverage_exposes_the_gap_it_was_built_to_prevent(tmp_db):
    """覆盖面自检的意义: "某市场零留痕" 必须一行查询就能发现 (港股此前正是这样消失的)."""
    _seed(tmp_db, [("US", "US.TSLA", "2026-09-03")])
    cov = live_ledger.ledger_coverage()
    assert cov["deals"]["US"]["rows"] == 1
    assert cov["deals"]["HK"]["rows"] == 0, "港股零留痕必须显式可见, 不得被美股数据掩盖"


# ══════════════════════════════════════════════════════════════════
#  7. 时间尺度 (2026-09-08 补正): 窗口/快照日按**各市场本地日**, synced_at 用 UTC
#     病灶原型: 首版三处用不带尺的裸 now = 服务器 CST 墙钟 (UTC+8, 比 ET 快 12~13h),
#     被 tests/test_naive_datetime_now_ratchet.py 咬出来后逐点核 = **真缺陷而非风格问题**:
#     美股腿的查询窗尾会被推成 ET 的"明天"、当日美股仓位会被标成 ET 次日。
# ══════════════════════════════════════════════════════════════════
def _freeze_clocks(monkeypatch, cst_iso="2026-09-09T00:30:00+08:00",
                   et_iso="2026-09-08T12:30:00-04:00"):
    """钉一个"CST 已入 09-09 凌晨、ET 仍是 09-08 中午"的真实瞬间 (每天 ET 12:00 后都是)."""
    from datetime import datetime

    monkeypatch.setattr(live_ledger, "now_cst", lambda: datetime.fromisoformat(cst_iso))
    monkeypatch.setattr(live_ledger, "now_et", lambda: datetime.fromisoformat(et_iso))


def test_deal_window_is_each_market_local_day(tmp_db, monkeypatch):
    """**回归钉**: 两腿各拿自己本地日的窗口; 共用服务器 CST 日会把美股窗口整体推后一天.

    证伪: 把 window_for 改回不区分市场 (统一 now_cst) ⇒ 美股 end 变 2026-09-09 ⇒ 本条转红。
    """
    _freeze_clocks(monkeypatch)
    hk, us = _Svc(), _Svc()
    _wire(monkeypatch, hk=hk, us=us)
    r = live_ledger.sync_deals(days=7)
    assert r["windows"]["HK"] == ("2026-09-02", "2026-09-09")
    assert r["windows"]["US"] == ("2026-09-01", "2026-09-08")
    assert hk.calls and hk.calls[0]["end"] == "2026-09-09", f"港股腿窗口: {hk.calls}"
    assert us.calls and us.calls[0]["end"] == "2026-09-08", \
        f"美股腿窗尾被 CST 推成了 ET 的明天: {us.calls}"


def test_window_pads_one_extra_day_start(tmp_db, monkeypatch):
    """窗口是"日"粒度且 upsert 幂等 ⇒ 起始多放宽 1 天只多覆盖边界日, 不漏整天成交."""
    _freeze_clocks(monkeypatch)
    _wire(monkeypatch)
    start, end = live_ledger.window_for("US", 7)
    assert (end, start) == ("2026-09-08", "2026-09-01")
    assert live_ledger._TZ_LABEL == {"HK": "HKT", "US": "ET"}


def test_snapshot_default_date_is_per_market_local(tmp_db, monkeypatch):
    """不传 snapshot_date 时两腿各按本地日落行 (主键含 market ⇒ 允许不同日).

    证伪: 改回统一 CST 日 ⇒ 美股行落到 2026-09-09 ⇒ 本条转红。
    """
    _freeze_clocks(monkeypatch)
    _wire(monkeypatch, hk=_Svc(positions=_HK_POS), us=_Svc(positions=_US_POS))
    r = live_ledger.sync_position_snapshots()
    assert r["snapshot_dates"] == {"HK": "2026-09-09", "US": "2026-09-08"}
    assert r["snapshot_date"] == "2026-09-09", "运行标签取较晚的 HK 本地日 (HK 日恒 ≥ ET 日)"
    assert r["rows_today"] == 2 and r["per_market"] == {"US": 1, "HK": 1}
    conn = sqlite3.connect(str(tmp_db))
    try:
        got = conn.execute("SELECT market, symbol, snapshot_date FROM live_position_snapshots "
                           "ORDER BY market").fetchall()
    finally:
        conn.close()
    assert got == [("HK", "HK.02513", "2026-09-09"), ("US", "US.TSLA", "2026-09-08")]


def test_explicit_snapshot_date_still_applies_to_both_legs(tmp_db, monkeypatch):
    """显式传日期 = 复算/回归入口, 必须压过本地日推导 (否则历史窗口无法重放)."""
    _freeze_clocks(monkeypatch)
    _wire(monkeypatch, hk=_Svc(positions=_HK_POS), us=_Svc(positions=_US_POS))
    r = live_ledger.sync_position_snapshots(snapshot_date="2026-08-01")
    assert r["snapshot_dates"] == {"HK": "2026-08-01", "US": "2026-08-01"}
    assert r["snapshot_date"] == "2026-08-01"


def test_synced_at_is_utc_labeled_not_server_wall_clock(tmp_db, monkeypatch):
    """审计时刻存 UTC 带偏移: naive 串与 deal_date(市场本地日) 混在一库里迟早被当同尺比较."""
    _freeze_clocks(monkeypatch)
    _wire(monkeypatch, hk=_Svc(deals=[_deal("4657813206881206341", "HK.00700", "BUY",
                                            100, 440.0, "2026-09-08 10:00:00")]), us=_Svc())
    r = live_ledger.sync_deals(days=30)
    assert r["synced_at"].endswith("+00:00"), f"synced_at 不带 UTC 偏移: {r['synced_at']}"
    conn = sqlite3.connect(str(tmp_db))
    try:
        stored = conn.execute("SELECT synced_at FROM live_deal_records").fetchone()[0]
    finally:
        conn.close()
    assert stored.endswith("+00:00") and stored == r["synced_at"]


def test_disconnect_failure_is_logged_not_swallowed(tmp_db, monkeypatch, caplog):
    """**坑4 的留痕面**: 断开失败必须能在日志里查到 (except:pass 会吞掉"cron 不退出"的线索).

    证伪: 把 finally 里的 except 改回 pass ⇒ caplog 无记录 ⇒ 本条转红。
    """
    svc = _Svc(positions=_HK_POS, disconnect_error=RuntimeError("OpenD 连接已断"))
    _wire(monkeypatch, hk=svc, us=svc)
    with caplog.at_level("WARNING", logger="ftseek_us.live_ledger"):
        r = live_ledger.sync_position_snapshots(snapshot_date="2026-09-08")
    assert r["status"] == "OK", "清理路径的异常不得冒泡成同步失败"
    assert "disconnect" in caplog.text, f"断开失败被静默吞掉: {caplog.text}"
    assert "OpenD 连接已断" in caplog.text


def test_summarize_deals_direction_semantics():
    """开/平方向只有一份定义: `SELL_SHORT` 是**开空**(计入开仓额)、`BUY_BACK` 是平空.

    证伪: 把 SELL_SHORT 挪进 CLOSE_SIDES ⇒ 开/平仓额互换、净现金流符号反 ⇒ 本条转红。
    """
    deals = [
        {"symbol": "US.MU", "trd_side": "BUY", "amount": 1000.0, "fee": None},
        {"symbol": "US.MU", "trd_side": "SELL", "amount": 1100.0, "fee": None},
        {"symbol": "US.TSLA", "trd_side": "SELL_SHORT", "amount": 500.0, "fee": None},
        {"symbol": "US.TSLA", "trd_side": "BUY_BACK", "amount": 400.0, "fee": None},
        {"symbol": "HK.00700", "trd_side": "SOME_NEW_SIDE", "amount": 77.0, "fee": None},
    ]
    s = live_ledger.summarize_deals(deals)
    assert s["open_amount"] == 1500.0, s
    assert s["close_amount"] == 1500.0, s
    assert s["net_cash"] == 0.0, s
    assert s["unknown_side_rows"] == 1, "未知方向只计数上报, 不得猜成开或平"
    assert s["n"] == 5 and s["symbols"] == 3
    assert s["fee_rows_null"] == 5 and s["amount_caliber"] == "gross_deal_amount_no_fee"
    assert "SELL_SHORT" in live_ledger.OPEN_SIDES and "BUY_BACK" in live_ledger.CLOSE_SIDES
