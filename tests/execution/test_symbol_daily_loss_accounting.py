"""_check_symbol_daily_loss 出场日记账回归测试 (2026-08-27)。

背景: 标的日亏损限额 (防死亡螺旋, $500/标的/日) 的 DB 自举查询原
WHERE trade_date = ? 入场日记账 — 隔夜仓当日出场亏损 (跳空) 记在入场日,
限额对隔夜跳空失效 (8/27 实盘实证: UBER 出场日 -307.13, 旧口径查到 0)。
同 AccountCircuitBreaker 2026-08-27 修复口径: substr(exit_time,1,10)=当日 ET,
净额 COALESCE(pnl_net, realized_pnl)。

已知边界 (未在本轮修复, 记录在案): 进程内缓存每个 symbol 当日只自举一次,
position_monitor 平仓增量走 _update_symbol_daily_loss; playbook 路径平仓
不增量 — 缓存值可能滞后, 属另一缺陷面。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.pipeline import order_executor as oe
from app.utils.timezone import _et_trade_date, now_et


@pytest.fixture
def loss_env(tmp_path, monkeypatch):
    """临时 DB + 每测试重置进程级缓存"""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL)"
    )
    conn.commit()
    conn.close()

    import app.core.db_utils as dbu
    monkeypatch.setattr(dbu, "get_main_db", lambda: db)

    oe._symbol_daily_loss_cache.clear()
    oe._symbol_daily_loss_ts.clear()
    oe._symbol_daily_loss_date = ""
    yield db
    oe._symbol_daily_loss_cache.clear()
    oe._symbol_daily_loss_ts.clear()
    oe._symbol_daily_loss_date = ""


def _today():
    return _et_trade_date(now_et())


def _insert(db, symbol, trade_date, exit_date, pnl_net=None, realized_pnl=None, exit_price=100.0):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, pnl_net, realized_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, trade_date, f"{exit_date}T10:00:00-04:00" if exit_date else None,
         exit_price if exit_date else None, pnl_net, realized_pnl),
    )
    conn.commit()
    conn.close()


def test_overnight_loss_blocks_reentry(loss_env):
    # 8/27 UBER 案例最小复现: 昨日入场今日出场, 净亏超限 → 当日禁入
    _insert(loss_env, "US.UBER", "2026-08-26", _today(), pnl_net=-600.0, realized_pnl=-595.0)
    assert oe._check_symbol_daily_loss("US.UBER") is False


def test_same_day_loss_still_blocks(loss_env):
    _insert(loss_env, "US.SNDK", _today(), _today(), pnl_net=-500.0)
    assert oe._check_symbol_daily_loss("US.SNDK") is False


def test_yesterday_closed_not_counted_today(loss_env):
    # 昨日出场的亏损不跨日累计 (日限额语义)
    _insert(loss_env, "US.AMD", "2026-08-25", "2026-08-26", pnl_net=-800.0)
    assert oe._check_symbol_daily_loss("US.AMD") is True


def test_pnl_net_null_falls_back_to_gross(loss_env):
    _insert(loss_env, "US.MU", "2026-08-26", _today(), pnl_net=None, realized_pnl=-520.0)
    assert oe._check_symbol_daily_loss("US.MU") is False


def test_small_loss_allows(loss_env):
    _insert(loss_env, "US.ORCL", "2026-08-26", _today(), pnl_net=-100.0)
    assert oe._check_symbol_daily_loss("US.ORCL") is True


def test_open_position_not_counted(loss_env):
    # 未出场行 (exit_time NULL) 不影响当日已实现亏损
    _insert(loss_env, "US.MRK", _today(), None, pnl_net=None, realized_pnl=None, exit_price=None)
    assert oe._check_symbol_daily_loss("US.MRK") is True


def test_db_error_fail_open(loss_env, monkeypatch):
    import app.core.db_utils as dbu
    monkeypatch.setattr(dbu, "get_main_db", lambda: "/nonexistent/dir/x.db")
    assert oe._check_symbol_daily_loss("US.AMD") is True


# ── TTL: playbook 平仓不增量, 缓存到期必须重查 DB (SSOT) ─────────────────

def test_stale_cache_refreshed_from_db(loss_env, monkeypatch):
    # 2026-08-28 缺陷复现: 首查后 playbook 路径平仓落 DB (-800), 增量不进
    # 缓存 — TTL 内旧值放行 (已知边界), TTL 过期必须重查 DB 拦下。
    _insert(loss_env, "US.SNDK", _today(), _today(), pnl_net=-100.0)
    assert oe._check_symbol_daily_loss("US.SNDK") is True  # 首查自举 -100
    _insert(loss_env, "US.SNDK", _today(), _today(), pnl_net=-700.0)  # playbook 平仓
    # TTL 内: 缓存值 -100 放行 (即时性靠增量, 已知滞后窗口)
    assert oe._check_symbol_daily_loss("US.SNDK") is True
    # TTL 过期: 强制重查 DB → -800 超限拦下
    monkeypatch.setattr(oe, "_SYMBOL_DAILY_LOSS_TTL", -1.0)
    assert oe._check_symbol_daily_loss("US.SNDK") is False


def test_increment_refresh_within_ttl(loss_env, monkeypatch):
    # position_monitor 增量路径: TTL 内增量即时生效 (不等 DB 重查)
    _insert(loss_env, "US.SNDK", _today(), _today(), pnl_net=-100.0)
    assert oe._check_symbol_daily_loss("US.SNDK") is True
    oe._update_symbol_daily_loss("US.SNDK", -450.0)  # 增量 → 缓存 -550
    assert oe._check_symbol_daily_loss("US.SNDK") is False


def test_ttl_refresh_resets_clock(loss_env, monkeypatch):
    # TTL 过期重查后时间戳重置 — 不会每次检查都打 DB
    monkeypatch.setattr(oe, "_SYMBOL_DAILY_LOSS_TTL", 300.0)
    _insert(loss_env, "US.MU", _today(), _today(), pnl_net=-10.0)
    assert oe._check_symbol_daily_loss("US.MU") is True
    ts1 = oe._symbol_daily_loss_ts["US.MU"]
    monkeypatch.setattr(oe, "_SYMBOL_DAILY_LOSS_TTL", -1.0)
    assert oe._check_symbol_daily_loss("US.MU") is True  # 过期重查
    ts2 = oe._symbol_daily_loss_ts["US.MU"]
    assert ts2 >= ts1
    monkeypatch.setattr(oe, "_SYMBOL_DAILY_LOSS_TTL", 300.0)
    # 重查后时钟重置 → TTL 内纯缓存命中
    assert oe._check_symbol_daily_loss("US.MU") is True
