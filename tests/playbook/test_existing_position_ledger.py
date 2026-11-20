"""台账口径: broker 已有持仓 ⇒ 本系统未下单 (幻影行) 的写入/回填/读数.

实证背景 (2026-11-03 LMT/MRK):
  auto_execute 的 already_held 分支为防重试写 executed=1, 但 execution_state 不在
  _TRACKING_FIELDS 白名单里 ⇒ 行停在 PENDING ⇒ 下次启动回填按 executed=1 映射成
  'BOOKED'。BOOKED 是 broker 镜像状态机的 "已成交且已入账", 可这行没有 broker_order_id、
  execution_price=0、trade_records 无对应成交 ⇒ Push/日报/看板把 "按防重复规则跳过的指令"
  读成 "已成交" (当日报 8/16, 实际 6 笔)。

本文件守三条不变量:
  ① 回填不得把幻影行标成 BOOKED; 真有单号/有价的行逐字不动;
  ② 存量已误标 BOOKED 的幻影行被幂等纠正 (只动无单号+无价+already_held 的行);
  ③ 读数判据 is_phantom_execution() 对 dict / sqlite3.Row 同语义, 且缺列时不改变旧统计。
"""
import sqlite3

import pytest

from app.playbook.db import (
    EXISTING_POSITION_STATE,
    PlaybookDB,
    is_phantom_execution,
)

_COLUMNS = (
    "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, "
    "executed INTEGER, execution_price REAL, outcome TEXT, broker_order_id TEXT, "
    "execution_state TEXT, execution_attempt INTEGER, skip_reason TEXT, updated_at TEXT, "
    "execution_time TEXT"
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE playbook_instructions ({_COLUMNS})")
    return conn


def _insert(conn, **kw):
    cols = ", ".join(kw)
    marks = ", ".join("?" * len(kw))
    conn.execute(f"INSERT INTO playbook_instructions ({cols}) VALUES ({marks})",
                 tuple(kw.values()))
    conn.commit()


def _state(conn, row_id):
    return conn.execute(
        "SELECT execution_state FROM playbook_instructions WHERE id = ?", (row_id,)
    ).fetchone()[0]


def test_backfill_keeps_phantom_rows_out_of_booked():
    conn = _conn()
    # 幻影行: executed=1 但无单号/无价 (旧 CASE 会判成 BOOKED)
    _insert(conn, id=1, executed=1, execution_price=0, outcome="already_held",
            broker_order_id=None, execution_state="PENDING", execution_attempt=0)
    # 真成交: 有价有单号 → 必须保持 BOOKED (旧库加列时的形态: PENDING + attempt 0)
    _insert(conn, id=2, executed=1, execution_price=407.57, outcome="win",
            broker_order_id="8324353", execution_state="PENDING", execution_attempt=0)
    # 未执行 → PENDING
    _insert(conn, id=3, executed=0, execution_price=0, outcome=None,
            broker_order_id=None, execution_state=None, execution_attempt=None)

    PlaybookDB(conn=conn)._backfill_execution_state(conn)

    assert _state(conn, 1) == EXISTING_POSITION_STATE
    assert _state(conn, 2) == "BOOKED"
    assert _state(conn, 3) == "PENDING"
    # executed 保持 1: 它是 save_instruction 覆盖仲裁的 "已认领/不得重写" 闩
    assert conn.execute(
        "SELECT executed FROM playbook_instructions WHERE id = 1").fetchone()[0] == 1


def test_backfill_corrects_rows_already_mislabelled_booked():
    """存量纠正: 旧 CASE 已经把幻影行写进 BOOKED, 上面的 WHERE 不会再覆盖它们。"""
    conn = _conn()
    _insert(conn, id=1, executed=1, execution_price=0, outcome="already_held",
            broker_order_id=None, execution_state="BOOKED", execution_attempt=0)
    # 有价但 outcome=already_held (例外形态) → 视为真成交档案, 不动
    _insert(conn, id=2, executed=1, execution_price=141.0, outcome="already_held",
            broker_order_id="8324360", execution_state="BOOKED", execution_attempt=1)

    PlaybookDB(conn=conn)._backfill_execution_state(conn)

    assert _state(conn, 1) == EXISTING_POSITION_STATE
    assert _state(conn, 2) == "BOOKED"


def test_mark_existing_position_writes_terminal_non_booked_state():
    conn = _conn()
    _insert(conn, id=1, executed=0, execution_price=0, outcome=None,
            broker_order_id=None, execution_state="PENDING", execution_attempt=0,
            skip_reason="chase_cap_enforced")

    ok = PlaybookDB(conn=conn).mark_instruction_existing_position(
        1, "2026-11-03T09:37:45-05:00")

    row = conn.execute(
        "SELECT executed, execution_price, outcome, execution_state, skip_reason, "
        "execution_time FROM playbook_instructions WHERE id = 1").fetchone()
    assert ok is True
    assert row[0] == 1                       # 防重试闩
    assert row[1] == 0                       # 没有成交价
    assert row[2] == "already_held"
    assert row[3] == EXISTING_POSITION_STATE  # 不冒充 BOOKED
    assert row[4] == ""                      # executed=1 ⇒ skip_reason 必须空 (REVIEW F-2)
    assert row[5] == "2026-11-03T09:37:45-05:00"


def test_mark_existing_position_never_touches_rows_with_a_broker_order():
    conn = _conn()
    _insert(conn, id=1, executed=1, execution_price=407.57, outcome="win",
            broker_order_id="8324353", execution_state="FILLED_UNBOOKED",
            execution_attempt=1)

    ok = PlaybookDB(conn=conn).mark_instruction_existing_position(
        1, "2026-11-03T09:37:45-05:00")

    row = conn.execute(
        "SELECT execution_price, outcome, execution_state FROM playbook_instructions "
        "WHERE id = 1").fetchone()
    assert ok is False
    assert tuple(row) == (407.57, "win", "FILLED_UNBOOKED")


@pytest.mark.parametrize("state", ["UNKNOWN", "SUBMITTED", "FILLED_UNBOOKED", "BOOKED", "LEGACY_LOCKED"])
def test_existing_position_preserves_execution_claim_without_order_id(state):
    conn = _conn()
    _insert(conn, id=1, executed=1, execution_price=0, outcome=None,
            broker_order_id=None, execution_state=state, execution_attempt=1,
            execution_time="original")
    before = conn.execute("SELECT * FROM playbook_instructions WHERE id=1").fetchone()

    assert not PlaybookDB(conn=conn).mark_instruction_existing_position(1, "later")
    assert conn.execute("SELECT * FROM playbook_instructions WHERE id=1").fetchone() == before


@pytest.mark.parametrize("row,expected", [
    ({"outcome": "already_held", "execution_price": 0}, True),
    ({"outcome": "already_held", "execution_price": None}, True),
    ({"outcome": "already_held", "execution_price": 141.0}, False),  # 例外形态=真成交
    ({"outcome": "win", "execution_price": 0}, False),
    ({}, False),                            # 缺列 ⇒ 不改变旧统计语义
])
def test_is_phantom_execution_python_predicate(row, expected):
    assert is_phantom_execution(row) is expected


def test_is_phantom_execution_accepts_sqlite_row():
    conn = _conn()
    _insert(conn, id=1, executed=1, execution_price=0, outcome="already_held",
            broker_order_id=None, execution_state=EXISTING_POSITION_STATE,
            execution_attempt=0)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM playbook_instructions WHERE id = 1").fetchone()
    # sqlite3.Row 没有 .get() —— 判据必须按列名取
    assert is_phantom_execution(row) is True