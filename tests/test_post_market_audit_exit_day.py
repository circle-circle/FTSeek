"""post_market_audit 今日归因出场日记账回归测试 (2026-08-28)。

背景: 盘后归因报告的"今日"桶 (费用净额总览 + 策略归因 today_cnt/today_pnl)
原按 trade_date (入场日) 归集 — 隔夜仓今日了结的盈亏在"今日"里整笔消失,
与同报告其他段及日报/熔断口径互相矛盾。7 日窗口按入场日分桶属边界语义
(报告标题即"近 N 日入场交易"), 有意保留不动。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

_spec = importlib.util.spec_from_file_location(
    "post_market_audit", PROJECT_ROOT / "scripts" / "post_market_audit.py"
)
pma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pma)

from app.utils.timezone import now_et  # noqa: E402

_TODAY = now_et().strftime("%Y-%m-%d")
_YESTERDAY = (now_et() - timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture
def audit_db(tmp_path):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, strategy_id TEXT, trade_date TEXT, "
        "exit_time TEXT, realized_pnl REAL, pnl_net REAL, fees_est REAL, "
        "holding_hours REAL)"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row  # 同脚本 get_db(), 归因函数按列名取值
    yield conn
    conn.close()


def _closed(db, symbol, strategy, trade_date, exit_date, net, gross, fees):
    db.execute(
        "INSERT INTO trade_records (symbol, strategy_id, trade_date, exit_time, "
        "realized_pnl, pnl_net, fees_est, holding_hours) VALUES (?,?,?,?,?,?,?,?)",
        (symbol, strategy, trade_date, f"{exit_date}T09:31:00-04:00",
         gross, net, fees, 12.5),
    )
    db.commit()


def test_fee_net_today_bucket_is_exit_day(audit_db):
    """核心回归: 隔夜跳空割 (昨入今出) 计入"今日"毛/费/净总览。"""
    _closed(audit_db, "US.UBER", "playbook", _YESTERDAY, _TODAY, -307.13, -302.60, 4.53)
    out = pma.attribution_fee_net(audit_db, _TODAY)["today"]
    assert out["cnt"] == 1
    assert out["net"] == pytest.approx(-307.13)
    assert out["gross"] == pytest.approx(-302.60)
    assert out["fees"] == pytest.approx(4.53)


def test_fake_win_flagged(audit_db):
    """假盈利单 (毛>0 净<=0, RIVN 8/27 实录) 在"今日"桶被抓出。"""
    _closed(audit_db, "US.RIVN", "playbook", _TODAY, _TODAY, -2.31, 7.06, 9.37)
    out = pma.attribution_fee_net(audit_db, _TODAY)["today"]
    assert out["fake_wins"] == 1
    assert out["net_win_rate"] == 0.0


def test_yesterday_exit_not_in_today(audit_db):
    _closed(audit_db, "US.OLD", "playbook", "2026-08-25", _YESTERDAY, 80.0, 84.0, 4.0)
    assert pma.attribution_fee_net(audit_db, _TODAY)["today"]["cnt"] == 0


def test_window_bucket_keeps_entry_date_semantics(audit_db):
    """7 日窗按入场日分桶 (本轮有意不动) — 昨入今出的行同时出现在窗与今日。"""
    _closed(audit_db, "US.UBER", "playbook", _YESTERDAY, _TODAY, -307.13, -302.60, 4.53)
    out = pma.attribution_fee_net(audit_db, _TODAY)
    assert out["window"]["cnt"] == 1 and out["today"]["cnt"] == 1


def test_strategy_today_bucket_is_exit_day(audit_db):
    """策略归因 today_cnt/today_pnl 同口径: 隔夜割归到今日的策略行上。"""
    _closed(audit_db, "US.UBER", "playbook_v2", _YESTERDAY, _TODAY, -307.13, -302.60, 4.53)
    _closed(audit_db, "US.MU", "playbook_v2", _TODAY, _TODAY, 101.30, 105.60, 4.30)
    rows = {r["strategy"]: r for r in pma.attribution_strategy(audit_db, _TODAY)}
    assert rows["playbook_v2"]["today_cnt"] == 2
    assert rows["playbook_v2"]["today_pnl"] == pytest.approx(-205.83)
    assert rows["playbook_v2"]["cnt"] == 2  # 窗口口径 (入场日) 本轮不变
