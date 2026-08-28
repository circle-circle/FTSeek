"""单标的日亏损"一把尺"回归测试 (2026-08-28, 全链路复查 P0-1).

背景: "$500/标的/日" 死亡螺旋防线此前只写在主线 order_executor._check_symbol_daily_loss
的内联 SQL, 而主线开仓路径 2026-07-02 起冻结 (playbook 经 dual_broker 直连下单, 不经
order_executor) → 实盘唯一执行链路上没有任何单标的日亏损门。修复 = 口径抽成
app/risk/symbol_day_loss 单一实现, 两条链共用同一阈值同一 SQL。

覆盖点:
  1. 出场日归集 — 隔夜跳空亏损算进出场日 (旧入场日口径查到 0, 8/27 UBER 实锤)
  2. 净额优先 + 旧行 pnl_net NULL 回退毛额
  3. 未了结行 (exit_price 0/NULL) 不进分母
  4. 限额边界: 恰 -500 触发 (<=), -499 放行
  5. 标的隔离 (A 亏穿不拦 B)
  6. 阈值单一来源不变量: 主线 _DAILY_LOSS_LIMIT 与本模块同一对象
  7. et_date 缺省 = 当前 ET 交易日

回滚: 删本文件 + 还原 app/risk/symbol_day_loss.py 与两处调用点即可。
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.risk.symbol_day_loss import (
    DAILY_LOSS_LIMIT_USD,
    is_symbol_day_loss_exceeded,
    symbol_day_net_pnl,
)
from app.utils.timezone import _et_trade_date, now_et


@pytest.fixture
def ruler_db(tmp_path, monkeypatch):
    """独立 trade_records 库, 不碰生产 data/ftseek_us.db."""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL, source_type TEXT)"
    )
    conn.commit()
    conn.close()
    import app.core.db_utils as dbu
    monkeypatch.setattr(dbu, "get_main_db", lambda: db)
    return db


def _insert(db, symbol, entry_date, exit_date, pnl_net=None, realized_pnl=None,
            exit_price=100.0, source_type="PLAYBOOK_SIM"):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl, source_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (symbol, entry_date,
         f"{exit_date}T10:00:00-04:00" if exit_date else None,
         exit_price if exit_date else 0.0, pnl_net, realized_pnl, source_type),
    )
    conn.commit()
    conn.close()


# ── 1. 出场日归集 ──
def test_overnight_loss_attributed_to_exit_day(ruler_db):
    """8/27 UBER 形态: 8/26 入场, 8/27 跳空出场 -307.13 → 8/27 必须看到."""
    _insert(ruler_db, "US.UBER", "2026-08-26", "2026-08-27",
            pnl_net=-307.13, realized_pnl=-305.00)
    assert symbol_day_net_pnl("US.UBER", "2026-08-27", db_path=str(ruler_db)) == pytest.approx(-307.13)
    assert symbol_day_net_pnl("US.UBER", "2026-08-26", db_path=str(ruler_db)) == 0.0


def test_old_entry_date_caliber_would_have_missed_it(ruler_db):
    """对照留痕: 旧口径 WHERE trade_date=? 对同一行查到 0.00 (缺陷复现基线)."""
    _insert(ruler_db, "US.UBER", "2026-08-26", "2026-08-27",
            pnl_net=-307.13, realized_pnl=-305.00)
    conn = sqlite3.connect(str(ruler_db))
    legacy = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) FROM trade_records "
        "WHERE symbol = ? AND trade_date = ? AND exit_price > 0",
        ("US.UBER", "2026-08-27"),
    ).fetchone()[0]
    conn.close()
    assert legacy == 0.0
    assert symbol_day_net_pnl("US.UBER", "2026-08-27", db_path=str(ruler_db)) == pytest.approx(-307.13)


# ── 2. 净额口径 ──
def test_net_preferred_over_gross(ruler_db):
    """同一行毛 -600 / 净 -700: 熔断看净额."""
    _insert(ruler_db, "US.MU", "2026-08-28", "2026-08-28",
            pnl_net=-700.0, realized_pnl=-600.0)
    blocked, day_net = is_symbol_day_loss_exceeded("US.MU", et_date="2026-08-28")
    assert blocked is True
    assert day_net == pytest.approx(-700.0)


def test_legacy_row_without_net_falls_back_to_gross(ruler_db):
    """旧行 pnl_net NULL → 回退 realized_pnl (2026-07-30 前落库的数据)."""
    _insert(ruler_db, "US.MU", "2026-08-28", "2026-08-28",
            pnl_net=None, realized_pnl=-520.0)
    blocked, day_net = is_symbol_day_loss_exceeded("US.MU", et_date="2026-08-28")
    assert day_net == pytest.approx(-520.0)
    assert blocked is True


# ── 3. 只算已了结 ──
def test_unclosed_positions_excluded(ruler_db):
    """持仓中 (exit_price=0 / exit_time NULL) 的浮亏不进当日累计."""
    _insert(ruler_db, "US.MU", "2026-08-28", None, pnl_net=-900.0,
            realized_pnl=-900.0, exit_price=0.0)
    _insert(ruler_db, "US.MU", "2026-08-28", "2026-08-28", pnl_net=-10.0,
            realized_pnl=-10.0, exit_price=101.0)
    assert symbol_day_net_pnl("US.MU", "2026-08-28", db_path=str(ruler_db)) == pytest.approx(-10.0)


# ── 4. 边界算术 ──
def test_exact_limit_blocks_and_one_dollar_less_passes(ruler_db):
    assert DAILY_LOSS_LIMIT_USD == 500.0
    _insert(ruler_db, "US.A", "2026-08-28", "2026-08-28", pnl_net=-500.0,
            realized_pnl=-499.0)
    assert is_symbol_day_loss_exceeded("US.A", et_date="2026-08-28")[0] is True

    _insert(ruler_db, "US.B", "2026-08-28", "2026-08-28", pnl_net=-499.0,
            realized_pnl=-498.0)
    assert is_symbol_day_loss_exceeded("US.B", et_date="2026-08-28")[0] is False


def test_custom_limit_overrides_default(ruler_db):
    _insert(ruler_db, "US.MU", "2026-08-28", "2026-08-28", pnl_net=-150.0,
            realized_pnl=-150.0)
    assert is_symbol_day_loss_exceeded("US.MU", limit_usd=100.0, et_date="2026-08-28")[0] is True
    assert is_symbol_day_loss_exceeded("US.MU", limit_usd=200.0, et_date="2026-08-28")[0] is False


# ── 5. 标的隔离 ──
def test_loss_on_one_symbol_does_not_block_another(ruler_db):
    _insert(ruler_db, "US.MU", "2026-08-28", "2026-08-28", pnl_net=-800.0,
            realized_pnl=-800.0)
    assert is_symbol_day_loss_exceeded("US.AVGO", et_date="2026-08-28") == (False, 0.0)


# ── 6. 阈值单一来源 (两链同尺不变量) ──
def test_main_line_and_ruler_share_one_threshold():
    from app.pipeline import order_executor as oe
    assert oe._DAILY_LOSS_LIMIT is DAILY_LOSS_LIMIT_USD


def test_main_line_check_uses_ruler_caliber(ruler_db, monkeypatch):
    """主线 _check_symbol_daily_loss 与 playbook 门对同一数据集给出同一判据."""
    from app.pipeline import order_executor as oe
    today = _et_trade_date(now_et())
    _insert(ruler_db, "US.UBER", "2026-08-26", today, pnl_net=-307.13, realized_pnl=-305.0)
    _insert(ruler_db, "US.UBER", today, today, pnl_net=-250.0, realized_pnl=-248.0)
    # 清进程级缓存 → 走 DB 自举 (与 playbook 门同一份查询)
    oe._symbol_daily_loss_cache.clear()
    oe._symbol_daily_loss_ts.clear()
    oe._symbol_daily_loss_date = ""

    assert oe._check_symbol_daily_loss("US.UBER") is False
    blocked, day_net = is_symbol_day_loss_exceeded("US.UBER", et_date=today)
    assert blocked is True
    assert day_net == pytest.approx(-557.13)


# ── 7. et_date 缺省 ──
def test_default_et_date_is_today(ruler_db):
    today = _et_trade_date(now_et())
    _insert(ruler_db, "US.MU", today, today, pnl_net=-60.0, realized_pnl=-60.0)
    assert symbol_day_net_pnl("US.MU") == pytest.approx(-60.0)
