"""intraday_reentry._load_closed_today 出场日记账回归测试 (2026-08-28)。

背景: 盘中再入场裁决的"当日已平仓"集合原按 trade_date (入场日) 归集 —
隔夜仓今日开盘割 (跳空了结) 不在集合里, 该标的整日不进入再入场裁决,
"每标的日 1 次"约束同时失真 (8/27 实证: UBER/HOOD 0↔1, ORCL 1↔2)。
同 AccountCircuitBreaker / _check_symbol_daily_loss 口径: substr(exit_time,1,10)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.playbook.intraday_reentry import _load_closed_today, _today_et_str


@pytest.fixture
def re_env(tmp_path, monkeypatch):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, trade_date TEXT, "
        "exit_time TEXT, exit_price REAL, entry_price REAL, pnl_pct REAL, "
        "exit_reason TEXT, quantity REAL, source_type TEXT)"
    )
    conn.commit()
    conn.close()
    import app.core.paths as paths
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: db)
    return db


def _insert(db, symbol, *, trade_date, exit_date, source_type="PLAYBOOK_SIM",
            exit_price=100.0, direction="LONG", pnl_pct=-0.03):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, trade_date, exit_time, "
        "exit_price, entry_price, pnl_pct, exit_reason, quantity, source_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, direction, trade_date,
         f"{exit_date}T09:30:05-04:00" if exit_date else None,
         exit_price, 105.0, pnl_pct, "DAYTRADE_STOP", 10.0, source_type),
    )
    conn.commit()
    conn.close()


def test_overnight_close_today_enters_reentry_pool(re_env):
    """核心回归: 昨日入场/今晨出场的隔夜仓必须出现在"当日已平仓"集合。"""
    today = _today_et_str()
    _insert(re_env, "US.UBER", trade_date="2026-08-26", exit_date=today)
    agg = _load_closed_today()
    assert agg["US.UBER"]["closed_count"] == 1
    assert agg["US.UBER"]["trades"][0]["pnl_pct"] == pytest.approx(-0.03)


def test_same_day_round_trip_still_counted(re_env):
    today = _today_et_str()
    _insert(re_env, "US.HOOD", trade_date=today, exit_date=today)
    assert _load_closed_today()["US.HOOD"]["closed_count"] == 1


def test_two_closes_today_visible_to_once_per_day_cap(re_env):
    """ORCL 8/27 案例: 昨日隔夜割 + 今日盘中割 → closed_count=2,
    消费方 (:783 != 1 → continue) 据此拒绝再入场。旧口径只见 1 笔 → 误放行。"""
    today = _today_et_str()
    _insert(re_env, "US.ORCL", trade_date="2026-08-26", exit_date=today)
    _insert(re_env, "US.ORCL", trade_date=today, exit_date=today)
    assert _load_closed_today()["US.ORCL"]["closed_count"] == 2


def test_yesterday_close_not_counted_today(re_env):
    _insert(re_env, "US.AMD", trade_date="2026-08-25", exit_date="2026-08-26")
    assert "US.AMD" not in _load_closed_today()


def test_open_position_excluded(re_env):
    _insert(re_env, "US.MU", trade_date=_today_et_str(), exit_date=None,
            exit_price=None)
    assert "US.MU" not in _load_closed_today()


def test_non_playbook_source_excluded(re_env):
    """MANUAL 等外来行不进再入场池 (source_type 过滤仍生效)。"""
    _insert(re_env, "US.MRK", trade_date="2026-08-26", exit_date=_today_et_str(),
            source_type="MANUAL")
    assert "US.MRK" not in _load_closed_today()


def test_live_source_included(re_env):
    today = _today_et_str()
    _insert(re_env, "US.GRBK", trade_date="2026-08-26", exit_date=today,
            source_type="PLAYBOOK_LIVE")
    assert _load_closed_today()["US.GRBK"]["closed_count"] == 1
