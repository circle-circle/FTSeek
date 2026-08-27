"""复盘/盘后/CLI 三站 "date(exit_time) → substr" 口径统一回归测试 (2026-08-28)。

背景: 全仓"当日"自 2026-08-27/28 起统一 = 出场日 substr(exit_time,1,10) 的
**ET 墙钟日** + 净额 COALESCE(pnl_net, realized_pnl)。本批是最后一组分叉:
daily_review._analyze_shadow_vs_real / _analyze_coordinator、
catalyst.postmarket_review._get_actual_returns、scripts/coordinator_status
用 SQLite date(exit_time) —— date() 把行内 -04:00 偏移归一到 UTC (+4h),
20:00 ET 之后出场的行 date()=次日, 与传进来的 ET 交易日参数错位:
复盘数字漏一笔 / 盘后催化剂实际收益静默取不到 (全库 2 行已实际分叉)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.review.daily_review import DailyReviewGenerator
from app.utils.timezone import get_trade_date

_TODAY = get_trade_date()
# 21:30 ET = 次日 01:30 UTC → date() 与 substr 在这一行上必然分叉
_LATE = f"{_TODAY}T21:30:00-04:00"
_NORMAL = f"{_TODAY}T15:40:00-04:00"


@pytest.fixture
def dr_env(tmp_path, monkeypatch):
    """临时 trade_records + 隔离 decisions/rejections/output (不碰生产路径)"""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, realized_pnl REAL, pnl_net REAL, pnl_pct REAL, "
        "owner_strategy TEXT)"
    )
    # _analyze_coordinator 第 2 步读活跃持仓 (生产库有, 临时库须建)
    conn.execute(
        "CREATE TABLE open_positions (id INTEGER PRIMARY KEY, symbol TEXT, "
        "is_filled INTEGER, is_anomalous INTEGER, owner_strategy TEXT)"
    )
    conn.commit()
    conn.close()

    decisions = tmp_path / "decisions.db"
    sqlite3.connect(str(decisions)).close()

    import app.core.paths as paths
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: db)
    monkeypatch.setattr(paths, "get_decisions_db", lambda: decisions)
    monkeypatch.setattr(paths, "get_rejections_db", lambda: decisions)
    monkeypatch.setattr(paths, "get_reviews_dir", lambda: tmp_path / "reviews")
    import app.review.daily_review as dr
    monkeypatch.setattr(dr, "get_trade_records_db", lambda: db)
    monkeypatch.setattr(dr, "get_decisions_db", lambda: decisions)
    return db


def _closed(db, symbol, exit_time, *, net, gross=None, owner="playbook",
            trade_date="2026-08-26", pnl_pct=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "realized_pnl, pnl_net, pnl_pct, owner_strategy) VALUES (?,?,?,?,?,?,?,?)",
        (symbol, trade_date, exit_time, 100.0, gross if gross is not None else net,
         net, pnl_pct if pnl_pct is not None else net / 1000.0, owner),
    )
    conn.commit()
    conn.close()


# ── 1. daily_review._analyze_shadow_vs_real ────────────────────────

def test_shadow_vs_real_is_exit_day_and_net(dr_env):
    """隔夜仓 (昨入今出) 计入当日 + 净额优先 (毛+7/净-2.31 记为负笔)。"""
    _closed(dr_env, "US.UBER", _NORMAL, net=-307.13, gross=-302.60)
    _closed(dr_env, "US.RIVN", _NORMAL, net=-2.31, gross=7.06)
    real = DailyReviewGenerator()._analyze_shadow_vs_real(_TODAY)["real"]
    assert real["n"] == 2
    assert real["total_pnl"] == pytest.approx(-309.44)   # 净额, 非 -295.54 毛额
    assert real["win_rate"] == pytest.approx(0.0)        # 净口径下两笔皆负


def test_shadow_vs_real_late_et_exit_counted(dr_env):
    """21:30 ET 出场: date()=明日 (静默丢数), substr=今日。"""
    _closed(dr_env, "US.LATE", _LATE, net=-500.0)
    conn = sqlite3.connect(str(dr_env))
    utc_day, et_day = conn.execute(
        "SELECT date(exit_time), substr(exit_time,1,10) FROM trade_records").fetchone()
    conn.close()
    assert utc_day != _TODAY and et_day == _TODAY        # 前提: 两语义确已分叉
    assert DailyReviewGenerator()._analyze_shadow_vs_real(_TODAY)["real"]["n"] == 1


def test_shadow_vs_real_open_row_excluded(dr_env):
    conn = sqlite3.connect(str(dr_env))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "realized_pnl, pnl_net, owner_strategy) VALUES ('US.MU', ?, ?, NULL, NULL, NULL, 'x')",
        (_TODAY, _TODAY))
    conn.commit()
    conn.close()
    assert DailyReviewGenerator()._analyze_shadow_vs_real(_TODAY)["real"]["n"] == 0


# ── 2. daily_review._analyze_coordinator (owner 分布) ───────────────

def test_owner_distribution_is_exit_day(dr_env):
    _closed(dr_env, "US.UBER", _LATE, net=-10.0, owner="playbook_v2")
    _closed(dr_env, "US.MU", _NORMAL, net=20.0, owner="main_pipeline")
    out = DailyReviewGenerator()._analyze_coordinator(_TODAY)
    assert out["owner_strategy_distribution"] == {"playbook_v2": 1, "main_pipeline": 1}


# ── 3. catalyst.postmarket_review._get_actual_returns ──────────────

def test_postmarket_actual_return_found_for_late_exit(tmp_path, monkeypatch):
    """盘后催化剂评审: 晚出场 (20:00 ET 后) 的实际收益必须取到, 不能静默跳过。"""
    spec = importlib.util.spec_from_file_location(
        "postmarket_review_probe",
        PROJECT_ROOT / "app" / "catalyst" / "postmarket_review.py",
    )
    pmr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pmr)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, "
        "trade_date TEXT, exit_time TEXT, exit_price REAL, pnl_pct REAL)"
    )
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, pnl_pct) "
        "VALUES ('US.BABA', ?, ?, 100.0, 0.037)", (_TODAY, _LATE))
    conn.commit()
    conn.close()
    monkeypatch.setattr(pmr, "PROJECT_ROOT", tmp_path)
    assert pmr._get_actual_returns([{"symbol": "US.BABA"}]) == {"US.BABA": 0.037}


# ── 4. scripts/coordinator_status CLI ──────────────────────────────

def test_coordinator_status_cli_today_is_et_day(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "coordinator_status_probe", PROJECT_ROOT / "scripts" / "coordinator_status.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)

    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, "
        "trade_date TEXT, exit_time TEXT, exit_price REAL, owner_strategy TEXT)"
    )
    conn.execute("CREATE TABLE open_positions (id INTEGER PRIMARY KEY, "
                 "owner_strategy TEXT, is_filled INTEGER, is_anomalous INTEGER)")
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, owner_strategy) "
        "VALUES ('US.LATE', ?, ?, 100.0, 'playbook_v2')", (_TODAY, _LATE))
    conn.commit()
    conn.close()
    import app.core.paths as paths
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: db)

    cs.cmd_owner_summary()
    out = capsys.readouterr().out
    assert "playbook_v2     1 笔" in out
