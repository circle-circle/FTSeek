"""
owner_strategy 持久化端到端测试 (B.7 2026-05-14)

验证 schema → INSERT → 读回 完整链路, 防止 Phase A 出现"列加了但没人写"。

覆盖:
1. trade_records.save_trade 写 owner_strategy
2. open_positions.upsert_open_position 写 owner_strategy
3. trade_attribution: 通过 _ensure_trade_attribution_schema 测列存在 (实际 INSERT 在 order_executor)
"""
import os
import sqlite3
import tempfile
from datetime import datetime

import pytest

from app.storage.trade_store import TradeStore


@pytest.fixture
def db_path():
    p = tempfile.mktemp(suffix=".db")
    yield p
    if os.path.exists(p):
        os.unlink(p)


def test_save_trade_persists_owner_strategy(db_path):
    """trade_records.owner_strategy 实际写入 DB"""
    store = TradeStore(db_path)
    rid = store.save_trade({
        "trade_date": "2026-05-14",
        "symbol": "NVDA",
        "direction": "LONG",
        "entry_time": "2026-05-14T10:00:00",
        "exit_time": "2026-05-14T11:00:00",
        "entry_price": 100.0,
        "exit_price": 102.0,
        "quantity": 100,
        "realized_pnl": 200.0,
        "owner_strategy": "scalping",  # ← 关键
    })
    assert rid > 0
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT owner_strategy FROM trade_records WHERE id = ?", (rid,)
    ).fetchone()
    assert row is not None
    assert row[0] == "scalping"


def test_save_trade_default_owner_when_missing(db_path):
    """未传 owner_strategy → 默认 'main'"""
    store = TradeStore(db_path)
    rid = store.save_trade({
        "trade_date": "2026-05-14",
        "symbol": "TSLA",
        "direction": "LONG",
        "entry_time": "2026-05-14T10:00:00",
        "exit_time": "2026-05-14T11:00:00",
        "entry_price": 200.0,
        "exit_price": 195.0,
        "quantity": 50,
    })
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT owner_strategy FROM trade_records WHERE id = ?", (rid,)
    ).fetchone()
    assert row[0] == "main"


def test_upsert_open_position_persists_owner_strategy(db_path):
    """open_positions.owner_strategy 实际写入 DB"""
    from app.core.models import Position, Direction, MarketRegime

    store = TradeStore(db_path)
    pos = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        size=10.0,
        avg_entry_price=150.0,
        initial_sl=145.0,
        current_sl=145.0,
        initial_tp=160.0,
        market_regime=MarketRegime.RANGE,
    )
    pos.owner_strategy = "fast_lane"
    pos.position_pct = 0.05
    store.upsert_open_position(pos)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT symbol, owner_strategy FROM open_positions WHERE symbol = ?", ("AAPL",)
    ).fetchone()
    assert row is not None
    assert row[1] == "fast_lane"


def test_upsert_open_position_default_main_when_owner_missing(db_path):
    """Position 无 owner_strategy → 默认 'main'"""
    from app.core.models import Position, Direction, MarketRegime

    store = TradeStore(db_path)
    pos = Position(
        symbol="GOOG",
        direction=Direction.LONG,
        size=5.0,
        avg_entry_price=2500.0,
        initial_sl=2400.0,
        current_sl=2400.0,
        market_regime=MarketRegime.RANGE,
    )
    # 不设 owner_strategy
    store.upsert_open_position(pos)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT owner_strategy FROM open_positions WHERE symbol = ?", ("GOOG",)
    ).fetchone()
    assert row[0] == "main"


def test_trade_attribution_schema_has_owner_strategy(db_path):
    """trade_attribution 完整 schema 含 owner_strategy 列"""
    store = TradeStore(db_path)
    conn = sqlite3.connect(db_path)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(trade_attribution)").fetchall()]
    assert "owner_strategy" in cols
    # 验证可写入 + 读回
    conn.execute(
        "INSERT INTO trade_attribution (trace_id, symbol, direction, owner_strategy) "
        "VALUES (?, ?, ?, ?)",
        ("trace-1", "NVDA", "LONG", "scalping"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT owner_strategy FROM trade_attribution WHERE trace_id = 'trace-1'"
    ).fetchone()
    assert row[0] == "scalping"


def test_legacy_db_alter_adds_owner_strategy(db_path):
    """模拟旧 DB (无 owner_strategy 列) → ALTER 升级补列, 老数据保留"""
    # 先建一个无 owner_strategy 列的 trade_records 表
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, symbol TEXT, direction TEXT,
            created_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trade_records (trade_date, symbol, direction, created_at) "
        "VALUES (?, ?, ?, ?)", ("2026-01-01", "NVDA", "LONG", "2026-01-01T00:00:00")
    )
    conn.commit()
    conn.close()

    # TradeStore init 应当 ALTER 补列, 不破坏老数据
    TradeStore(db_path)
    conn = sqlite3.connect(db_path)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(trade_records)").fetchall()]
    assert "owner_strategy" in cols
    # 老数据应保留, owner_strategy=NULL
    row = conn.execute(
        "SELECT symbol, owner_strategy FROM trade_records WHERE symbol = 'NVDA'"
    ).fetchone()
    assert row[0] == "NVDA"
    assert row[1] is None
