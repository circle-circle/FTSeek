"""pre_trade_gate._check_daily_drawdown 出场日记账 + 净额回归测试 (2026-08-28)。

背景: 账户日回撤熔断 (全仓唯一"含手动仓"的 3% 门, fail-close) 原
SUM(realized_pnl) 毛额 + WHERE substr(trade_date...) 入场日归集 —
① 隔夜仓今日跳空了结不计入当日回撤; ② 每笔少计 ~$5 费使门偏松。
本轮统一到全仓口径 (同 AccountCircuitBreaker): substr(exit_time,1,10) + 净额。

刻意不用 SQLite date(exit_time): 它把行内 -04:00 偏移归一到 UTC, 与全仓
"ET 墙钟日前 10 位"约定是两种日语义 (20:00 ET 后出场会差一天) — 见
test_late_et_exit_attributed_to_et_day。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import app.risk.pre_trade_gate as ptg
from app.utils.timezone import get_trade_date, now_et

_TODAY = get_trade_date()
_YESTERDAY = (now_et() - timedelta(days=1)).strftime("%Y-%m-%d")
_EQUITY = 100_000.0
_DD = 0.03  # config risk.account.max_daily_drawdown → $3000


@pytest.fixture
def dd_env(tmp_path, monkeypatch):
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
    ptg._daily_pnl_cache = {"ts": 0, "pnl": 0.0, "date": ""}
    yield db
    ptg._daily_pnl_cache = {"ts": 0, "pnl": 0.0, "date": ""}


def _closed(db, symbol, exit_date, net, gross=None, *, hhmm="10:00:00",
            offset="-04:00", trade_date=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, trade_date or exit_date,
         f"{exit_date}T{hhmm}{offset}", 100.0, net,
         gross if gross is not None else net),
    )
    conn.commit()
    conn.close()


def test_overnight_realized_loss_counts_today(dd_env):
    """核心回归: 昨日入场/今日跳空割 -$3500 → 当日回撤熔断拒单 (旧口径看不见)。"""
    _closed(dd_env, "US.UBER", _TODAY, -3500.0, trade_date=_YESTERDAY)
    allowed, reason = ptg._check_daily_drawdown(_EQUITY)
    assert allowed is False and "日回撤熔断" in reason


def test_net_pushes_over_threshold_where_gross_does_not(dd_env):
    """10 笔毛亏 $298 (=2980, 门内) → 净 $303 (=3030, 越界): 费用使门更紧。"""
    for i in range(10):
        _closed(dd_env, f"US.S{i}", _TODAY, -303.0, -298.0)
    assert ptg._check_daily_drawdown(_EQUITY)[0] is False


def test_gross_below_threshold_would_allow(dd_env):
    """同规模但无费 (净=毛) → -2980 在 3% 门内放行 (锁住上例的归因: 差异来自费)。"""
    for i in range(10):
        _closed(dd_env, f"US.T{i}", _TODAY, -298.0, -298.0)
    assert ptg._check_daily_drawdown(_EQUITY)[0] is True


def test_late_et_exit_attributed_to_et_day(dd_env):
    """21:30 ET 出场 (-04:00 行内偏移): substr=ET 今日, 而 date() 归 UTC=明日。
    全仓约定 ET 墙钟日 → 本门必须计入今日回撤。"""
    _closed(dd_env, "US.LATE", _TODAY, -4000.0, hhmm="21:30:00")
    conn = sqlite3.connect(str(dd_env))
    utc_day, et_day = conn.execute(
        "SELECT date(exit_time), substr(exit_time,1,10) FROM trade_records").fetchone()
    conn.close()
    assert utc_day != _TODAY and et_day == _TODAY  # 前提: 两种日语义确已分叉
    assert ptg._check_daily_drawdown(_EQUITY)[0] is False


def test_yesterday_exit_not_in_today_drawdown(dd_env):
    _closed(dd_env, "US.OLD", _YESTERDAY, -9000.0, trade_date=_YESTERDAY)
    assert ptg._check_daily_drawdown(_EQUITY)[0] is True


def test_open_position_excluded(dd_env):
    """在仓行 (exit NULL) 不入已实现侧; 浮亏另经 broker_adapter 计入。"""
    conn = sqlite3.connect(str(dd_env))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl) VALUES ('US.MU', ?, NULL, NULL, NULL, NULL)",
        (_TODAY,))
    conn.commit()
    conn.close()
    assert ptg._check_daily_drawdown(_EQUITY)[0] is True


def test_broker_unrealized_still_adds(dd_env):
    """未实现浮亏 (含手动仓) 叠加已实现 → 合计越界拒单 (2026-06-19 规格不变)。"""
    _closed(dd_env, "US.AMD", _TODAY, -1000.0)

    class _Broker:
        def get_positions(self):
            return [{"qty": 100, "unrealized_pnl": -2500.0},
                    {"qty": 0, "unrealized_pnl": -9999.0}]

    assert ptg._check_daily_drawdown(_EQUITY, _Broker())[0] is False


def test_cache_hit_avoids_second_query(dd_env, monkeypatch):
    """60s TTL 缓存语义不变 (TTL 内第二次调用不查库)。"""
    _closed(dd_env, "US.MU", _TODAY, -100.0)
    assert ptg._check_daily_drawdown(_EQUITY)[0] is True
    monkeypatch.setattr(ptg, "_DAILY_PNL_TTL", 3600)
    _closed(dd_env, "US.BIG", _TODAY, -99_000.0)
    assert ptg._check_daily_drawdown(_EQUITY)[0] is True  # 仍读缓存 (已知滞后窗)


def test_db_error_fail_close(dd_env, tmp_path, monkeypatch):
    """fail-close 不变: 查询异常 → 拒单 (路径存在但非合法 DB)。"""
    import app.core.db_utils as dbu
    bogus = tmp_path / "not_a_db.db"
    bogus.write_text("this is not sqlite")
    monkeypatch.setattr(dbu, "get_main_db", lambda: bogus)
    allowed, reason = ptg._check_daily_drawdown(_EQUITY)
    assert allowed is False and "DB_ERROR" in reason
