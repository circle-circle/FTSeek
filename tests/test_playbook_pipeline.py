"""Tests for app.playbook.pipeline — 关键函数回归测试."""
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.playbook.pipeline import _close_trade_record, _check_intraday_trigger


class TestCloseTradeRecord:
    """_close_trade_record 写入完整性和幂等性测试."""

    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                realized_pnl REAL,
                pnl_pct REAL,
                holding_hours REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE playbook_instructions (
                id INTEGER PRIMARY KEY,
                outcome TEXT,
                exit_price REAL,
                exit_reason TEXT,
                pnl REAL,
                pnl_pct REAL,
                holding_period_hours REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO trade_records (id) VALUES (1)"
        )
        conn.execute(
            "INSERT INTO playbook_instructions (id, outcome) VALUES (1, 'holding')"
        )
        conn.commit()
        return conn

    def test_close_trade_record_writes_exit_time_and_reason(self, conn):
        """验证 exit_time / exit_reason / pnl 被正确写入 trade_records."""
        _close_trade_record(
            conn,
            trade_id=1,
            exit_price=150.0,
            exit_reason="SL_HIT",
            pnl=-120.5,
            pnl_pct=-0.015,
            holding_hours=2.5,
        )

        row = conn.execute("SELECT * FROM trade_records WHERE id=1").fetchone()
        assert row["exit_price"] == 150.0
        assert row["exit_reason"] == "SL_HIT"
        assert row["realized_pnl"] == -120.5
        assert row["pnl_pct"] == -0.015
        assert row["holding_hours"] == 2.5
        # exit_time 应被写入且为合法 ISO 格式
        assert row["exit_time"]
        dt = datetime.fromisoformat(row["exit_time"])
        assert dt.year == datetime.utcnow().year

    def test_close_trade_record_updates_playbook_instruction(self, conn):
        """验证关联的 playbook_instructions 行被同步更新."""
        _close_trade_record(
            conn,
            trade_id=1,
            exit_price=160.0,
            exit_reason="TP_HIT",
            pnl=200.0,
            pnl_pct=0.025,
            holding_hours=1.5,
            inst_id=1,
        )

        row = conn.execute("SELECT * FROM playbook_instructions WHERE id=1").fetchone()
        assert row["outcome"] == "win"
        assert row["exit_price"] == 160.0
        assert row["exit_reason"] == "TP_HIT"
        assert row["pnl"] == 200.0
        assert row["pnl_pct"] == 0.025
        assert row["holding_period_hours"] == 1.5

    def test_close_trade_record_does_not_overwrite_win_loss(self, conn):
        """已处于 win/loss 的 instruction 不应被覆盖."""
        conn.execute("UPDATE playbook_instructions SET outcome='win' WHERE id=1")
        conn.commit()
        _close_trade_record(
            conn,
            trade_id=1,
            exit_price=150.0,
            exit_reason="SL_HIT",
            pnl=-50.0,
            pnl_pct=-0.01,
            holding_hours=1.0,
            inst_id=1,
        )
        row = conn.execute("SELECT outcome FROM playbook_instructions WHERE id=1").fetchone()
        assert row["outcome"] == "win"


class TestCheckIntradayTrigger:
    """_check_intraday_trigger 触发条件边界测试."""

    def _make_scheduler(self, price, ma20, volume_ratio):
        """构造一个返回固定 SymbolContext 的 mock scheduler."""
        scheduler = MagicMock()
        scheduler.data_router.get_full_market_context.return_value = {
            "price": price,
            "ma20": ma20,
            "volume_5d_avg_ratio": volume_ratio,
        }
        return scheduler

    def test_long_breakout_and_volume_trigger(self):
        scheduler = self._make_scheduler(price=101.0, ma20=100.0, volume_ratio=2.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_long_breakout_without_volume_no_trigger(self):
        scheduler = self._make_scheduler(price=101.0, ma20=100.0, volume_ratio=1.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_long_volume_without_breakout_no_trigger(self):
        scheduler = self._make_scheduler(price=100.2, ma20=100.0, volume_ratio=2.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_short_breakout_and_volume_trigger(self):
        scheduler = self._make_scheduler(price=99.0, ma20=100.0, volume_ratio=2.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_short_breakout_without_volume_no_trigger(self):
        scheduler = self._make_scheduler(price=99.0, ma20=100.0, volume_ratio=1.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_missing_data_is_fail_closed(self):
        scheduler = MagicMock()
        scheduler.data_router.get_full_market_context.return_value = {}
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_entry_stop_immediately_intercepts(self):
        """触发后若价格已越过止损, 应返回 False 避免入场即止损."""
        scheduler = self._make_scheduler(price=101.0, ma20=100.0, volume_ratio=2.0)
        inst = {
            "symbol": "US.TEST",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 102.0,  # 当前价格 101 已低于 stop_loss, LONG 应被拦截
        }
        assert _check_intraday_trigger(scheduler, inst) is False
