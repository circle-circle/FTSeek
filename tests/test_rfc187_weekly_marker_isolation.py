"""[RFC-187] 周报幂等标记与环境行隔离。

守的行为:
  ① 写周报标记**不得**在 playbook_regime_log 留下 date=week_end 的占位行
     (旧写法撞 UNIQUE(date), 实测吃掉 16/18 个周五的环境行)
  ② 标记与环境行可同日期共存, 顺序无关
  ③ has_weekly_report_marker 幂等语义不变
"""
from __future__ import annotations

import sqlite3

from app.playbook.db import PlaybookDB

WEEK_END = "2026-11-06"


def _db() -> PlaybookDB:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row      # get_regime_log 依赖 _row_to_dict(dict(row))
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    return db


def _regime_rows(db: PlaybookDB, date: str):
    return db._conn().execute(
        "SELECT date, regime, vix FROM playbook_regime_log WHERE date = ?", (date,)
    ).fetchall()


def _save_env(db: PlaybookDB, date: str, regime: str = "cautious", vix: float = 16.5):
    db.save_regime_log({"date": date, "regime": regime, "vix": vix,
                        "spy_price": 660.0, "spy_ma20": 655.0, "spy_ma50": 640.0,
                        "score": 55, "notes": "env"})


def test_marker_does_not_occupy_regime_date():
    db = _db()
    db.mark_weekly_report(WEEK_END)
    assert db.has_weekly_report_marker(WEEK_END) is True
    assert _regime_rows(db, WEEK_END) == [], "标记不得在环境表留行"


def test_env_row_survives_marker_written_after():
    """先写环境行, 后写标记 ⇒ 环境行必须还在 (旧写法会被 UPSERT 覆盖)。"""
    db = _db()
    _save_env(db, WEEK_END)
    db.mark_weekly_report(WEEK_END)
    rows = _regime_rows(db, WEEK_END)
    assert len(rows) == 1 and rows[0][1] == "cautious", rows
    assert db.has_weekly_report_marker(WEEK_END) is True


def test_env_row_survives_marker_written_before():
    """先写标记, 后写环境行 ⇒ 两者并存。"""
    db = _db()
    db.mark_weekly_report(WEEK_END)
    _save_env(db, WEEK_END)
    rows = _regime_rows(db, WEEK_END)
    assert len(rows) == 1 and rows[0][1] == "cautious", rows
    assert db.has_weekly_report_marker(WEEK_END) is True


def test_marker_is_idempotent():
    db = _db()
    db.mark_weekly_report(WEEK_END)
    db.mark_weekly_report(WEEK_END)
    n = db._conn().execute(
        "SELECT COUNT(*) FROM playbook_weekly_markers WHERE week_end = ?", (WEEK_END,)
    ).fetchone()[0]
    assert n == 1


def test_get_regime_log_never_returns_markers():
    db = _db()
    _save_env(db, "2026-11-05", "risk_on", 15.4)
    db.mark_weekly_report("2026-11-06")
    rows = db.get_regime_log(days=10)
    assert all("weekly" not in str(r.get("regime", "")) for r in rows), rows
    assert [r["date"] for r in rows] == ["2026-11-05"]