r"""RFC-078 批 D · 回合台账(券商真值派生) 契约 (2026-09-08, 用户裁「需要，继续」).

为什么需要: 港股此前**没有任何回合级数据** (不进 `trade_records`, jsonl 只有 submit/cancel),
美股回合台账又断链过 3 个月 ⇒ 复盘只能看流水、看不到"一次进出赚亏多少、持有多久"。
批 D 从券商真值层 FIFO 派生回合 (市场中立), 港美同一套代码、同一条口径。

本文件钉的是**实现过程中真咬出来的问题**, 不是想象中的风险:
  · 咬1 **两份 FIFO**: 首版 `build_round_trips` 与既有 `fifo_realized` 各写一份配对 ⇒
    判据 3 (看板/周报/回合三处同一个数) 直接红: 69,188.36 vs 62,074.37。根因是**排除粒度**
    不同 (行级 vs 标的级)。现 `fifo_realized` **委派** `build_round_trips` = 一份实现两个视图。
  · 咬2 **胜率虚高**: 标的级排除会把亏损集中的老仓整段排掉 ⇒ 全量实测胜率 **96.4%**(27胜1负);
    美股段更极端: 38 回合计分 11, 9 笔亏损全落在被排除的 US.SNDK/US.BABA 里 ⇒ 该段 **100%**。
    一个漂亮的假胜率比没有胜率更坏 (它会进复盘结论并强化"我做对了"的错觉) ⇒ 加覆盖率闸门。
  · 咬3 **小样本比率**: 7 天窗口下美股只有 2 笔派生回合、覆盖率 100% ⇒ 覆盖率闸门放行,
    报告上就出现"胜 2/2 (100.0%)"。2 笔的 100% 与 50 笔的 100% 在纸面上长得一模一样 ⇒
    加样本闸门。计数是**事实**、比率是**推断**: 闸门不过就给计数、不给比率。
  · 咬4 **窗口截断误标**: 派生若按展示窗口切成交, 进场腿落在窗口外的**完整**回合会被误标成
    `INCOMPLETE_COST_BASIS` (盈亏变 NULL) ⇒ 派生必须用**全量**台账, 窗口只用于展示。
"""
from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.web_dashboard import live_ledger  # noqa: E402
import live_weekly_review as lwr  # noqa: E402

