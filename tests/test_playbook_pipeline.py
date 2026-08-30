"""Tests for app.playbook.pipeline — 关键函数回归测试."""
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.playbook.pipeline import (
    BridgeVotingAdapter,
    _close_trade_record,
    _check_intraday_trigger,
)


class TestBridgeVotingAdapterConfidence:
    """DEB-20260830 E11/E 测试钉子 — 消费端禁止重算生成端权威 confidence.

    病根回归钉: 旧代码用 `chall_dir == direction` 字面比较 (BUY/SELL_SHORT vs
    LONG/SHORT 恒不等) 把全候选误判分歧恒扣至 0.35, 与生成端权威值及落库真值矛盾。
    任何把"辩论标签比较"加回 vote() 的改动必须先打碎这些钉子。

    区分度分级 (第8轮变异测试实证): 真能捕获病根回填的是 1/3/5/7 号
    (一致读0.7 / 衰减0.64透传 / 0.0钳制 / bool排除); 2 号 (真分歧0.35) 与
    4 号 (缺字段0.5) 在病根下照过 (旧病输出恰等于断言值), 它们钉的是
    "透传语义不回退", 非病根回归钉, 分级留痕防误读。
    """

    def _adapter(self, cand):
        return BridgeVotingAdapter({cand["symbol"]: cand}, "2026-08-28")

    def _base(self, **over):
        cand = {
            "symbol": "US.TEST", "direction": "LONG", "score": 7.0,
            "confidence": 0.7, "decision_id": "DA-test",
        }
        cand.update(over)
        return cand

    def test_agreement_reads_generator_value_not_literal_recompute(self):
        """挑战者 BUY + agreed + 生成端 0.7 → 必须 0.7 (旧病: 字面比较扣成 0.35)."""
        cand = self._base(debate={
            "challenger_direction": "BUY", "challenger_agreed": True},
            debate_outcome="AGREE")
        v = self._adapter(cand).vote("US.TEST", None)
        assert v["final_confidence"] == 0.7

    def test_true_disagreement_passes_generator_value_through(self):
        """真分歧候选: 生成端已判 0.35, 消费端原样透传不再二次扣分."""
        cand = self._base(confidence=0.35, debate={
            "challenger_direction": "SELL_SHORT", "challenger_agreed": False})
        v = self._adapter(cand).vote("US.TEST", None)
        assert v["final_confidence"] == 0.35

    def test_score_decay_value_passthrough(self):
        """score 差值衰减值 (生成端 0.64) 原样透传 — 保留真方差."""
        cand = self._base(confidence=0.64)
        v = self._adapter(cand).vote("US.TEST", None)
        assert v["final_confidence"] == 0.64

    def test_missing_confidence_falls_to_neutral(self):
        """无 confidence 字段 → 中性 0.5 (非崩溃)."""
        cand = self._base()
        cand.pop("confidence")
        v = self._adapter(cand).vote("US.TEST", None)
        assert v["final_confidence"] == 0.5

    def test_zero_confidence_clamped_to_floor(self):
        """第6轮F钉: 合法 0.0 原样透传后被下限钳制改写成 0.10 (命名按第8轮纠正: 非'不改写')."""
        cand = self._base(confidence=0.0)
        v = self._adapter(cand).vote("US.TEST", None)
        assert v["final_confidence"] == 0.10  # 下限钳制, 不是中性 0.5

    def test_bool_confidence_not_treated_as_number(self):
        """第8轮F钉: isinstance(True, (int,float)) 为真, JSON true 不得当成 1.0 → 中性兜底."""
        cand = self._base(confidence=True)
        v = self._adapter(cand).vote("US.TEST", None)
        assert v["final_confidence"] == 0.5  # 不是 0.95

    def test_confidence_stays_01_scale_for_downstream_x100(self):
        """0-1→0-100 换算钉: 输出恒在 [0.10, 0.95] (下游 :1596 乘 100 落库)."""
        for conf in (0.25, 0.35, 0.5, 0.64, 0.7, 0.99, 1.5):
            v = self._adapter(self._base(confidence=conf)).vote("US.TEST", None)
            assert 0.10 <= v["final_confidence"] <= 0.95, conf


