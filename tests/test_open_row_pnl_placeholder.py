"""未平仓行 realized_pnl 占位污染下游 — 回归测试 (2026-08-28).

背景 (生产数据异常复核, 逐条实测):
  trade_store.save_trade 对"仅开仓"的行写 realized_pnl=0.0 占位, 而下游普遍用
  "realized_pnl IS NOT NULL" 作为**已平仓**的代理判据 → 在途仓位伪装成"一笔保本平仓":
    ① position_sync external_close 查重守卫命中该仓位的开仓行 → 真实券商平仓被跳过
       INSERT + 跳过通知, 而 open_positions 的 DELETE 照常执行 → 平仓彻底消失;
       (2026-08-21 建守卫时为拦 BULL id=1320/1327 双重记账, 当时零测试)
    ② pre_entry_gate._query_history 胜率被系统性压低 (现网 8/28 实测 US.AVGO 7d 报
       83.3% 而真值 100%; US.MU/US.PDD 7d 报"1 笔 0% 胜率", 实际无任何已了结历史)
       → LLM 入场前门拿到悲观历史, 误杀好入场;
    ③ 面板 watchlist 同源; trade_store.stats_by_date 对 NULL 直接 sum 会 TypeError,
       且旧行为把未平仓行按 realized_pnl<=0 算作亏损.

修复: 写入侧未了结行 realized_pnl 写 NULL (判据="有任何出场痕迹", 0 价坏行情仍保留真盈亏);
查重判据收紧为 exit_price>0 (抽 EXTERNAL_CLOSE_DUP_SQL 常量以便钉不变量);
_query_history / watchlist 补全仓统一的 exit_price>0 口径; stats_by_date 只统计已了结行.
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.trade_store import TradeStore          # noqa: E402
from app.core.position_sync import EXTERNAL_CLOSE_DUP_SQL  # noqa: E402
from app.utils.timezone import now_et                   # noqa: E402

_RECENT = now_et().isoformat()
_ENTRY_DAY = now_et().strftime("%Y-%m-%d")
_ENTRY_RECENT = _RECENT


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, symbol, direction, realized_pnl, exit_price, exit_time, trade_date "
            "FROM trade_records ORDER BY id").fetchall()]
    finally:
        conn.close()


# ─────────────────────────── ① 写入侧: 未了结不再落 0.0 ───────────────────────────

def _open_record(**over):
    rec = {
        "trade_date": _ENTRY_DAY, "symbol": "US.TEST", "direction": "LONG",
        "entry_time": _ENTRY_RECENT, "exit_time": "", "entry_price": 100.0,
        "exit_price": 0, "quantity": 10, "realized_pnl": 0, "pnl_pct": 0,
        "exit_reason": "", "holding_hours": 0, "signal_strength": 50,
        "signal_id": "sig-open-1", "source_type": "PLAYBOOK_SIM",
    }
    rec.update(over)
    return rec


def test_open_row_realized_pnl_is_null(tmp_path):
    """开仓行 realized_pnl 必须 NULL — 0.0 占位是本次全部下游缺陷的源头."""
    db = str(tmp_path / "t.db")
    TradeStore(db).save_trade(_open_record())
    r = _rows(db)[0]
    assert r["realized_pnl"] is None
    assert r["exit_price"] is None  # 既有归一, 未平仓行本就 NULL


def test_closed_row_keeps_realized_pnl(tmp_path):
    """平仓行 (exit_price>0) 的真实盈亏不得被 NULL 化."""
    db = str(tmp_path / "t.db")
    TradeStore(db).save_trade(_open_record(
        exit_time=_RECENT, exit_price=110.0, realized_pnl=95.5, pnl_pct=0.0955,
        exit_reason="TP1", signal_id="sig-close-1"))
    assert _rows(db)[0]["realized_pnl"] == 95.5


def test_closed_row_with_bad_quote_keeps_pnl(tmp_path):
    """0 价坏行情平仓 (exit_price 缺失但 exit_time 有值) 仍须保留真实盈亏 — 判据不能只看价格."""
    db = str(tmp_path / "t.db")
    TradeStore(db).save_trade(_open_record(
        exit_time=_RECENT, exit_price=0, realized_pnl=-42.0,
        exit_reason="OPEN", signal_id="sig-badpx-1"))
    assert _rows(db)[0]["realized_pnl"] == -42.0


def test_closed_row_without_pnl_is_null_not_zero(tmp_path):
    """已平仓但调用方未给盈亏 → NULL (未知), 而不是 0.0 (伪装保本)."""
    db = str(tmp_path / "t.db")
    rec = _open_record(exit_time=_RECENT, exit_price=110.0, exit_reason="SL_FINAL",
                       signal_id="sig-nopnl-1")
    rec.pop("realized_pnl")
    TradeStore(db).save_trade(rec)
    assert _rows(db)[0]["realized_pnl"] is None


# ─────────────── ② external_close 查重判据: 未平仓行不算"已记账平仓" ───────────────

@pytest.fixture
def dup_db(tmp_path):
    """构造生产实态: 同一 US.BULL SHORT 仓位的开仓行(旧数据带 0.0 占位) + 已了结行."""
    db = tmp_path / "dup.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, direction TEXT,
        entry_time TEXT, exit_time TEXT, exit_price REAL, realized_pnl REAL)""")
    conn.execute("INSERT INTO trade_records (symbol,direction,entry_time,exit_price,realized_pnl) "
                 "VALUES ('US.BULL','SHORT','2026-08-20T09:38:09.478896-04:00',NULL,0.0)")
    # id=1320 实态: entry_time 以 +08:00 (CST 挂钟) 写法落库, 平仓已记账
    conn.execute("INSERT INTO trade_records (symbol,direction,entry_time,exit_time,exit_price,realized_pnl) "
                 "VALUES ('US.BULL','SHORT','2026-08-20T21:47:17.057797+08:00',"
                 "'2026-08-20T10:14:36.475643-04:00',9.41,193.92)")
    # 另一标的的未平仓行 — 不得进入候选
    conn.execute("INSERT INTO trade_records (symbol,direction,entry_time,exit_price,realized_pnl) "
                 "VALUES ('US.MU','LONG','2026-08-27T09:38:22.092208-04:00',NULL,0.0)")
    conn.commit()
    conn.close()
    return str(db)


