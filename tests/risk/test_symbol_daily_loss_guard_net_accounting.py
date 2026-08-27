"""SymbolDailyLossGuard.restore_from_db 净额口径回归测试 (2026-08-28)。

背景: 这是与 order_executor._check_symbol_daily_loss ($500/标的/日) 并存的第二套
标的日亏损防线 (%净值 + 连续亏损 + 胜率)。restore 原取毛额 realized_pnl —
每笔少计 ~$5 费 (费随笔数累积), max_daily_loss_pct 与胜率两档都比兄弟门晚触发;
且"毛+7/净-2.31"的保本割在毛口径下被记成胜笔。日归属本已是出场日 (48h 窗 +
ET 日过滤), 故本轮只统一净额尺子 (同 AccountCircuitBreaker / _check_symbol_daily_loss)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import app.risk.symbol_daily_loss_guard as mod
from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
from app.utils.timezone import get_trade_date, now_et

_TODAY = get_trade_date()
_YESTERDAY = (now_et() - timedelta(days=1)).strftime("%Y-%m-%d")
_OUT_48H = (now_et() - timedelta(days=3)).strftime("%Y-%m-%d")


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL)"
    )
    conn.commit()
    conn.close()
    import app.core.paths as paths
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: db)
    # 非交易日直接跳过重建 → 测试固定按交易日跑
    monkeypatch.setattr(mod, "_is_us_trading_day", lambda: True)
    return db


def _closed(db, symbol, exit_date, net, gross=None, *, trade_date=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, trade_date or exit_date, f"{exit_date}T10:00:00-04:00", 100.0,
         net, gross if gross is not None else net),
    )
    conn.commit()
    conn.close()


def _guard(equity=100_000.0, **kw):
    g = SymbolDailyLossGuard(**kw)
    g.set_equity_provider(lambda: equity)
    return g


def test_net_loss_triggers_where_gross_would_not(guard_env):
    """核心回归: 毛亏 $498 (<0.5% 净值) 但净亏 $503 (超限) → 净额口径拦, 毛口径放行。"""
    _closed(guard_env, "US.UBER", _TODAY, -503.0, -498.0)
    g = _guard()
    g.restore_from_db()
    assert g.is_blocked("US.UBER")[0] is True


def test_overnight_exit_attributed_today(guard_env):
    """昨日入场/今日出场的隔夜割计入今日 (出场日归属, 本轮沿用)。"""
    _closed(guard_env, "US.ORCL", _TODAY, -300.0, trade_date=_YESTERDAY)
    g = _guard()
    g.restore_from_db()
    snap = {s["symbol"]: s for s in g.snapshot()}
    assert snap["US.ORCL"]["trades"] == 1
    assert snap["US.ORCL"]["total_pnl"] == pytest.approx(-300.0)


def test_gross_win_net_loss_counted_as_loss(guard_env):
    """"毛+7.06/净-2.31" 保本割 (RIVN 8/27 实录): 净口径下是亏损笔, 不再是胜笔。"""
    _closed(guard_env, "US.RIVN", _TODAY, -2.31, 7.06)
    g = _guard()
    g.restore_from_db()
    snap = {s["symbol"]: s for s in g.snapshot()}
    assert snap["US.RIVN"]["wins"] == 0
    assert snap["US.RIVN"]["consecutive_losses"] == 1


def test_win_rate_uses_net(guard_env):
    """胜率档 (≥3 笔且亏损率 >66%): 净口径把 2 笔"毛胜净负"如实算作负 → 触发。"""
    for net, gross in ((-2.31, 7.06), (-1.50, 6.00), (50.0, 54.0)):
        _closed(guard_env, "US.SNDK", _TODAY, net, gross)
    g = _guard(max_daily_loss_pct=9.9)  # 关停额度档, 单测胜率档
    g.restore_from_db()
    blocked, reason = g.is_blocked("US.SNDK")
    assert blocked is True and "loss_rate" in reason


def test_gross_caliber_would_not_trigger_same_case(guard_env):
    """同数据集在毛口径下亏损率仅 1/3 → 不触发 (锁住修复的边际效果)。"""
    for _, gross in ((-2.31, 7.06), (-1.50, 6.00), (50.0, 54.0)):
        _closed(guard_env, "US.MU", _TODAY, gross, gross)  # pnl_net := 毛值
    g = _guard(max_daily_loss_pct=9.9)
    g.restore_from_db()
    assert g.is_blocked("US.MU")[0] is False


def test_pnl_net_null_falls_back_to_gross(guard_env):
    """旧行无 pnl_net → 回退毛额, 不丢状态。"""
    _closed(guard_env, "US.XYZ", _TODAY, None, -600.0)
    g = _guard()
    g.restore_from_db()
    assert g.is_blocked("US.XYZ")[0] is True


def test_all_pnl_null_row_skipped(guard_env):
    """净额/毛额皆 NULL (在仓行) 不参与重建。"""
    conn = sqlite3.connect(str(guard_env))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl) VALUES ('US.MRK', ?, ?, 100.0, NULL, NULL)",
        (_TODAY, f"{_TODAY}T10:00:00-04:00"),
    )
    conn.commit()
    conn.close()
    g = _guard()
    g.restore_from_db()
    assert g.snapshot() == []


def test_out_of_window_not_restored(guard_env):
    """出场在 48h 窗外 → 不属今日。"""
    _closed(guard_env, "US.OLD", _OUT_48H, -900.0)
    g = _guard()
    g.restore_from_db()
    assert g.is_blocked("US.OLD")[0] is False
