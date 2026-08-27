"""面板 _calc_pnl "今日已实现"出场日记账 + 净额回归测试 (2026-08-28)。

背景: 面板头部今日盈亏/今日笔数原 WHERE trade_date=? (入场日) + realized_pnl
(毛额) — 隔夜仓今日了结不计入且费用不可见 (8/27 实证: 面板 10 笔/+381.15 vs
出场日净额 13 笔/+119.37, 与日报/心跳/熔断四个口径互相矛盾)。

刻意保留的口径边界 (本测试同时锁住, 改动需用户裁决):
  今日 = 出场日 + 净额;  all-time (realized_all/total 头条) = 毛额 realized_pnl
  — 全期改净会重述历史头条数字 (2026-08-20 净口径切换只作用于"当日/单笔"),
  故本轮不动, 由测试显式记录这一分叉而不是留成隐性 bug。

回滚: 删本文件即可, 零生产代码依赖。
"""
import asyncio
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.web_dashboard.routes.dashboard import _calc_pnl
from app.web_dashboard.data_service import et_today
from app.utils.timezone import now_et

_TODAY = et_today()
_YESTERDAY = (now_et() - timedelta(days=1)).strftime("%Y-%m-%d")


class _StubDS:
    """替身 DataService — 只实现 _calc_pnl 用到的 db_query (真实现吞异常返 [])."""

    def __init__(self, db):
        self.db = str(db)

    async def db_query(self, sql, params=(), db_path=None):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


@pytest.fixture
def panel_db(tmp_path):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL, trade_mode TEXT, "
        "source_type TEXT)"
    )
    conn.commit()
    conn.close()
    return db


def _closed(db, symbol, exit_date, net, gross, *, trade_date=None,
            trade_mode="simulate", source_type="PLAYBOOK_SIM"):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl, trade_mode, source_type) VALUES (?,?,?,?,?,?,?,?)",
        (symbol, trade_date or exit_date, f"{exit_date}T15:40:00-04:00", 100.0,
         net, gross, trade_mode, source_type),
    )
    conn.commit()
    conn.close()


def _run(coro):
    return asyncio.run(coro)


def test_overnight_close_shown_in_today(panel_db):
    """核心回归: 昨日入场/今日了结的隔夜仓计入面板"今日"。"""
    _closed(panel_db, "US.UBER", _TODAY, -307.13, -302.60, trade_date=_YESTERDAY)
    pnl = _run(_calc_pnl(_StubDS(panel_db), []))
    assert pnl["today_trades"] == 1
    assert pnl["realized"] == pytest.approx(-307.13)
    assert pnl["today_realized_sim"] == pytest.approx(-307.13)


def test_today_uses_net_and_old_caliber_differs(panel_db):
    """同数据集: 今日净额口径 10 笔/-22.31 (8/27 形态), 旧入场日毛额口径 7 笔/+225.92。"""
    for sym, td, net, gross in (
            ("US.UBER", _YESTERDAY, -307.13, -302.60),
            ("US.ORCL", _YESTERDAY, 90.73, 95.08),
            ("US.HOOD", _YESTERDAY, 2.99, 7.42),
    ):
        _closed(panel_db, sym, _TODAY, net, gross, trade_date=td)
    for i, (net, gross) in enumerate(((-2.31, 7.06), (101.30, 105.60), (61.01, 65.34),
                                      (5.13, 9.30), (9.20, 13.38), (12.96, 17.10),
                                      (3.81, 8.14))):
        _closed(panel_db, f"US.D{i}", _TODAY, net, gross, trade_date=_TODAY)
    pnl = _run(_calc_pnl(_StubDS(panel_db), []))
    assert pnl["today_trades"] == 10
    assert pnl["realized"] == pytest.approx(-22.31)
    # 旧口径 (入场日 + 毛额) 会报的数字 —— 差异方向锁死, 防回退
    conn = sqlite3.connect(str(panel_db))
    stale = conn.execute(
        "SELECT COUNT(*), SUM(realized_pnl) FROM trade_records WHERE trade_date=?",
        (_TODAY,)).fetchone()
    conn.close()
    assert stale == (7, pytest.approx(225.92))


def test_open_position_excluded(panel_db):
    _closed(panel_db, "US.MU", _TODAY, 10.0, 15.0)
    conn = sqlite3.connect(str(panel_db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl, trade_mode, source_type) "
        "VALUES ('US.MRK', ?, ?, NULL, NULL, NULL, 'simulate', 'PLAYBOOK_SIM')",
        (_TODAY, _TODAY))
    conn.commit()
    conn.close()
    pnl = _run(_calc_pnl(_StubDS(panel_db), []))
    assert pnl["today_trades"] == 1


def test_live_and_simulate_split(panel_db):
    """trade_mode 拆分 (全库真值: live/sim/simulate)。"""
    _closed(panel_db, "US.L", _TODAY, -50.0, -45.0, trade_mode="live")
    _closed(panel_db, "US.S", _TODAY, 30.0, 35.0, trade_mode="simulate")
    pnl = _run(_calc_pnl(_StubDS(panel_db), []))
    assert pnl["today_realized_live"] == pytest.approx(-50.0)
    assert pnl["today_realized_sim"] == pytest.approx(30.0)
    assert pnl["realized_sim"] == pytest.approx(35.0)   # all-time 毛额 - simulate 桶
    assert pnl["realized_live"] == pytest.approx(-45.0)


def test_excluded_source_types_still_filtered(panel_db):
    """source_type 黑名单过滤不受本轮影响。"""
    _closed(panel_db, "US.SHADOW", _TODAY, 999.0, 999.0,
            source_type="PLAYBOOK_SHADOW")
    pnl = _run(_calc_pnl(_StubDS(panel_db), []))
    assert pnl["today_trades"] == 0
    assert pnl["realized"] == 0


def test_all_time_headline_stays_gross_by_design(panel_db):
    """口径边界显式记录: all-time (total/realized_all) 仍毛额 — 改净需用户裁决重述。"""
    _closed(panel_db, "US.RIVN", _TODAY, -2.31, 7.06)
    pnl = _run(_calc_pnl(_StubDS(panel_db), []))
    assert pnl["realized"] == pytest.approx(-2.31)   # 今日: 净额
    assert pnl["realized_all"] == pytest.approx(7.06)  # 全期: 毛额 (有意分叉)
