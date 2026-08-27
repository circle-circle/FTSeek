"""intraday_heartbeat._query_today_trades 出场日记账 + 净额回归测试 (2026-08-28)。

背景: 盘中心跳钉钉推送的"当日盈亏/笔数/胜率"原 WHERE trade_date=? (入场日) +
SUM(realized_pnl) (毛额) — 隔夜仓今日了结不计入且费用不可见 (8/27 实证:
推送口径 10 笔/+381.15, 出场日净额实为 13 笔/+119.37), 决策者据此读数失真。
修复口径同 AccountCircuitBreaker: substr(exit_time,1,10) + COALESCE(pnl_net, realized_pnl)
+ exit_price>0 (未出场行不入胜率分母)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.observability import intraday_heartbeat as hb
from app.utils.timezone import _et_trade_date, now_et

_TODAY = _et_trade_date(now_et())
_YESTERDAY = (now_et() - timedelta(days=1)).strftime("%Y-%m-%d")


class _StubPool:
    """替身 get_pool — 心跳只用到 .connection("ftseek_us")"""

    def __init__(self, db):
        self._db = str(db)

    def connection(self, name):
        return sqlite3.connect(self._db)


@pytest.fixture
def hb_env(tmp_path, monkeypatch):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL)"
    )
    conn.execute(
        "CREATE TABLE open_positions (id INTEGER PRIMARY KEY, symbol TEXT, "
        "direction TEXT, size REAL, avg_entry_price REAL, current_sl REAL, "
        "owner_strategy TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(hb, "get_pool", lambda: _StubPool(db))
    return db


def _closed(db, symbol, trade_date, exit_date, net, gross=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, trade_date, f"{exit_date}T10:00:00-04:00", 100.0, net,
         gross if gross is not None else net),
    )
    conn.commit()
    conn.close()


def _open_row(db, symbol, trade_date, gross=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl) VALUES (?, ?, NULL, NULL, NULL, ?)",
        (symbol, trade_date, gross),
    )
    conn.commit()
    conn.close()


def _seed_real_827_shape(db):
    """8/27 形态 (数值取自 test_account_circuit_breaker_pnl): 3 隔夜 + 7 当日已了结
    + 1 在仓。出场日净额 = -22.31 / 10 笔 / 8 胜。"""
    _closed(db, "US.UBER", _YESTERDAY, _TODAY, -307.13, -302.60)
    _closed(db, "US.ORCL", _YESTERDAY, _TODAY, 90.73, 95.08)
    _closed(db, "US.HOOD", _YESTERDAY, _TODAY, 2.99, 7.42)
    for i, net in enumerate((-2.31, 101.30, 61.01, 5.13, 9.20, 12.96, 3.81)):
        _closed(db, f"US.D{i}", _TODAY, _TODAY, net)
    _open_row(db, "US.MU", _TODAY, gross=None)


def test_overnight_exits_included_and_net_caliber(hb_env):
    _seed_real_827_shape(hb_env)
    out = hb._query_today_trades()
    assert out["today_trades"] == 10
    assert out["today_pnl"] == pytest.approx(-22.31)
    assert out["today_wins"] == 8


def test_old_entry_date_caliber_would_differ(hb_env):
    """同一数据集按旧口径 (入场日) 归集只剩 7 笔/+191.10 — 锁住差异方向。"""
    _seed_real_827_shape(hb_env)
    conn = sqlite3.connect(str(hb_env))
    stale = conn.execute(
        "SELECT COUNT(*), SUM(realized_pnl) FROM trade_records WHERE trade_date=?",
        (_TODAY,)).fetchone()
    conn.close()
    assert stale[0] == 8 and stale[1] == pytest.approx(191.10)  # 含 1 在仓行 (NULL 盈亏)
    assert hb._query_today_trades()["today_trades"] == 10


def test_open_position_excluded_from_win_denominator(hb_env):
    """在仓行 realized_pnl NULL: 既不计笔数也不进胜率分母 (旧实现会算成"非胜")。"""
    _closed(hb_env, "US.AMD", _TODAY, _TODAY, 50.0)
    _open_row(hb_env, "US.MRK", _TODAY, gross=None)
    out = hb._query_today_trades()
    assert (out["today_trades"], out["today_wins"], out["today_pnl"]) == (1, 1, 50.0)


def test_gross_fallback_when_net_null(hb_env):
    _closed(hb_env, "US.XYZ", _YESTERDAY, _TODAY, None, gross=-120.0)
    out = hb._query_today_trades()
    assert out["today_pnl"] == pytest.approx(-120.0)
    assert out["today_wins"] == 0


def test_yesterday_exits_not_counted(hb_env):
    _closed(hb_env, "US.OLD", _YESTERDAY, _YESTERDAY, 500.0)
    assert hb._query_today_trades()["today_trades"] == 0


def test_positions_still_reported(hb_env):
    conn = sqlite3.connect(str(hb_env))
    conn.execute(
        "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, "
        "current_sl, owner_strategy) VALUES ('US.MU', 'LONG', 6, 946.87, 900.0, NULL)")
    conn.commit()
    conn.close()
    out = hb._query_today_trades()
    assert out["positions"][0]["symbol"] == "US.MU"
    assert out["positions"][0]["owner"] == "?"


def test_query_error_degrades_to_zero(hb_env, monkeypatch):
    """异常路径不抛 (心跳链必须继续推送其他维度)。"""
    class _Bad:
        def connection(self, name):
            raise RuntimeError("db down")
    monkeypatch.setattr(hb, "get_pool", lambda: _Bad())
    assert hb._query_today_trades()["today_trades"] == 0