def test_dup_guard_candidates_are_booked_closes_only(dup_db):
    """未平仓行 (exit_price NULL, realized_pnl=0.0 占位) 不得被当作重复记账依据."""
    conn = sqlite3.connect(dup_db)
    try:
        hits = conn.execute(EXTERNAL_CLOSE_DUP_SQL, ("US.BULL", "SHORT")).fetchall()
    finally:
        conn.close()
    assert [h[0] for h in hits] == [2], "只应命中已了结行 id=2"


def test_dup_guard_still_blocks_bull_1327_double_booking(dup_db):
    """BULL 1320/1327 双重记账场景仍须拦住: 时区写法不同的同一入场瞬间必须出现在候选集里."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    def epoch(s):
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=ET)
        return int(d.timestamp())

    conn = sqlite3.connect(dup_db)
    try:
        candidates = conn.execute(EXTERNAL_CLOSE_DUP_SQL, ("US.BULL", "SHORT")).fetchall()
    finally:
        conn.close()
    # 1327 写入时 existing_pos.entry_time 的 ET 瞬时
    this = epoch("2026-08-20T09:47:17.054237-04:00")
    matched = [cid for cid, es in candidates if epoch(es) == this]
    assert matched == [2]


def test_dup_guard_ignores_other_symbol_direction(dup_db):
    conn = sqlite3.connect(dup_db)
    try:
        assert conn.execute(EXTERNAL_CLOSE_DUP_SQL, ("US.MU", "LONG")).fetchall() == []
        assert conn.execute(EXTERNAL_CLOSE_DUP_SQL, ("US.BULL", "LONG")).fetchall() == []
    finally:
        conn.close()


# ─────────────── ③ pre_entry_gate 历史统计: 在途仓不得掺入胜率 ───────────────

@pytest.fixture
def gate_db(tmp_path, monkeypatch):
    db = tmp_path / "gate.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, direction TEXT, trade_date TEXT,
        entry_time TEXT, exit_time TEXT, exit_price REAL, realized_pnl REAL,
        exit_reason TEXT, holding_hours REAL)""")
    day = now_et().strftime("%Y-%m-%dT%H:%M:%S")
    # US.AVGO SHORT: 5 笔已了结全胜 + 1 笔在途 (旧实现会算成"1 笔 0 盈亏")
    for i in range(5):
        conn.execute("INSERT INTO trade_records (symbol,direction,trade_date,entry_time,"
                     "exit_time,exit_price,realized_pnl,exit_reason,holding_hours) "
                     "VALUES ('US.AVGO','SHORT',?,?,?,'290.0',100.0,'TP1',1.0)",
                     (_ENTRY_DAY, day, day))
    conn.execute("INSERT INTO trade_records (symbol,direction,trade_date,entry_time,"
                 "exit_time,exit_price,realized_pnl,exit_reason,holding_hours) "
                 "VALUES ('US.AVGO','SHORT',?,?,NULL,NULL,0.0,'',0.0)", (_ENTRY_DAY, day))
    # US.PDD LONG: 只有在途一行 → 真值是"无历史"
    conn.execute("INSERT INTO trade_records (symbol,direction,trade_date,entry_time,"
                 "exit_time,exit_price,realized_pnl,exit_reason,holding_hours) "
                 "VALUES ('US.PDD','LONG',?,?,NULL,NULL,0.0,'',0.0)", (_ENTRY_DAY, day))
    conn.commit()
    conn.close()
    import app.core.paths as paths
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: str(db))
    return str(db)