_CCY = {"HK": "HKD", "US": "USD"}
_DEAL_COLS = ("deal_id", "market", "currency", "symbol", "stock_name", "deal_market",
              "trd_side", "qty", "price", "amount", "fee", "deal_time", "deal_date",
              "counter_broker_id", "counter_broker_name", "status")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时 live 库 —— 本测试绝不得写生产 live 库."""
    db = tmp_path / "live.db"
    monkeypatch.setattr(live_ledger, "_db_path", lambda: db)
    monkeypatch.setattr(lwr, "_db_path", lambda: db)
    live_ledger.ensure_tables(db)
    return db


def _row(deal_id, symbol, side, qty, price, when, market=None, name="X"):
    mkt = market or ("HK" if symbol.startswith("HK.") else "US")
    return {"deal_id": str(deal_id), "market": mkt, "currency": _CCY[mkt],
            "symbol": symbol, "stock_name": name, "deal_market": mkt,
            "trd_side": side, "qty": float(qty), "price": float(price),
            "amount": round(float(qty) * float(price), 4), "fee": None,
            "deal_time": when, "deal_date": when[:10],
            "counter_broker_id": 1, "counter_broker_name": "C", "status": "OK"}


def _insert(db, rows):
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO live_deal_records ({', '.join(_DEAL_COLS)}) "
            f"VALUES ({', '.join('?' * len(_DEAL_COLS))})",
            [tuple(r[c] for c in _DEAL_COLS) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _strip(rows):
    return [{k: v for k, v in r.items() if k != "derived_at"} for r in rows]


# ══════════════════════════════════════════════════════════════════
#  判据 1 · 落点: 只在 live 库 (与 sim 数据收集仓物理隔离)
# ══════════════════════════════════════════════════════════════════
def test_round_trip_table_lands_in_live_db_only(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "live_round_trips" in tables, "派生回合表未建"
    # 载体必须是**看板专属 live 库**解析器, 不得随 TRADE_ENV 漂到 sim 库 (批 A 的判据同族)
    # 不用 inspect.getsource(_db_path): 夹具已 monkeypatch 成 lambda, 抓到的是替身源码
    src = (ROOT / "app" / "web_dashboard" / "live_ledger.py").read_text(encoding="utf-8")
    assert "from app.core.db_utils import get_dashboard_db" in src
    sim = ROOT / "data" / "ftseek_us.db"
    if sim.exists():
        c2 = sqlite3.connect(str(sim))
        sim_tables = {r[0] for r in c2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c2.close()
        assert "live_round_trips" not in sim_tables, \
            "派生回合表出现在 sim 数据收集仓 = 实盘/模拟隔离失效"


# ══════════════════════════════════════════════════════════════════
#  判据 2 · 确定性 + upsert 幂等
# ══════════════════════════════════════════════════════════════════
def test_derivation_is_deterministic_and_upsert_is_idempotent(tmp_db):
    _insert(tmp_db, [
        _row(1, "US.X", "BUY", 100, 10.0, "2026-08-01 10:00:00"),
        _row(2, "US.X", "SELL", 40, 12.0, "2026-08-02 10:00:00"),
        _row(3, "US.X", "SELL", 60, 11.0, "2026-08-03 10:00:00"),
    ])
    r1 = live_ledger.sync_round_trips()
    rows1 = live_ledger.read_round_trips()
    r2 = live_ledger.sync_round_trips()
    rows2 = live_ledger.read_round_trips()
    assert r1["status"] == "OK" and r1["inserted"] == 2 and r1["updated"] == 0
    assert r2["inserted"] == 0 and r2["updated"] == 2 and r2["pruned"] == 0, \
        "复跑必须是纯覆写 (派生确定性 ⇒ rt_id 稳定), 否则每跑一次就长一批重复回合"
    assert _strip(rows1) == _strip(rows2)
    assert len({r["rt_id"] for r in rows1}) == len(rows1) == 2
    # 一笔平仓吃掉两个批次 ⇒ 必须裂成两行且 rt_id 不撞 (seq 的存在理由)
    two_leg_exit = [r for r in rows1 if r["qty"] == 60]
    assert len(two_leg_exit) == 1 and rows1[0]["rt_id"] != rows1[1]["rt_id"]


# ══════════════════════════════════════════════════════════════════
#  判据 3 · 与 fifo_realized **逐字同一个数** (咬1 的钉子)
# ══════════════════════════════════════════════════════════════════
def test_round_trip_total_is_literally_the_fifo_total(tmp_db):
    """证伪: 把 summarize 的排除粒度改回**行级** ⇒ 本条转红 (首版就是这么红的)。"""
    _insert(tmp_db, [
        _row(1, "US.GOOD", "BUY", 100, 10.0, "2026-08-01 10:00:00"),
        _row(2, "US.GOOD", "SELL", 100, 12.0, "2026-08-02 10:00:00"),   # +200 完整
        _row(3, "US.BAD", "BUY", 50, 20.0, "2026-08-03 10:00:00"),
        _row(4, "US.BAD", "SELL", 80, 25.0, "2026-08-04 10:00:00"),     # 30 配对(+150) / 50 无腿
        _row(5, "HK.00001", "BUY", 200, 30.0, "2026-08-05 10:00:00"),
        _row(6, "HK.00001", "SELL", 200, 31.0, "2026-08-06 10:00:00"),  # +200 完整
    ])
    live_ledger.sync_round_trips()
    rows = live_ledger.read_round_trips()
    deals = live_ledger.read_all_deals()
    s = live_ledger.summarize_round_trips(rows)
    f = live_ledger.fifo_realized(deals)
    assert f["incomplete_basis_symbols"] == ["US.BAD"], "本用例必须含基准不完整标的, 否则钉不到排除粒度"
    assert s["pnl_gross_complete_basis_only"] == f["realized_gross_complete_basis_only"], \
        "回合合计与 fifo_realized 分家 = 同一页上会出现两个已实现盈亏"
    # 标的级排除的实质: US.BAD 那 30 股**配对完整**的 +150 也不计分
    row_level = round(sum(r["pnl_gross"] for r in rows if r["basis"] == "COMPLETE"), 2)
    assert row_level != s["pnl_gross_complete_basis_only"], \
        "两个口径相同 ⇒ 本用例已钉不住排除粒度 (需要一个'有完整行也有残缺行'的标的)"
    assert any(r["symbol"] == "US.BAD" and r["basis"] == "COMPLETE" for r in rows)


def test_fifo_realized_delegates_to_a_single_implementation():
    """一份 FIFO 实现、两个视图 — 不得再出现第二份配对循环."""
    src = inspect.getsource(live_ledger.fifo_realized)
    assert "build_round_trips(deals)" in src, "fifo_realized 必须委派 build_round_trips"
    assert "long_q" not in src and "short_q" not in src, \
        "fifo_realized 里仍有自己的多空队列 = 两份 FIFO (会给出两个不同的已实现盈亏)"


# ══════════════════════════════════════════════════════════════════
#  判据 4 · 基准不完整 ⇒ 盈亏 NULL 且不编基准
# ══════════════════════════════════════════════════════════════════
def test_incomplete_basis_rows_keep_pnl_null():
    deals = [_row(1, "US.X", "SELL", 30, 25.0, "2026-08-04 10:00:00")]   # 台账里没有开仓腿
    rt = live_ledger.build_round_trips(deals)
    assert rt["n_rows"] == 1, "残缺回合必须**留行** — 丢了会让平仓笔数与成交流水对不上"
    row = rt["rows"][0]
    assert row["basis"] == "INCOMPLETE_COST_BASIS"
    assert row["pnl_gross"] is None and row["pnl_pct"] is None
    assert row["entry_price"] is None and row["entry_time"] is None and row["entry_deal_id"] is None
    assert row["status"] == "CLOSED_NO_ENTRY_IN_LEDGER"
    assert row["qty"] == 30 and row["exit_price"] == 25.0, "出场腿是券商事实, 必须如实留"
    assert rt["realized_gross_complete_basis_only"] == 0.0
    assert rt["incomplete_basis_symbols"] == ["US.X"]


def test_partial_fill_splits_complete_and_incomplete_rows():
    deals = [_row(1, "US.X", "BUY", 50, 20.0, "2026-08-03 10:00:00"),
             _row(2, "US.X", "SELL", 80, 25.0, "2026-08-04 10:00:00")]
    rows = live_ledger.build_round_trips(deals)["rows"]
    assert len(rows) == 2
    ok = next(r for r in rows if r["basis"] == "COMPLETE")
    bad = next(r for r in rows if r["basis"] != "COMPLETE")
    assert ok["qty"] == 50 and ok["pnl_gross"] == 250.0
    assert bad["qty"] == 30 and bad["pnl_gross"] is None
    assert {r["rt_id"] for r in rows} == {"2#0", "2#1"}, "rt_id 必须由 exit_deal_id + seq 确定性生成"


# ══════════════════════════════════════════════════════════════════
#  判据 5 · 多空不混 (平多与平空盈亏符号相反)
# ══════════════════════════════════════════════════════════════════
def test_long_and_short_queues_are_not_netted():
    deals = [
        _row(1, "US.X", "BUY", 100, 10.0, "2026-08-01 10:00:00"),
        _row(2, "US.X", "SELL_SHORT", 50, 20.0, "2026-08-02 10:00:00"),
        _row(3, "US.X", "SELL", 100, 12.0, "2026-08-03 10:00:00"),      # 只吃 long: (12-10)*100
        _row(4, "US.X", "BUY_BACK", 50, 15.0, "2026-08-04 10:00:00"),   # 只吃 short: (20-15)*50
    ]
    rt = live_ledger.build_round_trips(deals)
    longs = [r for r in rt["rows"] if r["direction"] == "LONG"]
    shorts = [r for r in rt["rows"] if r["direction"] == "SHORT"]
    assert len(longs) == 1 and longs[0]["pnl_gross"] == 200.0
    assert len(shorts) == 1 and shorts[0]["pnl_gross"] == 250.0, \
        "空头盈亏必须是 (开仓−平仓)*qty; 与多头同式会把空单盈利报成亏损"
    assert not rt["incomplete_basis_symbols"], "两队列各自闭合 ⇒ 不得误报基准不完整"


def test_unknown_side_does_not_participate_in_pairing():
    deals = [_row(1, "US.X", "BUY", 10, 100.0, "2026-08-01 10:00:00"),
             _row(2, "US.X", "SELL_ALL", 10, 110.0, "2026-08-02 10:00:00")]
    rt = live_ledger.build_round_trips(deals)
    assert rt["rows"] == [] and rt["unknown_sides"] == {"SELL_ALL": 1}
    assert rt["open_lots"][0]["qty"] == 10, "未识别方向不得偷偷吃掉在仓批次"


# ══════════════════════════════════════════════════════════════════
#  判据 6 · exit_reason 恒不编造
# ══════════════════════════════════════════════════════════════════
def test_exit_reason_is_never_fabricated(tmp_db):
    """派生层只有**执行事实**; 决策理由无来源 ⇒ 恒 NULL + NOT_CAPTURED.

    证伪: 若有人按时间邻近把 jsonl/audit 的 reason 猜进来 (HK 63 行里 reason 非空仅 7 行),
    本条转红 —— 猜出来的理由会以"事实"的样子进 exit_reason 分布与复盘结论。
    """
    _insert(tmp_db, [_row(1, "HK.00700", "BUY", 100, 449.0, "2026-08-31 11:45:59.303"),
                     _row(2, "HK.00700", "SELL", 100, 485.0, "2026-09-02 10:05:00.000")])
    live_ledger.sync_round_trips()
    rows = live_ledger.read_round_trips()
    assert rows and all(r["exit_reason"] is None for r in rows)
    assert all(r["exit_reason_state"] == "NOT_CAPTURED" for r in rows)
    assert all(r["source"] == "broker_fifo" for r in rows)
    assert live_ledger.summarize_round_trips(rows)["exit_reason_not_captured"] == len(rows)


# ══════════════════════════════════════════════════════════════════
#  判据 7 · 未闭环批次必须可见 (0 回合 ≠ 无持仓)
# ══════════════════════════════════════════════════════════════════
def test_open_lots_are_surfaced_not_silently_dropped(tmp_db):
    _insert(tmp_db, [_row(1, "US.X", "BUY", 100, 10.0, "2026-08-01 10:00:00"),
                     _row(2, "US.X", "SELL", 40, 12.0, "2026-08-02 10:00:00")])
    rt = live_ledger.build_round_trips(live_ledger.read_all_deals())
    assert rt["n_rows"] == 1
    assert len(rt["open_lots"]) == 1 and rt["open_lots"][0]["qty"] == 60
    assert rt["open_lots"][0]["direction"] == "LONG" and rt["open_lots"][0]["entry_price"] == 10.0

    live_ledger.sync_round_trips()
    panel = live_ledger.round_trip_panel("US", "2026-08-01", "2026-08-31")
    assert panel["state"] == "OK" and panel["open_lots_n"] == 1
    assert panel["open_lots_symbols"] == ["US.X"]
    # 只有开仓、本窗口零了结 ⇒ 状态必须自陈"真没了结", 且仍报在仓批次
    panel2 = live_ledger.round_trip_panel("US", "2026-09-01", "2026-09-30")
    assert panel2["state"] == "NO_ROUNDS_IN_WINDOW"
    assert panel2["open_lots_n"] == 1, "零回合时更要报在仓批次, 否则被读成'没有持仓'"
    assert panel2["ledger_rows"] == 1


def test_panel_states_are_three_not_two(tmp_db):
    panel = live_ledger.round_trip_panel("HK", "2026-08-01", "2026-08-31")
    assert panel["state"] == "NO_DATA", "台账一行回合都没有 ⇒ NO_DATA (还没派生过)"
    _insert(tmp_db, [_row(1, "HK.00001", "BUY", 100, 10.0, "2026-08-01 10:00:00"),
                     _row(2, "HK.00001", "SELL", 100, 11.0, "2026-08-02 10:00:00")])
    live_ledger.sync_round_trips()
    assert live_ledger.round_trip_panel("HK", "2026-08-01", "2026-08-31")["state"] == "OK"
    assert live_ledger.round_trip_panel("HK", "2026-09-01", "2026-09-30")["state"] == \
        "NO_ROUNDS_IN_WINDOW"


# ══════════════════════════════════════════════════════════════════
#  判据 3/咬4 · 派生用全量台账, 窗口只用于展示
# ══════════════════════════════════════════════════════════════════
def test_derivation_uses_full_ledger_not_the_display_window(tmp_db):
    deals = [_row(1, "US.X", "BUY", 10, 100.0, "2026-07-01 10:00:00"),   # 进场在展示窗口外
             _row(2, "US.X", "SELL", 10, 110.0, "2026-09-01 10:00:00")]  # 出场在窗口内
    _insert(tmp_db, deals)
    live_ledger.sync_round_trips()
    rows = live_ledger.read_round_trips("2026-08-25", "2026-09-07", market="US")
    assert len(rows) == 1 and rows[0]["basis"] == "COMPLETE" and rows[0]["pnl_gross"] == 100.0
    # 证伪: 按窗口切成交再派生 ⇒ 同一笔被误标成基准不完整、盈亏变 NULL (真钱盈亏凭空消失)
    truncated = live_ledger.build_round_trips([d for d in deals if d["deal_date"] >= "2026-08-25"])
    assert truncated["rows"][0]["basis"] == "INCOMPLETE_COST_BASIS"
    assert truncated["rows"][0]["pnl_gross"] is None


def test_empty_ledger_does_not_prune_existing_round_trips(tmp_db):
    """台账空 ⇒ NO_DATA 且**不剪枝**: 把"没同步上"变成"没交易"比留着旧派生更坏."""
    _insert(tmp_db, [_row(1, "US.X", "BUY", 10, 100.0, "2026-08-01 10:00:00"),
                     _row(2, "US.X", "SELL", 10, 110.0, "2026-08-02 10:00:00")])
    assert live_ledger.sync_round_trips()["inserted"] == 1
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("DELETE FROM live_deal_records")
    conn.commit()
    conn.close()
    r = live_ledger.sync_round_trips()
    assert r["status"] == "NO_DATA" and r.get("pruned", 0) == 0
    assert "不是**零回合" in r["detail"] or "不是零回合" in r["detail"]
    assert len(live_ledger.read_round_trips()) == 1, "旧派生行被清空 = 把'没同步'伪装成'没交易'"


def test_prune_removes_rows_whose_deals_disappeared(tmp_db):
    """派生确定性 ⇒ 成交行被更正后, 旧 rt_id 必须被剪掉 (否则留着幽灵回合)."""
    _insert(tmp_db, [_row(1, "US.X", "BUY", 10, 100.0, "2026-08-01 10:00:00"),
                     _row(2, "US.X", "SELL", 10, 110.0, "2026-08-02 10:00:00")])
    live_ledger.sync_round_trips()
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("DELETE FROM live_deal_records WHERE deal_id = '2'")
    conn.commit()
    conn.close()
    r = live_ledger.sync_round_trips()
    assert r["pruned"] == 1 and r["total_rows"] == 0


# ══════════════════════════════════════════════════════════════════
#  咬2/咬3 · 胜率双闸门 (覆盖率 + 样本量)
# ══════════════════════════════════════════════════════════════════
def _mk(n_wins, extra_incomplete_symbol=False):
    deals = []
    i = 1
    for k in range(n_wins):
        deals.append(_row(i, f"US.W{k}", "BUY", 10, 100.0, "2026-08-01 10:00:00")); i += 1
        deals.append(_row(i, f"US.W{k}", "SELL", 10, 110.0, "2026-08-02 10:00:00")); i += 1
    if extra_incomplete_symbol:
        # 该标的有配对完整的赢行 + 一条无腿的平仓 ⇒ 整段被排除 (覆盖率被拉低)
        deals.append(_row(i, "US.BAD", "BUY", 10, 100.0, "2026-08-01 10:00:00")); i += 1
        deals.append(_row(i, "US.BAD", "SELL", 20, 90.0, "2026-08-02 10:00:00")); i += 1
    return live_ledger.summarize_round_trips(live_ledger.build_round_trips(deals)["rows"])


def test_win_rate_is_gated_on_sample_size():
    """2 笔的 100% 与 50 笔的 100% 纸面上长得一样 ⇒ 小样本不给比率, 只给计数."""
    s = _mk(2)
    assert s["n_scored"] == 2 and s["scored_coverage"] == 1.0
    assert s["win_rate_state"] == "SMALL_SAMPLE" and s["win_rate_pct"] is None
    assert (s["wins"], s["losses"]) == (2, 0), "计数是事实, 必须照给"
    ok = _mk(5)
    assert ok["win_rate_state"] == "OK" and ok["win_rate_pct"] == 100.0


def test_win_rate_is_gated_on_coverage():
    """标的级排除往往排掉的正是亏损行 ⇒ 覆盖率不足时给比率会得到漂亮但假的胜率."""
    s = _mk(2, extra_incomplete_symbol=True)
    assert s["incomplete_basis_symbols"] == ["US.BAD"]
    assert s["n"] == 4 and s["n_scored"] == 2 and s["scored_coverage"] == 0.5
    assert s["win_rate_state"] == "INSUFFICIENT_COVERAGE" and s["win_rate_pct"] is None
    # US.BAD 的**亏损**行被排除后剩下的全是赢的 ⇒ 照算就是 100% (这正是不能给的原因)
    assert s["wins"] == 2 and s["losses"] == 0


def test_win_rate_states_cover_the_empty_cases():
    assert live_ledger.summarize_round_trips([])["win_rate_state"] == "NO_DATA"
    deals = [_row(1, "US.X", "SELL", 10, 100.0, "2026-08-01 10:00:00")]   # 只有无腿平仓
    s = live_ledger.summarize_round_trips(live_ledger.build_round_trips(deals)["rows"])
    assert s["win_rate_state"] == "NO_SCORED_ROW" and s["win_rate_pct"] is None


# ══════════════════════════════════════════════════════════════════
#  持有时长: 必须按各市场时区 (跨 DST 裸减会差 1 小时)
# ══════════════════════════════════════════════════════════════════
def test_holding_hours_respects_dst_and_market_tz():
    """跨 DST 的美股回合必须是**绝对时刻**差 (23h), 不是墙钟差 (24h).

    证伪: 把实现改回 `exit_dt - entry_dt` ⇒ 本条转红 —— CPython 对"同一个 tzinfo 对象"
    的两个 aware datetime 相减时**忽略 utcoffset**, 而同市场的进出场腿正是同一个 tz 对象
    (2026-03-08 02:00 ET 进夏令时; 实测直接相减 24.0h, `timestamp()` 差 23.0h)。
    """
    us = live_ledger.build_round_trips([
        _row(1, "US.X", "BUY", 1, 10.0, "2026-03-07 09:00:00"),      # EST (UTC-5)
        _row(2, "US.X", "SELL", 1, 11.0, "2026-03-08 09:00:00")])    # EDT (UTC-4) ⇒ 真值 23h
    assert us["rows"][0]["holding_hours"] == 23.0, "美股跨 DST 用墙钟差会得 24h"
    hk = live_ledger.build_round_trips([
        _row(3, "HK.00001", "BUY", 1, 10.0, "2026-03-07 09:00:00"),
        _row(4, "HK.00001", "SELL", 1, 11.0, "2026-03-08 09:00:00")])
    assert hk["rows"][0]["holding_hours"] == 24.0, "HKT 无 DST ⇒ 同尺 24h"


def test_unparsable_deal_time_leaves_holding_null_not_guessed():
    rows = live_ledger.build_round_trips([
        _row(1, "US.X", "BUY", 1, 10.0, "2026-08-01 10:00:00"),
        _row(2, "US.X", "SELL", 1, 11.0, "不是时间")])["rows"]   # 出场腿时刻不可解析
    assert len(rows) == 1
    assert rows[0]["holding_hours"] is None, "解析失败必须留空, 不得猜一个数"
    assert rows[0]["pnl_gross"] == 1.0, "盈亏与时刻无关, 仍应如实给"


# ══════════════════════════════════════════════════════════════════
#  判据 8 · 周报两段都出派生回合, 且两层不得互相冒充
# ══════════════════════════════════════════════════════════════════
def _render(market, drt):
    empty = {"state": "NO_DATA", "detail": "x"}
    return "\n".join(lwr._render_market(market, empty, empty, empty, None,
                                        window={"start": "2026-09-01", "end": "2026-09-07",
                                                "tz": "ET" if market == "US" else "HKT"},
                                        drt=drt))


def test_weekly_report_renders_derived_section_for_both_markets(tmp_db):
    _insert(tmp_db, [
        _row(1, "HK.00700", "BUY", 100, 449.0, "2026-08-31 11:45:59.303"),
        _row(2, "HK.00700", "SELL", 100, 485.0, "2026-09-02 10:05:00.000"),
        _row(3, "US.MU", "BUY", 40, 937.65, "2026-09-02 09:40:00.000"),
        _row(4, "US.MU", "SELL", 40, 965.0, "2026-09-04 10:05:00.000"),
    ])
    live_ledger.sync_round_trips()
    lots = live_ledger.build_round_trips(live_ledger.read_all_deals())["open_lots"]
    for market, tz in (("HK", "HKT"), ("US", "ET")):
        sec = lwr.derived_roundtrip_section(market, "2026-09-01", "2026-09-07", lots)
        assert sec["state"] == "OK" and sec["source"] == "broker_fifo(live_round_trips)"
        txt = _render(market, sec)
        assert "回合(券商真值派生 FIFO" in txt, f"{market} 段缺派生回合小节"
        assert f"出场{tz}日归集" in txt, "窗口尺度必须写出来 (两市场日历不同)"
        assert "决策理由未捕获" in txt, "派生层没有决策理由, 必须自陈"
        assert "标的级" in txt and "不含手续费" in txt, "毛口径与排除粒度必须写明"
    hk_txt = _render("HK", lwr.derived_roundtrip_section("HK", "2026-09-01", "2026-09-07", lots))
    us_txt = _render("US", lwr.derived_roundtrip_section("US", "2026-09-01", "2026-09-07", lots))
    # 两层不得互相冒充 ⇒ 报告里派生段的标题必须**自带来源**(券商真值派生), 与决策台账段
    # (`回合台账(trade_records, …)`) 在字面上就可区分, 不靠读者猜
    assert "券商真值派生" in hk_txt and "券商真值派生" in us_txt
    assert "broker_fifo" in lwr.derived_roundtrip_section(
        "HK", "2026-09-01", "2026-09-07", lots)["source"]
    rt_us = lwr.roundtrip_section("2026-09-01", "2026-09-07")
    assert rt_us["state"] in ("OK", "NO_TRADES_IN_WINDOW", "NO_DATA", "UNAVAILABLE")


def test_weekly_report_states_why_win_rate_is_missing(tmp_db):
    """不给胜率时必须在「已知缺口」里说清楚为什么不给 — 否则读者只会以为漏印了一行."""
    _insert(tmp_db, [_row(1, "US.X", "BUY", 10, 100.0, "2026-09-02 10:00:00"),
                     _row(2, "US.X", "SELL", 10, 110.0, "2026-09-04 10:00:00")])
    live_ledger.sync_round_trips()
    us = {"roundtrip": {"state": "NO_DATA", "ledger_total_live_rows": 0, "ledger_span": [None, None]},
          "deals": {"state": "NO_DATA"}, "positions": {"state": "NO_DATA"},
          "derived_rt": lwr.derived_roundtrip_section("US", "2026-09-01", "2026-09-07", [])}
    hk = {"deals": {"state": "NO_DATA"}, "positions": {"state": "NO_DATA"},
          "derived_rt": {"state": "NO_DATA", "summary": {}}}
    txt = _render("US", us["derived_rt"])
    assert "不给胜率" in txt and "SMALL_SAMPLE" not in txt, "状态码不得直出给读者, 要说人话"
    gaps = "\n".join(lwr.collect_gaps(us, hk))
    assert "不给胜率" in gaps and "小样本" in gaps


def test_derived_section_reports_unavailable_not_zero(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("no such table: live_round_trips")
    monkeypatch.setattr(live_ledger, "read_round_trips", boom)
    sec = lwr.derived_roundtrip_section("HK", "2026-09-01", "2026-09-07", [])
    assert sec["state"] == "UNAVAILABLE" and "不是**零回合" in sec["detail"]


# ══════════════════════════════════════════════════════════════════
#  同页两个"已实现" ⇒ 必须标口径 + 不等时解释 (2026-09-08 实测咬出)
# ══════════════════════════════════════════════════════════════════
def _render_with_deals(market, start, end, lots=None):
    deals = lwr.deal_section(market, start, end)
    drt = lwr.derived_roundtrip_section(market, start, end, lots or [])
    empty = {"state": "NO_DATA", "detail": "x"}
    txt = "\n".join(lwr._render_market(
        market, deals, empty, empty, None,
        window={"start": start, "end": end, "tz": "HKT" if market == "HK" else "ET"},
        drt=drt))
    return txt, deals, drt


def test_report_labels_both_pairing_calibers_when_they_disagree(tmp_db):
    """跨窗持仓 ⇒ 两种配对口径必然不等 ⇒ 报告必须标口径并解释, 且指明以哪个为准.

    实测原型: 港股 30 天窗口下成交层 HK$49,200 vs 回合层 HK$59,775。
    """
    _insert(tmp_db, [_row(1, "HK.00700", "BUY", 100, 449.0, "2026-08-20 10:00:00"),
                     _row(2, "HK.00700", "SELL", 100, 485.0, "2026-09-03 10:00:00")])
    live_ledger.sync_round_trips()
    txt, deals, drt = _render_with_deals("HK", "2026-09-01", "2026-09-07")
    assert deals["state"] == "OK" and drt["state"] == "OK"
    # 证伪前置: 两种口径此时必须**真的**不等, 否则本条钉的是空话
    assert deals["fifo"]["realized_gross_complete_basis_only"] == 0.0, \
        "窗口内只有平仓腿 ⇒ 窗口配对口径应把该标的整段排除"
    assert drt["summary"]["pnl_gross_complete_basis_only"] == 3600.0
    assert "仅用本窗口成交配对" in txt and "全量台账配对" in txt
    assert "不相等" in txt and "窗口口径偏低" in txt and "为准" in txt


def test_no_reconcile_note_when_the_two_calibers_agree(tmp_db):
    """相等时不得喊狼: 无差别地贴"两个数不等"的警告会把真警告稀释成噪声."""
    _insert(tmp_db, [_row(1, "HK.00700", "BUY", 100, 449.0, "2026-09-02 10:00:00"),
                     _row(2, "HK.00700", "SELL", 100, 485.0, "2026-09-03 10:00:00")])
    live_ledger.sync_round_trips()
    txt, deals, drt = _render_with_deals("HK", "2026-09-01", "2026-09-07")
    assert deals["fifo"]["realized_gross_complete_basis_only"] == \
        drt["summary"]["pnl_gross_complete_basis_only"] == 3600.0
    assert "不相等" not in txt
    assert "仅用本窗口成交配对" in txt and "全量台账配对" in txt, "口径标注是常驻的, 与是否相等无关"


def test_win_rate_state_enum_is_not_leaked_to_readers(tmp_db):
    """状态码是给程序的; 报告/看板必须说人话 (实测首版把 INSUFFICIENT_COVERAGE 直出了)."""
    _insert(tmp_db, [_row(1, "US.BAD", "SELL", 20, 90.0, "2026-09-02 10:00:00")])
    live_ledger.sync_round_trips()
    txt, _, drt = _render_with_deals("US", "2026-09-01", "2026-09-07")
    assert drt["summary"]["win_rate_state"] == "INSUFFICIENT_COVERAGE" or \
        drt["summary"]["win_rate_state"] == "NO_SCORED_ROW"
    assert "INSUFFICIENT_COVERAGE" not in txt and "NO_SCORED_ROW" not in txt
    assert "不给胜率" in txt