class TestCloseTradeRecord:
    """_close_trade_record 写入完整性和幂等性测试."""

    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # 2026-07 schema: _close_trade_record 现在读写 metadata_json/entry_time/quantity/
        # source_type 快照 + mfe_pct/mae_pct/max_drawdown 落库 (2026-07-09 MFE/MAE 采集)
        conn.execute(
            """
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY,
                symbol TEXT,
                direction TEXT,
                entry_price REAL,
                entry_time TEXT,
                quantity INTEGER,
                source_type TEXT,
                metadata_json TEXT,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                realized_pnl REAL,
                pnl_pct REAL,
                holding_hours REAL,
                close_futu_order_id TEXT,
                mfe_pct REAL,
                mae_pct REAL,
                max_drawdown REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE playbook_instructions (
                id INTEGER PRIMARY KEY,
                outcome TEXT,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                pnl REAL,
                pnl_pct REAL,
                holding_period_hours REAL,
                max_favorable_excursion REAL,
                max_adverse_excursion REAL
            )
            """
        )
        # _close_trade_record 平仓时物理删除 open_positions 行 (2026-07-01 幽灵仓根因修复)
        conn.execute(
            """
            CREATE TABLE open_positions (
                symbol TEXT PRIMARY KEY,
                sync_status TEXT
            )
            """
        )
        conn.execute(
            # entry_time 留空 → _close_trade_record 保留调用方传入的 holding_hours
            # (2026-07-07 起 entry_time 存在时会用 get_market_hours_us 重算, 测试不密封)
            "INSERT INTO trade_records (id, symbol, direction, entry_price, quantity, source_type) "
            "VALUES (1, 'US.TEST', 'LONG', 100.0, 10, 'PLAYBOOK_SIM')"
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
    """_check_intraday_trigger 触发条件边界测试 (2026-06-29 后改为方向穿越触发).

    2026-07 密封: 生产新增 14:00 ET 触发截止/财报门/低ATR门/量能门等 wall-clock
    与外部数据依赖的门 — 测试注入固定 pb_cfg 关闭这些门, 只验证触发带核心逻辑.
    """

    SEALED_CFG = {
        "entry_time_cutoff_enabled": False,      # 密封 wall-clock (14:00 ET 截止)
        "earnings_blackout_gate_enabled": False,
        "low_atr_gate_enabled": False,
        "symbol_streak_ban_enabled": False,
        "intraday_vol_gate_enabled": False,      # 量能门密封 (fail-open)
        "entry_vwap_gate_enabled": False,
        "entry_reanchor_enabled": True,          # 与 unified_config.yaml 对齐 (Lever A)
        "entry_reanchor_max_drift_pct": 4.0,
        "thesis_drift_invalidate_enabled": True,
        "falsification_gates_enabled": False,     # P1-1 证伪门密封 (依赖 1min bar)
    }

    @pytest.fixture(autouse=True)
    def _sealed_pb_cfg(self, monkeypatch):
        monkeypatch.setattr(
            "app.playbook.config.get_playbook_config", lambda: dict(self.SEALED_CFG)
        )

    def _make_scheduler(self, price, day_high, day_low):
        """构造一个返回固定 SymbolContext 的 mock scheduler."""
        scheduler = MagicMock()
        scheduler.data_router.get_full_market_context.return_value = {
            "price": price,
            "day_high": day_high,
            "day_low": day_low,
            "volume_5d_avg_ratio": 2.0,
        }
        return scheduler

    def test_long_price_at_upper_band_trigger(self):
        # entry=100, upper=101, price=101 → 触发 (<=101)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_long_price_above_upper_band_no_trigger(self):
        # 2026-07-11: entry=100, 追涨带 +5% + 重锚上限 4%; price=106 (>6%) 两者皆超 → 未触发
        scheduler = self._make_scheduler(price=106.0, day_high=107.0, day_low=99.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_long_price_below_upper_band_trigger(self):
        # entry=100, upper=101, price=99 → 触发 (<=101)
        scheduler = self._make_scheduler(price=99.0, day_high=101.0, day_low=98.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_short_price_at_lower_band_trigger(self):
        # entry=100, lower=99, price=99 → 触发 (>=99)
        scheduler = self._make_scheduler(price=99.0, day_high=101.0, day_low=98.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_short_price_below_lower_band_no_trigger(self):
        # 2026-07-11: entry=100, 追空带 -5% + 重锚上限 4%; price=94 (>6%) 两者皆超 → 未触发
        scheduler = self._make_scheduler(price=94.0, day_high=101.0, day_low=93.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_long_reanchor_within_cap_triggers(self):
        # 2026-07-11 Lever A: entry=100, price=96.5 (跌破 -3% 接飞刀带但 <=4% 重锚上限) → 重锚触发
        scheduler = self._make_scheduler(price=96.5, day_high=100.0, day_low=96.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_short_reanchor_within_cap_triggers(self):
        # 2026-07-11 Lever A: entry=100, price=103.5 (升破 +3% 带但 <=4% 重锚上限) → 重锚触发
        scheduler = self._make_scheduler(price=103.5, day_high=104.0, day_low=99.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_short_price_above_lower_band_trigger(self):
        # entry=100, lower=99, price=100 → 触发 (>=99)
        scheduler = self._make_scheduler(price=100.0, day_high=102.0, day_low=99.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

    def test_missing_data_is_fail_closed(self):
        scheduler = MagicMock()
        scheduler.data_router.get_full_market_context.return_value = {}
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_entry_stop_immediately_intercepts(self):
        """触发后若价格已越过止损, 应返回 False 避免入场即止损."""
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=100.0)
        inst = {
            "symbol": "US.TEST",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 102.0,  # 当前价格 101 已低于 stop_loss, LONG 应被拦截
        }
        assert _check_intraday_trigger(scheduler, inst) is False

    # ── 2026-08-11 (LIF 盘前-20%实锤): 极端gap逆方向作废 ──

    def test_extreme_gap_adverse_invalidates_instruction(self, monkeypatch):
        """BUY 逆向暴跌 (>8%) → 永久作废: 落库 skip_reason=thesis_drift_invalidate.
        (防盘中反弹回触发带后 pullback 无量能确认误触发接飞刀)"""
        recorded = {}
        class _FakeDB:
            def update_instruction_tracking(self, inst_id, updates):
                recorded["id"] = inst_id
                recorded["updates"] = updates
        monkeypatch.setattr("app.playbook.db.PlaybookDB", _FakeDB)
        scheduler = self._make_scheduler(price=91.0, day_high=100.0, day_low=90.0)
        inst = {"id": 999, "symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False
        assert recorded.get("id") == 999
        assert recorded["updates"]["skip_reason"] == "thesis_drift_invalidate"

    def test_extreme_gap_short_adverse_invalidates(self, monkeypatch):
        """SELL_SHORT 逆向暴涨 (>8%) → 同样作废."""
        recorded = {}
        class _FakeDB:
            def update_instruction_tracking(self, inst_id, updates):
                recorded["id"] = inst_id
                recorded["updates"] = updates
        monkeypatch.setattr("app.playbook.db.PlaybookDB", _FakeDB)
        scheduler = self._make_scheduler(price=109.0, day_high=110.0, day_low=100.0)
        inst = {"id": 998, "symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False
        assert recorded["updates"]["skip_reason"] == "thesis_drift_invalidate"

    def test_extreme_gap_favorable_direction_not_invalidated(self, monkeypatch):
        """顺方向 gap>8% (BUY 价远在 entry 上方) → 只拦不触发, 不作废
        (thesis 可能仍有效, 只是价格暂远)."""
        recorded = {}
        class _FakeDB:
            def update_instruction_tracking(self, inst_id, updates):
                recorded["id"] = inst_id
                recorded["updates"] = updates
        monkeypatch.setattr("app.playbook.db.PlaybookDB", _FakeDB)
        scheduler = self._make_scheduler(price=109.0, day_high=110.0, day_low=105.0)
        inst = {"id": 997, "symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False
        assert recorded == {}

    def test_extreme_gap_adverse_gate_disabled_not_invalidated(self, monkeypatch):
        """thesis_drift_invalidate_enabled=False → 极端gap只拦不写作废 (回滚路径)."""
        monkeypatch.setattr(
            "app.playbook.config.get_playbook_config",
            lambda: {**self.SEALED_CFG, "thesis_drift_invalidate_enabled": False},
        )
        recorded = {}
        class _FakeDB:
            def update_instruction_tracking(self, inst_id, updates):
                recorded["id"] = inst_id
        monkeypatch.setattr("app.playbook.db.PlaybookDB", _FakeDB)
        scheduler = self._make_scheduler(price=91.0, day_high=100.0, day_low=90.0)
        inst = {"id": 996, "symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False
        assert recorded == {}
