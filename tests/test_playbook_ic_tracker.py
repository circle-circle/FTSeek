"""Tests for app.playbook.ic_tracker — 幂等性保障."""
import sqlite3
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from app.playbook.ic_tracker import backfill_forward_returns
from app.playbook.db import PlaybookDB


class TestBackfillForwardReturns:
    """backfill_forward_returns 幂等性测试."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "test_ic_tracker.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        return db

    def _make_market_data(self, close_price: float):
        """构造返回固定日线的 mock market_data."""
        md = MagicMock()

        def _get_daily_bars(symbol, count=30):
            # 生成从 today-count 到 today-1 的日线 (默认覆盖 30 天, 与 ic_tracker 一致)
            bars = []
            for i in range(count, 0, -1):
                d = date(2026, 6, 27) - timedelta(days=i)
                bars.append({
                    "time": d.isoformat(),
                    "close": close_price,
                    "open": close_price * 0.99,
                    "high": close_price * 1.01,
                    "low": close_price * 0.98,
                })
            return bars

        md.get_daily_bars = _get_daily_bars
        return md

    def _insert_instruction(self, db, symbol="US.TEST", direction="BUY",
                             entry_price=100.0, trade_date="2026-06-20"):
        conn = db._conn()
        conn.execute(
            """
            INSERT INTO playbook_instructions
            (date, symbol, direction, entry_price, stop_loss, take_profit, position_size, confidence, outcome, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trade_date, symbol, direction, entry_price, 98.0, 104.0, 10, 70, "holding",
             "2026-06-20T10:00:00", "2026-06-20T10:00:00"),
        )
        conn.commit()
        return db.get_instruction(trade_date, symbol)

    def test_backfill_forward_returns_is_idempotent(self, db):
        """同一交易日多次 backfill 应返回相同结果, 不会重复计数或报错."""
        inst = self._insert_instruction(db, trade_date="2026-06-20")
        market_data = self._make_market_data(close_price=105.0)

        # 第一次回填
        updated1 = backfill_forward_returns(db, market_data, trade_date="2026-06-26")
        assert updated1 == 1

        inst_after = db.get_instruction_by_id(inst["id"])
        assert inst_after["fwd_return_t1"] is not None
        assert inst_after["fwd_return_t3"] is not None
        # T+5 可能跨周末无数据, 不强制非空

        # 第二次回填 (幂等性关键)
        updated2 = backfill_forward_returns(db, market_data, trade_date="2026-06-26")
        assert updated2 == 1  # 仍更新 1 条, 不会重复插入

        inst_second = db.get_instruction_by_id(inst["id"])
        assert inst_second["fwd_return_t1"] == inst_after["fwd_return_t1"]
        assert inst_second["fwd_return_t3"] == inst_after["fwd_return_t3"]
        assert inst_second["fwd_return_t5"] == inst_after["fwd_return_t5"]

    def test_backfill_forward_returns_updates_consistently_on_price_change(self, db):
        """价格变化后重新 backfill, 应更新为新值."""
        inst = self._insert_instruction(db, trade_date="2026-06-20")
        md1 = self._make_market_data(close_price=105.0)
        backfill_forward_returns(db, md1, trade_date="2026-06-26")
        r1 = db.get_instruction_by_id(inst["id"])["fwd_return_t1"]

        # 模拟价格变化后重新 backfill
        md2 = self._make_market_data(close_price=110.0)
        backfill_forward_returns(db, md2, trade_date="2026-06-26")
        r2 = db.get_instruction_by_id(inst["id"])["fwd_return_t1"]

        assert r2 != r1
        # BUY 方向, 110 > 105, 收益应更高
        assert r2 > r1

    def test_backfill_forward_returns_skips_skip_direction(self, db):
        """SKIP 方向的指令不应被回填."""
        self._insert_instruction(db, direction="SKIP", trade_date="2026-06-20")
        market_data = self._make_market_data(close_price=105.0)
        updated = backfill_forward_returns(db, market_data, trade_date="2026-06-26")
        assert updated == 0
