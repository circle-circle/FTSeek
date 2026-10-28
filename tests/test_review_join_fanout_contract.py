"""复盘日数据采集的 JOIN 扇出回归 (DEB-20261028).

`playbook_instructions` 会写入大量 ``decision_id=''`` 的供给占位行。复盘原按
``tr.decision_id = pi.decision_id`` LEFT JOIN，导致无 decision_id 的成交与全部
占位行做笛卡尔匹配；实测 2026-09-16 单日 18 笔成交膨胀成 5,962 行。

本测试锁三件事：
  1) metadata_json.playbook_instruction_id 是权威锚，优先于 decision_id 回退；
  2) decision_id 只在全表唯一且非空时才允许回退；
  3) 每笔 trade_record 最多产出一行，空占位行绝不会再次扇出。
"""
from pathlib import Path
import sqlite3

import pytest

from app.review.review_orchestrator import ReviewDataCollector


REPO = Path(__file__).resolve().parents[1]


def _make_db(tmp_path: Path) -> Path:
    path = tmp_path / "review-join.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            trade_date TEXT,
            symbol TEXT,
            direction TEXT,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            source_type TEXT,
            decision_id TEXT,
            metadata_json TEXT,
            created_at TEXT,
            realized_pnl REAL,
            pnl_net REAL
        );
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY,
            date TEXT,
            decision_id TEXT,
            max_favorable_excursion REAL,
            max_adverse_excursion REAL,
            mfe_analysis TEXT,
            conditions_json TEXT,
            thesis TEXT,
            matched_patterns_json TEXT,
            pattern_verdict TEXT,
            fwd_return_t1 REAL,
            fwd_return_t3 REAL,
            fwd_return_t5 REAL,
            confidence REAL,
            market_regime TEXT,
            vix_at_generation REAL,
            stop_loss REAL,
            take_profit REAL
        );
        CREATE TABLE capital_state (last_update TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO playbook_instructions (id, date, decision_id, thesis) VALUES (?,?,?,?)",
        [
            (101, "2026-09-16", "", "authoritative anchor"),
            (102, "2026-09-16", "UNIQUE-1", "unique fallback"),
            (103, "2026-09-16", "DUP-1", "ambiguous one"),
            (104, "2026-09-16", "DUP-1", "ambiguous two"),
            (105, "2026-09-16", "", "another placeholder"),
            (106, "2026-09-16", "", "third placeholder"),
        ],
    )
    conn.executemany(
        "INSERT INTO trade_records "
        "(id, trade_date, symbol, direction, exit_time, exit_price, exit_reason, "
        " source_type, decision_id, metadata_json, created_at, realized_pnl, pnl_net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "2026-09-16", "US.ANCHOR", "LONG", None, None, None, "PLAYBOOK_SIM",
             "not-the-anchor", '{"playbook_instruction_id": 101}', "2026-09-16T10:00:00-04:00", None, None),
            (2, "2026-09-16", "US.UNIQUE", "LONG", None, None, None, "PLAYBOOK_SIM",
             "UNIQUE-1", None, "2026-09-16T10:01:00-04:00", None, None),
            (3, "2026-09-16", "US.DUP", "LONG", None, None, None, "PLAYBOOK_SIM",
             "DUP-1", None, "2026-09-16T10:02:00-04:00", None, None),
            (4, "2026-09-16", "US.EMPTY", "LONG", None, None, None, "PLAYBOOK_SIM",
             "", None, "2026-09-16T10:03:00-04:00", None, None),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_collect_daily_never_fans_out_on_placeholder_rows(tmp_path):
    collector = ReviewDataCollector(str(_make_db(tmp_path)))

    rows = collector.collect_daily("2026-09-16")["trades"]

    assert len(rows) == 4
    assert {row["id"] for row in rows} == {1, 2, 3, 4}
    by_id = {row["id"]: row for row in rows}
    assert by_id[1]["pb_thesis"] == "authoritative anchor"
    assert by_id[2]["pb_thesis"] == "unique fallback"
    assert by_id[3]["pb_thesis"] is None  # 重复 decision_id: 不猜
    assert by_id[4]["pb_thesis"] is None  # 空 decision_id: 不得匹配占位行


@pytest.mark.skipif(not (REPO / "data" / "ftseek_us.db").exists(), reason="无生产库")
def test_production_review_rows_do_not_exceed_trade_records():
    """生产只读回归：强化后的 JOIN 不得让任何一笔成交重复出现。"""
    db = REPO / "data" / "ftseek_us.db"
    uri = f"file:{db.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        source_count = conn.execute(
            "SELECT COUNT(*) FROM trade_records "
            "WHERE (trade_date = ? OR substr(exit_time, 1, 10) = ?) "
            "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
            "AND (exit_reason IS NULL OR exit_reason != 'PHANTOM_RECONCILE_VOID')",
            ("2026-09-16", "2026-09-16"),
        ).fetchone()[0]
    finally:
        conn.close()

    rows = ReviewDataCollector(str(db)).collect_daily("2026-09-16")["trades"]
    assert len(rows) <= source_count
    assert len({row["id"] for row in rows}) == len(rows)
