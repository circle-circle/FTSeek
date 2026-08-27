"""_check_symbol_cooldown 出场日记账 + 净额回归测试 (2026-08-28)。

背景: 标的冷却门 (24h 内同方向连续亏损 ≥3 次 → 冷却 24h) 的 DB 查询原
WHERE trade_date >= cutoff (入场日记账) + pnl_pct<0 毛盈亏判据 — 前日入场
今日割的隔夜仓不计入连续亏损计数, 门在隔夜跳空日失明。
同 _check_symbol_daily_loss / AccountCircuitBreaker 2026-08-27 修复口径:
substr(exit_time,1,10) 出场日归集 + COALESCE(pnl_net, realized_pnl) 净额。

阈值来自 config symbol_cooldown.structure_exit (consecutive_losses=3,
cooldown_hours=24), 测试不依赖注入值而是读同一配置 (与本门语义绑定)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.pipeline import order_executor as oe
from app.utils.timezone import now_et

_THRESHOLD = 3  # config symbol_cooldown.structure_exit.consecutive_losses


@pytest.fixture
def cd_env(tmp_path, monkeypatch):
    """临时 DB + 每测试清进程级冷却缓存 (否则上一条用例写入的冷却期串到下一条)"""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, trade_date TEXT, "
        "exit_time TEXT, exit_price REAL, pnl_pct REAL, pnl_net REAL, "
        "realized_pnl REAL, exit_reason TEXT)"
    )
    conn.commit()
    conn.close()

    import app.core.db_utils as dbu
    monkeypatch.setattr(dbu, "get_main_db", lambda: db)
    oe._symbol_cooldown_cache.clear()
    yield db
    oe._symbol_cooldown_cache.clear()


_TODAY = now_et().strftime("%Y-%m-%d")
# 本门窗口 = "出场日 >= (now-24h 的日期)": 昨日+今日在窗内, 前日在窗外
_IN_WINDOW = [(now_et() - timedelta(days=d)).strftime("%Y-%m-%d") for d in (0, 1)]
_OUT_OF_WINDOW = (now_et() - timedelta(days=3)).strftime("%Y-%m-%d")


def _insert(db, symbol, direction, entry_date, exit_date, *, pnl_net=None,
            realized_pnl=None, pnl_pct=-0.02, exit_price=100.0):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, trade_date, exit_time, "
        "exit_price, pnl_pct, pnl_net, realized_pnl, exit_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, direction, entry_date,
         f"{exit_date}T10:00:00-04:00" if exit_date else None,
         exit_price if exit_date else None, pnl_pct, pnl_net, realized_pnl,
         "DAYTRADE_STOP"),
    )
    conn.commit()
    conn.close()


def _rows_for(db, symbol, direction, entry_date, exit_dates, **pnl):
    for ed in exit_dates:
        _insert(db, symbol, direction, entry_date, ed, **pnl)


def test_overnight_losses_count_toward_cooldown(cd_env):
    """核心回归: 前日入场/昨今出场的 3 笔隔夜割 → 冷却触发 (旧口径计数 0 → 放行)。

    entry_date 在窗外 (3 天前), 出场日在窗内 → 只有出场日记账能看到。
    """
    _rows_for(cd_env, "US.UBER", "LONG", _OUT_OF_WINDOW, _IN_WINDOW[:2] + [_TODAY],
              pnl_net=-300.0, realized_pnl=-295.0)
    assert oe._check_symbol_cooldown("US.UBER", "LONG") is False


def test_gross_positive_net_negative_counts_as_loss(cd_env):
    """净额判据: 毛+7.06/净-2.31 的保本割 (RIVN 8/27 实录) 算亏损笔。"""
    _rows_for(cd_env, "US.RIVN", "LONG", _TODAY, [_TODAY] * 3,
              pnl_net=-2.31, realized_pnl=7.06)
    assert oe._check_symbol_cooldown("US.RIVN", "LONG") is False


def test_net_positive_not_counted(cd_env):
    """毛净同为正 → 不计入亏损, 未达阈值放行。"""
    _rows_for(cd_env, "US.MU", "LONG", _TODAY, [_TODAY] * 3,
              pnl_net=2.31, realized_pnl=7.06)
    assert oe._check_symbol_cooldown("US.MU", "LONG") is True


def test_below_threshold_allows(cd_env):
    _rows_for(cd_env, "US.AMD", "LONG", _TODAY, [_TODAY] * (_THRESHOLD - 1),
              pnl_net=-100.0)
    assert oe._check_symbol_cooldown("US.AMD", "LONG") is True


def test_out_of_window_exit_not_counted(cd_env):
    """出场日在 24h 窗外 → 不累计 (冷却窗日语义, 日级近似为原设计)。"""
    _rows_for(cd_env, "US.ORCL", "LONG", _OUT_OF_WINDOW, [_OUT_OF_WINDOW] * 3,
              pnl_net=-100.0)
    assert oe._check_symbol_cooldown("US.ORCL", "LONG") is True


def test_open_position_excluded(cd_env):
    """未出场行 (exit_price/exit_time 空) 不进冷却计数。"""
    _rows_for(cd_env, "US.MRK", "LONG", _TODAY, [None] * 3,
              pnl_net=None, realized_pnl=None, exit_price=None)
    assert oe._check_symbol_cooldown("US.MRK", "LONG") is True


def test_direction_isolated(cd_env):
    """同标的反向亏损不叠加 (direction 等值过滤仍生效)。"""
    _rows_for(cd_env, "US.SNDK", "LONG", _TODAY, [_TODAY] * 3, pnl_net=-100.0)
    assert oe._check_symbol_cooldown("US.SNDK", "SHORT") is True


def test_existing_cooldown_cache_blocks(cd_env):
    """缓存内冷却未到期 → 不查库直接拒 (原行为, 防回归)。"""
    oe._symbol_cooldown_cache["US.COOL_LONG"] = now_et().timestamp() + 3600
    assert oe._check_symbol_cooldown("US.COOL", "LONG") is False


def test_db_error_fail_open(cd_env, monkeypatch):
    """查询异常放行 = 原 fail-open 策略 (本门非资金安全底线, 刻意保守侧不同于熔断)。"""
    import app.core.db_utils as dbu
    monkeypatch.setattr(dbu, "get_main_db", lambda: "/nonexistent/dir/x.db")
    assert oe._check_symbol_cooldown("US.AMD", "LONG") is True
