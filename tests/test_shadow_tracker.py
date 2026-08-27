"""Tests for app.playbook.shadow_tracker — Shadow PnL + MFE/MAE"""
import pytest
import sqlite3
from datetime import date, datetime
from unittest.mock import MagicMock

from app.playbook.db import PlaybookDB
from app.playbook.shadow_tracker import (
    ShadowTracker,
    is_trading_day,
    analyze_mfe_mae,
    US_MARKET_HOLIDAYS_2026,
)


# ========== Fixtures ==========

@pytest.fixture
def db():
    """内存数据库 PlaybookDB"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def mock_market_data():
    """Mock 市场数据提供者"""
    md = MagicMock()
    return md


@pytest.fixture
def mock_trade_store():
    """Mock TradeStore"""
    return MagicMock()


@pytest.fixture
def tracker(db, mock_market_data, mock_trade_store):
    """ShadowTracker 实例"""
    return ShadowTracker(db, mock_market_data, mock_trade_store)


def _save_instruction(db, **kwargs):
    """Helper: 保存指令并返回 id"""
    defaults = {
        "date": "2026-06-18",
        "symbol": "US.NTES",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": 50,
        # 2026-08-27: 显式 created_at (早于全部 mock entry_time) — 默认 now_utc() 是
        # 真实当前时间, 会被幻影回填守卫 (成交早于创建→丢弃) 把合成数据误判为幻影。
        "created_at": "2026-06-18T08:00:00",
    }
    defaults.update(kwargs)
    return db.save_instruction(defaults)


# ========== is_trading_day 测试 ==========

class TestIsTradingDay:
    """交易日判断测试"""

    def test_weekday_is_trading_day(self):
        """周三正常交易日"""
        assert is_trading_day(date(2026, 6, 17)) is True  # 周三

    def test_saturday_not_trading(self):
        """周六不交易"""
        assert is_trading_day(date(2026, 6, 20)) is False  # 周六

    def test_sunday_not_trading(self):
        """周日不交易"""
        assert is_trading_day(date(2026, 6, 21)) is False  # 周日

    def test_holiday_not_trading(self):
        """假日不交易"""
        # Juneteenth 2026-06-19 是周五
        assert is_trading_day(date(2026, 6, 19)) is False

    def test_independence_day(self):
        """Independence Day observed"""
        assert is_trading_day(date(2026, 7, 3)) is False

    def test_christmas(self):
        """Christmas"""
        assert is_trading_day(date(2026, 12, 25)) is False

    def test_regular_friday(self):
        """普通周五是交易日"""
        assert is_trading_day(date(2026, 6, 26)) is True  # 普通周五

    def test_holiday_set_complete(self):
        """确保假日列表包含主要假日"""
        assert "2026-01-01" in US_MARKET_HOLIDAYS_2026
        assert "2026-11-26" in US_MARKET_HOLIDAYS_2026
        assert "2026-12-25" in US_MARKET_HOLIDAYS_2026


# ========== Shadow PnL 计算测试 ==========

class TestShadowPnL:
    """Shadow PnL 计算测试"""

    def test_skip_instruction_no_shadow_pnl(self, tracker, db, mock_market_data, mock_trade_store):
        """SKIP 指令不再计算 shadow_pnl (2026-06-19 Phase 0: 设计变更)"""
        inst_id = _save_instruction(db, direction="SKIP", skip_reason="财报3天内")

        # Mock: 收盘价 105
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 102, "high": 106, "low": 101, "close": 105}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["outcome"] == "skipped"
        # SKIP 不再计算 shadow_pnl
        assert result.get("shadow_pnl") is None

    def test_skip_short_direction(self, tracker, db, mock_market_data, mock_trade_store):
        """SELL_SHORT 方向使用实际 position_size 计算 shadow_pnl"""
        inst_id = _save_instruction(db, direction="SELL_SHORT", skip_reason="风险")

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 102, "high": 106, "low": 101, "close": 105}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        # SHORT shadow_pnl = (100 - 105) * 50 (实际 position_size) = -250
        assert result["shadow_pnl"] == -250.0

    def test_non_holiday_tracking_runs(self, tracker, db, mock_market_data, mock_trade_store):
        """非假日正常执行追踪"""
        _save_instruction(db, direction="BUY")
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 102, "high": 106, "low": 101, "close": 105}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        # 应该被更新 (不是 None)
        assert result["outcome"] is not None or result["triggered"] is not None

    def test_holiday_skips_tracking(self, tracker, db, mock_market_data):
        """假日跳过追踪"""
        _save_instruction(db, date="2026-06-19")  # Juneteenth

        tracker.daily_tracking("2026-06-19")

        result = db.get_instruction("2026-06-19", "US.NTES")
        # 不应被更新
        assert result["outcome"] is None
        mock_market_data.get_daily_bars.assert_not_called()


# ========== 触发检测测试 ==========

class TestTriggerDetection:
    """入场触发检测"""

    def test_triggered_when_price_in_range(self, tracker, db, mock_market_data, mock_trade_store):
        """价格进入入场区间 → triggered=1"""
        _save_instruction(db, entry_price=100.0)

        # 触发: 当日穿过 MA20 且放量
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 98, "high": 103, "low": 97, "close": 101, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["triggered"] == 1

    def test_not_triggered_when_price_outside_range(self, tracker, db, mock_market_data, mock_trade_store):
        """价格不在入场区间 → triggered=0"""
        _save_instruction(db, entry_price=100.0)

        # 未触发: 收盘未站上 MA20
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 105, "high": 110, "low": 105, "close": 108, "ma20": 110, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["triggered"] == 0

    def test_triggered_but_not_executed_is_missed(self, tracker, db, mock_market_data, mock_trade_store):
        """触发了但没执行 → outcome=missed"""
        _save_instruction(db, entry_price=100.0)

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 99, "high": 102, "low": 98, "close": 105, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = []  # 无匹配交易

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["triggered"] == 1
        assert result["outcome"] == "missed"


# ========== 盘中已触发 + 未成交的归因测试 (2026-08-26, RFC-066 漏斗审计) ==========

class TestIntradayTriggeredMissedAttribution:
    """auto_execute 盘中已写 triggered=1, EOD 影子追踪必须归因为'已触发未执行',
    不得落进'未触发'分支 — 8/24 实锤: US.AU/AVGO/NTES 三笔再入场被单日notional帽
    拦截, 日志有'方向穿越触发'实据, 但 EOD 误标 no_trigger 污染漏斗."""

    def test_intraday_triggered_no_exec_gets_trigger_no_execution_label(
            self, tracker, db, mock_market_data, mock_trade_store):
        """核心契约: 盘中 triggered=1 + 无成交 → skip_reason 标'已触发未执行'."""
        inst_id = _save_instruction(db, entry_price=100.0, position_size=50)
        # auto_execute 盘中已确认触发 (未成交)
        db.update_instruction_tracking(inst_id, {"triggered": 1})

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 105, "high": 110, "low": 105,
             "close": 108, "ma20": 110, "volume_ratio": 2.0}  # 影子自身判定不会触发
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["triggered"] == 1               # 盘中状态不被覆盖
        assert result["outcome"] == "missed"
        assert "trigger_no_execution" in (result["skip_reason"] or "")
        assert "no_trigger" not in (result["skip_reason"] or "")

    def test_never_triggered_still_gets_no_trigger_label(
            self, tracker, db, mock_market_data, mock_trade_store):
        """回归: 从未触发 → 仍标'未触发' (防过度翻转)."""
        _save_instruction(db, entry_price=100.0)

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 105, "high": 110, "low": 105,
             "close": 108, "ma20": 110, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["outcome"] == "missed"
        assert "no_trigger" in (result["skip_reason"] or "")

    def test_shadow_backfill_trigger_path_unchanged(
            self, tracker, db, mock_market_data, mock_trade_store):
        """回归: 影子自身判定触发 (无盘中先验) → 仍走'已触发未执行'分支."""
        _save_instruction(db, entry_price=100.0)

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 99, "high": 102, "low": 98,
             "close": 105, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["triggered"] == 1
        assert result["outcome"] == "missed"
        assert "trigger_no_execution" in (result["skip_reason"] or "")

    def test_existing_skip_reason_not_overwritten(
            self, tracker, db, mock_market_data, mock_trade_store):
        """回归: auto_execute 已写入具体拒因时不被补标覆盖."""
        inst_id = _save_instruction(db, entry_price=100.0, position_size=50)
        db.update_instruction_tracking(inst_id, {
            "triggered": 1,
            "skip_reason": "跳过 — 单日notional上限 (具体拒因)",
        })

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert "notional上限" in (result["skip_reason"] or "")
        assert "trigger_no_execution" not in (result["skip_reason"] or "")


# ========== 执行匹配测试 ==========

class TestExecutionMatching:
    """交易执行匹配"""

    def test_executed_trade_matched(self, tracker, db, mock_market_data, mock_trade_store):
        """匹配到执行记录"""
        _save_instruction(db, entry_price=100.0, position_size=50)

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99, "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {
                "symbol": "US.NTES",
                "direction": "LONG",  # trade_records 存储格式
                "source_type": "MANUAL_PLAYBOOK",
                "entry_price": 100.5,
                "entry_time": "2026-06-18T09:35:00",
            }
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 1
        assert result["execution_price"] == 100.5

    def test_holding_position_pnl(self, tracker, db, mock_market_data, mock_trade_store):
        """持仓中: 用收盘价计算浮盈"""
        inst_id = _save_instruction(db, entry_price=100.0, position_size=50)
        # 先标记为已执行
        db.update_instruction_tracking(inst_id, {
            "executed": 1,
            "execution_price": 100.0,
            "execution_time": "2026-06-18T09:35:00",
        })

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99, "close": 106}
        ]
        mock_trade_store.query_by_date.return_value = []  # 无平仓记录

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["outcome"] == "holding"
        # pnl = (106 - 100) * 50 = 300
        assert result["pnl"] == 300.0

    def test_exit_trade_detected(self, tracker, db, mock_market_data, mock_trade_store):
        """检测到平仓记录"""
        inst_id = _save_instruction(db, entry_price=100.0, position_size=50)
        db.update_instruction_tracking(inst_id, {
            "executed": 1,
            "execution_price": 100.0,
        })

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 110, "low": 99, "close": 108}
        ]
        mock_trade_store.query_by_date.return_value = [
            {
                "symbol": "US.NTES",
                "action": "CLOSE",
                "source_type": "MANUAL_PLAYBOOK",
                "exit_price": 108.0,
                "exit_time": "2026-06-18T14:30:00",
                "exit_reason": "take_profit",
            }
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["outcome"] == "win"
        assert result["exit_price"] == 108.0
        # pnl = (108 - 100) * 50 = 400
        assert result["pnl"] == 400.0

    def test_rearmed_row_does_not_claim_previous_round_trade(
            self, tracker, db, mock_market_data, mock_trade_store):
        """P2b 回归 (2026-08-24): 重武装后未成交的新轮指令不得认领上一轮成交.

        根因: intraday_reentry 重武装 UPSERT 同行并复位 outcome/executed 后,
        EOD 影子链用 symbol+direction+date 启发式把第一轮 trade_record 认领回来
        (盖章 executed=1 + 第一轮盈亏) — 复位当晚即被重新写脏 (夜间复燃).
        实锤: 指令#991 US.TSM execution_time(10:02) 早于武装时刻(12:36) 却带 loss.
        """
        _save_instruction(db, entry_price=100.0, position_size=50)
        inst = db.get_instruction("2026-06-18", "US.NTES")
        conn = db._conn()
        conn.execute("UPDATE playbook_instructions SET decision_id=? WHERE id=?",
                     ("intraday_reentry-999-1230", inst["id"]))
        conn.commit()

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {   # 上一轮成交: 挂旧轮 decision_id, 已平仓盈利
                "symbol": "US.NTES", "direction": "LONG",
                "source_type": "PLAYBOOK_SIM",
                "decision_id": "intraday_reentry-888-1046",
                "entry_price": 100.5, "entry_time": "2026-06-18T10:49:00",
                "exit_price": 103.0, "exit_time": "2026-06-18T11:30:00",
                "exit_reason": "SL_BREAKEVEN",
            }
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 0           # 不再被盖章已执行
        assert result["outcome"] != "win"        # 不再认领上一轮盈亏

    def test_same_round_trade_linked_by_decision_id(
            self, tracker, db, mock_market_data, mock_trade_store):
        """P2b 正向联接: 成交记录挂与本指令一致的 decision_id 时正常认领."""
        _save_instruction(db, entry_price=100.0, position_size=50)
        inst = db.get_instruction("2026-06-18", "US.NTES")
        conn = db._conn()
        conn.execute("UPDATE playbook_instructions SET decision_id=? WHERE id=?",
                     ("intraday_reentry-999-1230", inst["id"]))
        conn.commit()

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {"symbol": "US.NTES", "direction": "LONG",
             "source_type": "PLAYBOOK_SIM",
             "decision_id": "intraday_reentry-999-1230",   # 本轮
             "entry_price": 100.5, "entry_time": "2026-06-18T13:30:00"},
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 1
        assert result["execution_price"] == 100.5

    def test_unattributed_trade_still_matched_as_fallback(
            self, tracker, db, mock_market_data, mock_trade_store):
        """P2b 兼容: 无归属 (decision_id 空) 的历史成交仍按原启发式认领."""
        _save_instruction(db, entry_price=100.0, position_size=50)
        inst = db.get_instruction("2026-06-18", "US.NTES")
        conn = db._conn()
        conn.execute("UPDATE playbook_instructions SET decision_id=? WHERE id=?",
                     ("DA-20260618-US.NTES-a1", inst["id"]))
        conn.commit()

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {"symbol": "US.NTES", "direction": "LONG",
             "source_type": "MANUAL_PLAYBOOK",
             # 无 decision_id 键 (旧数据形态)
             "entry_price": 100.5, "entry_time": "2026-06-18T09:35:00"},
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 1
        assert result["execution_price"] == 100.5


# ========== 幻影回填时刻守卫 (2026-08-27, 8/26 #1777 认领 #1370 成交实锤) ==========

class TestPhantomTemporalGuard:
    """成交 entry_time 早于指令 created_at (>30s) → 物理上不可能属于本指令, 丢弃匹配.

    纵深防御B: 即使 P2b decision_id 联接键再被削弱 (如列缺失/空值), 时刻不变量
    也能拦住跨轮误联。30s 容差覆盖 auto_execute 先 claim 后写行的时钟偏斜。
    """

    def test_fill_before_creation_is_dropped(self, tracker, db, mock_market_data, mock_trade_store):
        """生产形态复现: created_at(UTC) 晚于 entry_time(ET-aware) 数小时 → 丢弃."""
        _save_instruction(db, entry_price=100.0, position_size=50,
                          created_at="2026-06-18T19:00:00+00:00")  # 15:00 ET

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {"symbol": "US.NTES", "direction": "LONG",
             "source_type": "PLAYBOOK_SIM",
             # 无 decision_id (联接键最弱形态) + 成交在指令创建前 ~9h → 必须丢弃
             "entry_price": 100.5, "entry_time": "2026-06-18T09:43:03-04:00"},
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] != 1
        assert not result["execution_price"]

    def test_fill_after_creation_kept(self, tracker, db, mock_market_data, mock_trade_store):
        """正向: 成交在指令创建之后 (正常时序) → 正常认领 executed=1."""
        _save_instruction(db, entry_price=100.0, position_size=50,
                          created_at="2026-06-18T13:00:00+00:00")  # 09:00 ET

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {"symbol": "US.NTES", "direction": "LONG",
             "source_type": "PLAYBOOK_SIM",
             "entry_price": 100.5, "entry_time": "2026-06-18T09:35:00-04:00"},  # 13:35Z
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 1
        assert result["execution_price"] == 100.5

    def test_small_clock_skew_within_tolerance_kept(
            self, tracker, db, mock_market_data, mock_trade_store):
        """容差: 成交仅早于创建 20s (claim→写行偏斜) → 不触发守卫."""
        _save_instruction(db, entry_price=100.0, position_size=50,
                          created_at="2026-06-18T09:35:20")  # naive → 按 ET 解释

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 99,
             "close": 106, "ma20": 100, "volume_ratio": 2.0}
        ]
        mock_trade_store.query_by_date.return_value = [
            {"symbol": "US.NTES", "direction": "LONG",
             "source_type": "PLAYBOOK_SIM",
             "entry_price": 100.5, "entry_time": "2026-06-18T09:35:00"},  # 早 20s
        ]

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 1


# ========== MFE/MAE 计算测试 ==========

class TestMfeMae:
    """MFE/MAE 计算"""

    def test_mfe_mae_buy_direction(self, tracker, db, mock_market_data, mock_trade_store):
        """BUY 方向: MFE 看 high, MAE 看 low"""
        inst_id = _save_instruction(db, entry_price=100.0, position_size=50)
        db.update_instruction_tracking(inst_id, {
            "executed": 1,
            "execution_price": 100.0,
            "execution_time": "2026-06-18",
        })

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 110, "low": 95, "close": 105}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        # MFE = (110 - 100) * 50 = 500
        assert result["max_favorable_excursion"] == 500.0
        # MAE = (95 - 100) * 50 = -250
        assert result["max_adverse_excursion"] == -250.0

    def test_mfe_mae_short_direction(self, tracker, db, mock_market_data, mock_trade_store):
        """SHORT 方向: MFE 看 low, MAE 看 high"""
        inst_id = _save_instruction(db, direction="SELL_SHORT", entry_price=100.0, position_size=50)
        db.update_instruction_tracking(inst_id, {
            "executed": 1,
            "execution_price": 100.0,
            "execution_time": "2026-06-18",
        })

        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 108, "low": 92, "close": 95}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        # SHORT MFE = (100 - 92) * 50 = 400
        assert result["max_favorable_excursion"] == 400.0
        # SHORT MAE = (100 - 108) * 50 = -400
        assert result["max_adverse_excursion"] == -400.0

    def test_mfe_mae_accumulates_over_days(self, tracker, db, mock_market_data, mock_trade_store):
        """MFE/MAE 跨天累积"""
        inst_id = _save_instruction(db, entry_price=100.0, position_size=50)
        db.update_instruction_tracking(inst_id, {
            "executed": 1,
            "execution_price": 100.0,
            "execution_time": "2026-06-17",
            "max_favorable_excursion": 300.0,  # 之前已有 MFE
            "max_adverse_excursion": -100.0,
        })

        # 今天 high=112 → MFE 应该更新为 (112-100)*50=600
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-17", "open": 100, "high": 106, "low": 98, "close": 104},
            {"time": "2026-06-18", "open": 104, "high": 112, "low": 103, "close": 110},
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")

        result = db.get_instruction("2026-06-18", "US.NTES")
        # 新的 MFE = max(300, (112-100)*50) = 600
        assert result["max_favorable_excursion"] == 600.0


# ========== analyze_mfe_mae 测试 ==========

class TestAnalyzeMfeMAE:
    """MFE/MAE 分析文本"""

    def test_take_profit_too_low(self):
        """MFE 远大于 TP → 止盈设低了"""
        inst = {
            "entry_price": 100,
            "take_profit": 105,  # TP 收益 = 5*50 = 250
            "stop_loss": 95,
            "position_size": 50,
            "pnl": 200,
        }
        updates = {
            "max_favorable_excursion": 500,  # MFE = 500 > 250*1.5=375
            "max_adverse_excursion": -100,
        }

        result = analyze_mfe_mae(inst, updates)
        assert "止盈设低了" in result

    def test_stop_loss_lucky(self):
        """MAE 超过 SL 但最终盈利 → 运气好"""
        inst = {
            "entry_price": 100,
            "take_profit": 110,
            "stop_loss": 95,  # SL 损失 = 5*50 = 250
            "position_size": 50,
            "pnl": 200,  # 最终盈利
        }
        updates = {
            "max_favorable_excursion": 300,
            "max_adverse_excursion": -300,  # |MAE|=300 > SL=250
        }

        result = analyze_mfe_mae(inst, updates)
        assert "运气好" in result

    def test_reasonable_levels(self):
        """止损止盈设置合理"""
        inst = {
            "entry_price": 100,
            "take_profit": 110,
            "stop_loss": 95,
            "position_size": 50,
            "pnl": 200,
        }
        updates = {
            "max_favorable_excursion": 200,  # MFE < TP*1.5
            "max_adverse_excursion": -100,  # |MAE| < SL
        }

        result = analyze_mfe_mae(inst, updates)
        assert "合理" in result


# ========== 边界情况测试 ==========

class TestEdgeCases:
    """边界情况"""

    def test_no_market_data(self, db, mock_trade_store):
        """market_data=None 时不崩溃"""
        _save_instruction(db)
        mock_trade_store.query_by_date.return_value = []

        tracker = ShadowTracker(db, market_data=None, trade_store=mock_trade_store)
        tracker.daily_tracking("2026-06-18")
        # 不应抛异常

    def test_no_trade_store(self, db, mock_market_data):
        """trade_store=None 时不崩溃"""
        _save_instruction(db)
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 105, "low": 99, "close": 103}
        ]

        tracker = ShadowTracker(db, market_data=mock_market_data, trade_store=None)
        tracker.daily_tracking("2026-06-18")
        # 不应抛异常

    def test_empty_instructions(self, tracker, db):
        """无指令时不崩溃"""
        tracker.daily_tracking("2026-06-18")
        # 不应抛异常

    def test_zero_entry_price(self, tracker, db, mock_market_data, mock_trade_store):
        """入场价为 0"""
        _save_instruction(db, entry_price=0)
        mock_market_data.get_daily_bars.return_value = [
            {"time": "2026-06-18", "open": 100, "high": 105, "low": 99, "close": 103}
        ]
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")
        # 不应抛异常

    def test_market_data_exception(self, tracker, db, mock_market_data, mock_trade_store):
        """市场数据获取异常时不崩溃"""
        _save_instruction(db)
        mock_market_data.get_daily_bars.side_effect = Exception("API error")
        mock_market_data.get_quote.side_effect = Exception("API error")
        mock_trade_store.query_by_date.return_value = []

        tracker.daily_tracking("2026-06-18")
        # 不应抛异常