def test_gate_history_excludes_open_position(gate_db):
    from app.llm.pre_entry_gate import _query_history
    st = _query_history("US.AVGO", "SHORT", days=7)
    assert st["trades"] == 5, "在途行不得进分母"
    assert st["win_rate"] == 1.0, "旧实现报 83.3% (5胜/6)"
    assert st["avg_pnl"] == pytest.approx(100.0)


def test_gate_history_open_only_means_no_history(gate_db):
    from app.llm.pre_entry_gate import _query_history
    st = _query_history("US.PDD", "LONG", days=7)
    assert st["trades"] == 0 and st["win_rate"] == 0.0
    # 旧实现在此返回 trades=1 / 0% 胜率 → LLM 被告知"该标的上次亏了 0 元"


# ─────────────── ④ stats_by_date: NULL 不得炸, 在途仓不得算亏损 ───────────────

def test_stats_by_date_null_safe_and_excludes_open(tmp_path):
    db = str(tmp_path / "t.db")
    ts = TradeStore(db)
    ts.save_trade(_open_record(signal_id="sig-a"))
    ts.save_trade(_open_record(exit_time=_RECENT, exit_price=110.0, realized_pnl=50.0,
                               exit_reason="TP1", signal_id="sig-b"))
    ts.save_trade(_open_record(direction="SHORT", exit_time=_RECENT, exit_price=90.0,
                               realized_pnl=-20.0, exit_reason="SL_FINAL",
                               signal_id="sig-c", entry_price=100.0))
    st = ts.stats_by_date(_ENTRY_DAY)
    assert st["total_trades"] == 2, "在途行不计入"
    assert st["wins"] == 1 and st["losses"] == 1
    assert st["total_pnl"] == 30.0
    assert st["max_pnl"] == 50.0 and st["min_pnl"] == -20.0


def test_stats_by_date_only_open_positions(tmp_path):
    """全是在途仓 → 视为当日无了结, 不得 TypeError 也不得报 1 笔亏损."""
    db = str(tmp_path / "t.db")
    ts = TradeStore(db)
    ts.save_trade(_open_record(signal_id="sig-only-open"))
    st = ts.stats_by_date(_ENTRY_DAY)
    assert st == {"total_trades": 0, "total_pnl": 0.0}


# ─────────────── ⑤ 面板标的统计: 数据源跟随模式, 在途仓不计 ───────────────

@pytest.fixture
def panel_db(tmp_path, monkeypatch):
    """面板 _get_trade_stats 的数据源注入 (绝不触碰生产库)."""
    from app.web_dashboard.routes import watchlist as wl
    db = tmp_path / "panel.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, trade_date TEXT,
        entry_time TEXT, exit_time TEXT, exit_price REAL, realized_pnl REAL)""")
    for pnl, xp in ((100.0, "12.0"), (-50.0, "12.0"), (0.0, None)):  # 第 3 行=在途占位
        conn.execute("INSERT INTO trade_records (symbol,trade_date,entry_time,exit_time,"
                     "exit_price,realized_pnl) VALUES ('US.PANEL','2026-08-28',?,?,?,?)",
                     (_ENTRY_RECENT, _RECENT, xp, pnl))
    conn.commit()
    conn.close()
    monkeypatch.setattr(wl, "_get_db", lambda: str(db))
    return str(db)


def test_panel_stats_reads_mode_aware_main_db(panel_db):
    from app.web_dashboard.routes.watchlist import _get_trade_stats
    st = _get_trade_stats(["US.PANEL"])["US.PANEL"]
    assert st["count"] == 2, "在途行不得计入 (旧: realized_pnl=0.0 占位会算成第 3 笔)"
    assert st["win_rate"] == 50.0
    assert st["total_pnl"] == 50.0


def test_panel_stats_no_longer_hardcodes_live_file(tmp_path, monkeypatch):
    """回归钉: 不得再无条件读 ftseek_us_live.db —— sim 模式下它是零写入的死库."""
    import inspect

    from app.web_dashboard.routes import watchlist as wl
    code = "\n".join(l for l in inspect.getsource(wl._get_trade_stats).splitlines()
                     if not l.lstrip().startswith("#"))
    assert "ftseek_us_live" not in code
