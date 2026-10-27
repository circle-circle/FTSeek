"""[DEB-20261027 A] open_positions 的 SL/TP/entry_time 必须与出场链同源。

病灶 (2026-10-27 实测):
  ``app/core/position_sync.py`` 的 ``_is_pb_sim`` 分支写 open_positions 时硬写
  ``initial_sl=0, current_sl=0, initial_tp=0`` 且 ``entry_time=datetime.now().isoformat()``
  (宿主 CST、naive)。⇒
    ① 同一笔仓在 open_positions 里"没有止损", 而出场链实际用的
       ``trade_records.metadata_json.stop_loss`` 有值 ⇒ 两表两个止损;
       实测 US.DYN: open_positions.initial_sl=0 vs trade_records stop_loss=15.17。
    ② entry_time 口径与 R-12 (DEB-20260823) 已裁的 ET 口径不一致 (差 12h);
       R-12 只覆盖 ``trade_store.upsert_position_simple``, 本分支当年被漏掉。

本测试锁死:
  1) 助手 ``_playbook_position_fields`` 的取值与合并语义 (只补空, 不覆盖已有权威值);
  2) 量纲: initial_sl / initial_tp 为绝对价格 (USD), 与既有列一致;
  3) 回归: 生产库中每个未平 PLAYBOOK 仓的 open_positions SL 必须与其
     trade_records.metadata_json.stop_loss 一致 (或两边都空)。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from app.core.position_sync import _playbook_position_fields

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE open_positions (
        symbol TEXT PRIMARY KEY, direction TEXT, size REAL, avg_entry_price REAL,
        initial_sl REAL, current_sl REAL, initial_tp REAL, entry_time TEXT,
        updated_at TEXT, strategy_id TEXT, trader_type TEXT, owner_strategy TEXT,
        owner TEXT, sync_status TEXT)""")
    c.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_time TEXT,
        metadata_json TEXT, source_type TEXT, exit_time TEXT)""")
    c.commit()
    return c


def _op(conn, symbol, sl=0.0, tp=0.0, entry=""):
    conn.execute(
        "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, "
        "initial_sl, current_sl, initial_tp, entry_time) VALUES (?,?,?,?,?,?,?,?)",
        (symbol, "LONG", 10, 100.0, sl, sl, tp, entry))


def _tr(conn, symbol, sl=None, tp=None, entry="2026-10-27T09:47:21-04:00",
        src="PLAYBOOK_SIM", exit_time=None, meta_override=None):
    md = {"stop_loss": sl, "take_profit": tp} if meta_override is None else meta_override
    conn.execute(
        "INSERT INTO trade_records (symbol, entry_time, metadata_json, source_type, exit_time) "
        "VALUES (?,?,?,?,?)",
        (symbol, entry, json.dumps(md), src, exit_time))


# ── 助手语义 ────────────────────────────────────────────────────────────────

def test_no_row_no_trade_record_falls_back_to_et_now(conn):
    sl, tp, entry = _playbook_position_fields(conn, "US.NONE")
    assert sl == 0.0 and tp == 0.0
    # entry_time 必须带时区 (R-12 ET 口径) — naive 串会让按 ET 判断的函数错 12h
    from datetime import datetime
    assert datetime.fromisoformat(entry).tzinfo is not None, entry


def test_sl_tp_come_from_trade_record_metadata(conn):
    _tr(conn, "US.DYN", sl=15.17, tp=17.17)
    sl, tp, entry = _playbook_position_fields(conn, "US.DYN")
    assert sl == pytest.approx(15.17)
    assert tp == pytest.approx(17.17)
    # entry_time 取 trade_record 的 ET-aware 值, 不用写入时刻
    assert entry == "2026-10-27T09:47:21-04:00"


def test_existing_authoritative_sl_is_not_overwritten(conn):
    _op(conn, "US.KEEP", sl=134.51, tp=152.26, entry="2026-10-27T09:44:03.746075-04:00")
    _tr(conn, "US.KEEP", sl=99.99, tp=199.99)
    sl, tp, entry = _playbook_position_fields(conn, "US.KEEP")
    assert sl == pytest.approx(134.51)      # 保留更早的权威写入
    assert tp == pytest.approx(152.26)
    assert entry == "2026-10-27T09:44:03.746075-04:00"   # 已带时区 ⇒ 保留


def test_naive_existing_entry_time_is_replaced_by_trade_record(conn):
    _op(conn, "US.NAIVE", sl=0.0, tp=0.0, entry="2026-10-27T21:47:45.691846")
    _tr(conn, "US.NAIVE", sl=15.17, tp=17.17)
    sl, tp, entry = _playbook_position_fields(conn, "US.NAIVE")
    assert entry == "2026-10-27T09:47:21-04:00"
    assert sl == pytest.approx(15.17)


def test_closed_trade_record_is_not_used(conn):
    _tr(conn, "US.CLOSED", sl=50.0, tp=60.0, exit_time="2026-10-27T10:00:00-04:00")
    sl, tp, _entry = _playbook_position_fields(conn, "US.CLOSED")
    assert sl == 0.0 and tp == 0.0


def test_manual_source_trade_record_is_not_used(conn):
    _tr(conn, "US.MAN", sl=50.0, tp=60.0, src="MANUAL")
    sl, tp, _entry = _playbook_position_fields(conn, "US.MAN")
    assert sl == 0.0 and tp == 0.0


def test_metadata_without_stop_loss_yields_zero(conn):
    _tr(conn, "US.NOMETA", meta_override={"adopted": True})
    sl, tp, _entry = _playbook_position_fields(conn, "US.NOMETA")
    assert sl == 0.0 and tp == 0.0


# ── 源级锚点: 分支不得再硬写 0/0/0 + datetime.now() ───────────────────────

def test_source_no_longer_hardcodes_zero_sl_in_pb_sim_branch():
    src = (REPO / "app" / "core" / "position_sync.py").read_text(encoding="utf-8")
    i = src.find("if _is_pb_sim:")
    assert i > 0, "未找到 _is_pb_sim 分支"
    seg = src[i:i + 3000]
    assert "_playbook_position_fields(cursor, symbol)" in seg
    # 旧的硬写形态不得复现
    assert "0, 0, 0, datetime.now().isoformat(), datetime.now().isoformat()," not in seg


# ── 生产回归 (只读): open_positions SL 与出场链真源一致 ─────────────────────

@pytest.mark.skipif(not (REPO / "data" / "ftseek_us.db").exists(), reason="无生产库")
def test_production_open_position_never_missing_sl_when_trade_record_has_one():
    """未平 PLAYBOOK 仓: open_positions **不得丢失** trade_records 里已有的止损。

    注意: 两表的 SL **不要求相等** ——
      open_positions.initial_sl 是执行时 re-price 后的止损 (playbook_service 写入),
      trade_records.metadata_json.stop_loss 是入场时快照;
      出场链的早期紧SL/保本/锁利还会继续移动**生效线**, 两者本就会分叉
      (实测 US.TSM: open_positions 422.40 vs trade_records 411.60 —— 后者是入场快照值)。
    本测试只锁"**不该缺失**"这条: 有止损却记成 0 = 本批修的病灶。
    """
    uri = f"file:{(REPO / 'data' / 'ftseek_us.db').resolve()}?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT o.symbol, o.initial_sl, o.entry_time,
               t.metadata_json, t.entry_time AS tr_entry
        FROM open_positions o
        LEFT JOIN trade_records t
          ON t.id = (SELECT id FROM trade_records t2
                     WHERE t2.symbol = o.symbol
                       AND t2.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
                       AND (t2.exit_time IS NULL OR t2.exit_time='')
                     ORDER BY t2.id DESC LIMIT 1)
        WHERE o.owner_strategy IN ('playbook_sim','playbook_live')
    """).fetchall()
    missing = []
    naive_entry = []
    for r in rows:
        try:
            md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except Exception:
            md = {}
        tr_sl = float((md or {}).get("stop_loss") or 0)
        op_sl = float(r["initial_sl"] or 0)
        if tr_sl > 0 and op_sl <= 0:
            missing.append((r["symbol"], op_sl, tr_sl))
        # entry_time 必须带时区 (R-12 ET 口径)
        try:
            from datetime import datetime
            if r["entry_time"] and datetime.fromisoformat(r["entry_time"]).tzinfo is None:
                naive_entry.append(r["symbol"])
        except Exception:
            naive_entry.append(r["symbol"])
    c.close()
    assert not missing, f"open_positions 丢了止损 (trade_records 有值): {missing}"
    assert not naive_entry, f"open_positions.entry_time 仍是 naive (应为 ET-aware): {naive_entry}"
