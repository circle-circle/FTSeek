"""daily_alert_summary.get_us_trade_summary 出场日记账回归测试 (2026-08-27)。

背景: 日报盈亏原 WHERE trade_date = ? 入场日记账 — 隔夜仓当日出场 (含跳空亏损)
记在入场日, 当日盈亏失真 (8/27 实盘实证: 推送 +332.78 vs 出场日净 +119.37,
隔夜 UBER -307.13/ORCL +90.73/HOOD +2.99 三笔全部漏记)。净口径
COALESCE(pnl_net, realized_pnl) 2026-08-20 已切, 本测试锁定"归集日=出场日"。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


_spec = importlib.util.spec_from_file_location(
    "daily_alert_summary", PROJECT_ROOT / "scripts" / "daily_alert_summary.py"
)
das = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(das)


@pytest.fixture
def us_env(tmp_path, monkeypatch):
    """临时 US_ROOT + 最小 trade_records/open_positions 表"""
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL)"
    )
    conn.execute("CREATE TABLE open_positions (id INTEGER PRIMARY KEY, symbol TEXT, is_filled INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(das, "US_ROOT", tmp_path)
    return db


def _insert(db, symbol, trade_date, exit_time, exit_price, pnl_net=None, realized_pnl=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, pnl_net, realized_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, trade_date, exit_time, exit_price, pnl_net, realized_pnl),
    )
    conn.commit()
    conn.close()


def test_overnight_closed_today_counted(us_env):
    # 8/27 实盘 UBER 案例的最小复现: 入场 8/26, 出场 8/27 跳空亏损
    _insert(us_env, "US.UBER", "2026-08-26", "2026-08-27T09:35:12-04:00", 68.0, -307.13, -306.0)
    s = das.get_us_trade_summary("2026-08-27")
    assert s["trades"] == 1
    assert s["pnl"] == pytest.approx(-307.13)


def test_entry_date_only_rows_not_counted(us_env):
    # 昨天入场昨天出场的行, 与今天无关 (即使入场日不是今天)
    _insert(us_env, "US.AMD", "2026-08-26", "2026-08-26T14:00:00-04:00", 150.0, -50.0, -49.0)
    # 今天入场今天出场 — 计入
    _insert(us_env, "US.COIN", "2026-08-27", "2026-08-27T10:00:00-04:00", 200.0, 101.30, 102.0)
    # 今天入场未出场 (open) — 不计入盈亏也不计数
    _insert(us_env, "US.MRK", "2026-08-27", None, None, None, None)
    s = das.get_us_trade_summary("2026-08-27")
    assert s["trades"] == 1
    assert s["pnl"] == pytest.approx(101.30)
    # 昨天报昨日: 出场日在昨天 → 那笔 -50 计入昨天
    s2 = das.get_us_trade_summary("2026-08-26")
    assert s2["trades"] == 1
    assert s2["pnl"] == pytest.approx(-50.0)


def test_pnl_net_null_falls_back_to_gross(us_env):
    # 旧行 pnl_net=NULL → 回退毛额 realized_pnl
    _insert(us_env, "US.HOOD", "2026-08-27", "2026-08-27T11:00:00-04:00", 40.0, None, 2.99)
    s = das.get_us_trade_summary("2026-08-27")
    assert s["pnl"] == pytest.approx(2.99)


def test_positions_from_open_positions_ssot(us_env):
    conn = sqlite3.connect(str(us_env))
    conn.execute("INSERT INTO open_positions (symbol, is_filled) VALUES ('US.AMD', 1)")
    conn.execute("INSERT INTO open_positions (symbol, is_filled) VALUES ('US.BABA', 0)")
    conn.commit()
    conn.close()
    s = das.get_us_trade_summary("2026-08-27")
    assert s["positions"] == 1


def test_missing_db_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(das, "US_ROOT", tmp_path)  # 有 data 目录但无 db 文件
    (tmp_path / "data").mkdir()
    s = das.get_us_trade_summary("2026-08-27")
    assert s == {"trades": 0, "pnl": 0.0, "positions": 0, "errors": 0}
