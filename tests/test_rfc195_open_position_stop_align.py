"""[RFC-195] open_positions 止损对齐指令真值 (恢复路径合成 SL 的收口)。

病灶 (2026-11-09 实测 US.LLY): position_sync 在 playbook 落库前抢先建仓, 写入 ATR×mult
被 2% 硬底截断的合成 SL (1351.79 = −2%); 31s 后 reconcile 用指令真值 (1311.52 = −5%)
补录 trade_record ⇒ 同一笔仓两表两个止损。出场链只读 trade_records (无交易影响), 但
open_positions 是推送/看板/旧风控告警的取值面。
"""
import sqlite3

import pytest

from app.core.position_sync import _align_open_position_stops


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "pos.db"))
    c.execute(
        "CREATE TABLE open_positions ("
        " symbol TEXT PRIMARY KEY, strategy_id TEXT, initial_sl REAL, current_sl REAL,"
        " initial_tp REAL, updated_at TEXT)"
    )
    yield c
    c.close()


def test_align_overwrites_synthetic_stop(conn):
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.LLY','playbook_sim',1351.79,1351.79,0,'t0')"
    )
    assert _align_open_position_stops(conn, "US.LLY", 1311.52, 1484.09) == 1
    row = conn.execute(
        "SELECT initial_sl, current_sl, initial_tp FROM open_positions WHERE symbol='US.LLY'"
    ).fetchone()
    assert row[0] == pytest.approx(1311.52)
    assert row[1] == pytest.approx(1311.52), "未收紧过的 current_sl 应跟着对齐"
    assert row[2] == pytest.approx(1484.09)


def test_align_keeps_tightened_current_sl(conn):
    """current_sl 已被 trailing/规则0.5 抬高 ⇒ 只改 initial_sl, 不得松掉活止损。"""
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.LLY','playbook_sim',1351.79,1362.42,0,'t0')"
    )
    assert _align_open_position_stops(conn, "US.LLY", 1311.52, 1484.09) == 1
    row = conn.execute(
        "SELECT initial_sl, current_sl FROM open_positions WHERE symbol='US.LLY'"
    ).fetchone()
    assert row[0] == pytest.approx(1311.52)
    assert row[1] == pytest.approx(1362.42)


def test_align_skips_manual_and_zero_stop(conn):
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.MCD','extended_hours_entry',267.61,267.61,0,'t0')"
    )
    assert _align_open_position_stops(conn, "US.MCD", 250.0, 300.0) == 0
    assert _align_open_position_stops(conn, "US.MCD", 0, 0) == 0
    row = conn.execute(
        "SELECT initial_sl FROM open_positions WHERE symbol='US.MCD'"
    ).fetchone()
    assert row[0] == pytest.approx(267.61), "非 playbook 仓位不得被改写"


def test_align_noop_when_already_equal(conn):
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.XOM','playbook_sim',156.94,156.94,177.65,'t0')"
    )
    assert _align_open_position_stops(conn, "US.XOM", 156.94, 177.65) == 0