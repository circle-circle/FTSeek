"""指令追踪列白名单契约: 传进来 ≠ 落库 (静默丢弃是本仓反复出现过的事故型).

背景 (2026-11-05 复查):
  auto_execute 的 re-price 把重算后的 effective `risk_reward` 放进 `_reprice_params`
  并传给 `update_instruction_tracking`, 但该函数只写 `_TRACKING_FIELDS` 白名单内的列,
  而 `risk_reward` 不在其中 ⇒ 列值永远停在 LLM 计划口径 (实测 COIN 列 4.44 / 几何 1.50),
  所有直接读列的分析都偏乐观。同型前科: reprice_from/reprice_to/reprice_original_sl
  (db.py 注释记载: 构造了、注释写\"可追溯\"、但白名单不含 ⇒ 从未落库)。

本文件钉住两条不变量:
  ① risk_reward 必须在白名单且真的写进去 (列 = effective);
  ② 白名单是**有意**的边界: 非列字段 (如 reprice_from) 仍被静默忽略, 不因本次放宽而泄漏。
"""
import sqlite3

from app.playbook.db import _TRACKING_FIELDS, PlaybookDB

_COLUMNS = (
    "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, "
    "entry_price REAL, stop_loss REAL, take_profit REAL, risk_reward REAL, "
    "risk_amount REAL, position_size REAL, executed INTEGER, updated_at TEXT"
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE playbook_instructions ({_COLUMNS})")
    conn.execute(
        "INSERT INTO playbook_instructions (id, date, symbol, direction, entry_price, "
        "stop_loss, take_profit, risk_reward, executed) "
        "VALUES (1, '2026-11-04', 'US.COIN', 'SELL_SHORT', 149.0, 157.19, 136.71, 4.44, 0)"
    )
    conn.commit()
    return conn


def test_risk_reward_is_whitelisted():
    assert "risk_reward" in _TRACKING_FIELDS, (
        "risk_reward 不在 _TRACKING_FIELDS ⇒ re-price 算出的 effective RR 会被静默丢弃"
    )


def test_risk_reward_write_reaches_db():
    conn = _conn()
    PlaybookDB(conn=conn).update_instruction_tracking(1, {"risk_reward": 1.5})
    value = conn.execute(
        "SELECT risk_reward FROM playbook_instructions WHERE id = 1").fetchone()[0]
    assert value == 1.5


def test_none_risk_reward_does_not_blank_column():
    """re-price 算不出 RR 时传 None —— 不得把既有计划值写空 (修复前该键被静默丢弃)。"""
    conn = _conn()
    PlaybookDB(conn=conn).update_instruction_tracking(1, {"risk_reward": None})
    value = conn.execute(
        "SELECT risk_reward FROM playbook_instructions WHERE id = 1").fetchone()[0]
    assert value == 4.44


def test_non_column_fields_still_ignored():
    """reprice_from 等审计键不是 DB 列 —— 白名单必须继续挡住它们 (不炸也不写)。"""
    conn = _conn()
    PlaybookDB(conn=conn).update_instruction_tracking(
        1, {"reprice_from": 145.0, "reprice_to": 149.0})
    cols = {r[1] for r in conn.execute("PRAGMA table_info(playbook_instructions)")}
    assert "reprice_from" not in cols          # 不该被动态建列
    assert "risk_reward" in cols