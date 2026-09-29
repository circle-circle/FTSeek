"""Tests for app.playbook.pipeline — 关键函数回归测试."""
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, tzinfo
from threading import Barrier
from types import ModuleType, SimpleNamespace
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

    @pytest.fixture(autouse=True)
    def sealed_close_callbacks(self, monkeypatch):
        """The isolated runner seals I/O before imports; callbacks are explicit fakes."""
        calls = []
        callbacks = {}

        def module(name, **attributes):
            fake = ModuleType(name)
            fake.__dict__.update(attributes)
            monkeypatch.setitem(sys.modules, name, fake)

        def callback(name, result=None):
            mock = MagicMock(side_effect=lambda *a, **kw: (calls.append(name), result)[1])
            callbacks[name] = mock
            return mock

        sync = SimpleNamespace(
            mark_closed_tombstone=callback("tombstone"),
            confirm_tombstone=callback("confirm"),
            request_immediate_sync=callback("sync"),
        )
        module("app.core.position_sync", get_us_position_sync=lambda: sync)
        module("app.playbook.exit_shadow", finalize_exit_shadow=callback("shadow"))
        module("app.playbook.reentry_tp_shadow", settle_on_close=callback("reentry"))
        acb = SimpleNamespace(record_trade_result=callback("acb"))
        pdt = SimpleNamespace(record_close=callback("pdt"))
        module("app.risk.absolute_circuit_breaker", get_absolute_circuit_breaker=lambda: acb)
        module("app.risk.pdt_guard", get_pdt_guard=lambda: pdt)
        module("app.coordinator.manual_hold_registry", manual_hold_registry=SimpleNamespace(
            get_hold_info=lambda symbol: {}, unregister=callback("unregister")))
        module("app.analysis.trade_attribution", compute_attribution=callback("attribution", SimpleNamespace(
            summary="sealed", improvement="sealed", to_dict=lambda: {"sealed": True})))
        push = SimpleNamespace(push=callback("push"), push_text=callback("push_text"))
        module("app.push", get_push_hub=lambda: push, trade_exit=lambda **kw: kw)
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: {
            "sl_burst_alert_enabled": False})
        return SimpleNamespace(calls=calls, callbacks=callbacks)

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
        try:
            yield conn
        finally:
            conn.close()

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

    # ── [观察点1 修v2 2026-09-29] 跨夜标注功能级回归 ──
    # v1 bug: overnights 读 _tr[7] (exit_time 列), 但 _tr 在 UPDATE 前 SELECT,
    # 列恒 NULL → 标注永不出现。COIN 9/29 08:21ET 隔夜单实弹实证补丁是死的。
    # 本测试真调 _close_trade_record 并截获推送 payload — v1 代码下必红。
    def _book_with_push(self, conn, sealed, entry_time, exit_time):
        conn.execute("UPDATE trade_records SET entry_time=? WHERE id=1", (entry_time,))
        conn.commit()
        _close_trade_record(
            conn, trade_id=1, exit_price=196.34, exit_reason="SL_BREAKEVEN",
            pnl=9.2, pnl_pct=0.002, holding_hours=6.0, symbol="US.TEST",
            exit_time=exit_time,
        )
        push_mock = sealed.callbacks["push"]
        assert push_mock.call_args_list, "平仓推送未发出"
        return push_mock.call_args_list[-1].args[0]

    def test_push_marks_overnight_for_next_day_exit(self, conn, sealed_close_callbacks):
        """COIN 9/29 真实时间戳: 入场 9/28 09:59 ET, 出场 9/29 08:21 ET → 跨夜1晚。"""
        msg = self._book_with_push(
            conn, sealed_close_callbacks,
            "2026-09-28T09:59:12-04:00", "2026-09-29T08:21:38-04:00")
        assert msg["extra"]["overnights"] == 1

    def test_push_no_overnight_for_same_day_exit(self, conn, sealed_close_callbacks):
        """同日单 → overnights=0 (渲染端不加标注, 外观与旧版一致)。"""
        msg = self._book_with_push(
            conn, sealed_close_callbacks,
            "2026-09-28T09:59:12-04:00", "2026-09-28T15:00:00-04:00")
        assert msg["extra"]["overnights"] == 0

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

    @pytest.fixture
    def owned_conn(self, conn):
        conn.execute("ALTER TABLE trade_records ADD COLUMN futu_order_id TEXT")
        for definition in ("fees_est REAL", "pnl_net REAL", "fees_source TEXT"):
            conn.execute("ALTER TABLE trade_records ADD COLUMN " + definition)
        for definition in ("direction TEXT", "size REAL", "avg_entry_price REAL", "entry_time TEXT"):
            conn.execute("ALTER TABLE open_positions ADD COLUMN " + definition)
        conn.execute(
            "UPDATE trade_records SET entry_time=?, futu_order_id='entry-1', metadata_json=? WHERE id=1",
            ("2026-09-03T10:00:00-04:00", json.dumps({"peak_price": 102, "trough_price": 99})),
        )
        conn.execute(
            "INSERT INTO open_positions VALUES ('US.TEST', 'synced', 'LONG', 10, 100, ?)",
            ("2026-09-03T10:00:00-04:00",),
        )
        conn.commit()
        return conn

    def owner(self, **overrides):
        return {
            "trade_id": 1, "db_path": ":memory:", "entry_order_id": "entry-1",
            "symbol": "US.TEST", "direction": "LONG", "quantity": 10,
            "entry_price": 100, "entry_time": "2026-09-03T10:00:00-04:00",
            "source_type": "PLAYBOOK_SIM", **overrides,
        }

    def book(self, conn, **overrides):
        args = dict(trade_id=1, exit_price=101, exit_reason="TP_HIT", pnl=10,
                    pnl_pct=0.01, holding_hours=1, inst_id=1, symbol="US.TEST",
                    close_futu_order_id="exit-order-1", exit_intent_id="intent-1",
                    exit_owner=self.owner(), exit_metadata={"decision_frozen": True},
                    exit_time="2026-09-03T11:00:00-04:00")
        args.update(overrides)
        return _close_trade_record(conn, **args)

    def test_receipt_replay_is_noop_after_durable_booking(self, owned_conn, sealed_close_callbacks):
        assert self.book(owned_conn) is True
        row = dict(owned_conn.execute("SELECT * FROM trade_records WHERE id=1").fetchone())
        metadata = json.loads(row["metadata_json"])
        assert metadata["exit_intent_id"] == "intent-1"
        assert metadata["decision_frozen"] is True
        assert row["exit_time"] == "2026-09-03T11:00:00-04:00"
        assert row["holding_hours"] == 1
        assert row["mfe_pct"] == 2 and row["mae_pct"] == 1
        assert row["pnl_net"] == pytest.approx(10 - row["fees_est"])
        calls = list(sealed_close_callbacks.calls)
        assert "acb" in calls and "push" in calls and "tombstone" in calls
        assert self.book(owned_conn, exit_price=199, pnl=990) is False
        assert dict(owned_conn.execute("SELECT * FROM trade_records WHERE id=1").fetchone()) == row
        assert sealed_close_callbacks.calls == calls
        assert not owned_conn.in_transaction

    @pytest.mark.parametrize("override", [
        {"trade_id": 2}, {"entry_order_id": "new-entry"}, {"symbol": "US.OTHER"},
        {"direction": "SHORT"}, {"quantity": 11}, {"entry_price": 99},
        {"entry_time": "2026-09-03T10:01:00-04:00"}, {"source_type": "PLAYBOOK_LIVE"},
    ])
    def test_original_owner_mismatch_is_rejected(self, owned_conn, sealed_close_callbacks, override):
        with pytest.raises(ValueError, match="identity|owner"):
            self.book(owned_conn, exit_owner=self.owner(**override))
        assert owned_conn.execute("SELECT exit_time FROM trade_records WHERE id=1").fetchone()[0] is None
        assert sealed_close_callbacks.calls == []
        assert not owned_conn.in_transaction

    @pytest.mark.parametrize("receipt", [None, "intent-other"])
    def test_already_closed_conflict_cannot_claim_receipt(self, owned_conn, sealed_close_callbacks, receipt):
        owned_conn.execute("UPDATE trade_records SET exit_time='closed', exit_price=100, metadata_json=?",
                           (json.dumps({"exit_intent_id": receipt}),))
        owned_conn.commit()
        with pytest.raises(ValueError, match="closed|receipt|intent"):
            self.book(owned_conn)
        assert sealed_close_callbacks.calls == []
        assert not owned_conn.in_transaction

    @pytest.mark.parametrize("target", ["metadata_json", "exit_time"])
    def test_receipt_and_close_roll_back_together(self, owned_conn, sealed_close_callbacks, target):
        owned_conn.execute("CREATE TRIGGER reject_close BEFORE UPDATE OF " + target +
                           " ON trade_records BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
        with pytest.raises(sqlite3.DatabaseError, match="injected"):
            self.book(owned_conn)
        row = owned_conn.execute("SELECT exit_time, metadata_json FROM trade_records WHERE id=1").fetchone()
        assert row[0] is None and "exit_intent_id" not in json.loads(row[1])
        assert owned_conn.execute("SELECT outcome FROM playbook_instructions").fetchone()[0] == "holding"
        assert owned_conn.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0] == 1
        assert sealed_close_callbacks.calls == []
        assert not owned_conn.in_transaction
        owned_conn.execute("DROP TRIGGER reject_close")
        assert self.book(owned_conn) is True

    def test_pending_caller_writes_are_not_committed(self, owned_conn, sealed_close_callbacks):
        owned_conn.execute("UPDATE playbook_instructions SET outcome='caller-pending'")
        with pytest.raises(ValueError, match="clean|transaction"):
            self.book(owned_conn)
        assert owned_conn.in_transaction
        owned_conn.rollback()
        assert owned_conn.execute("SELECT outcome FROM playbook_instructions").fetchone()[0] == "holding"
        assert sealed_close_callbacks.calls == []

    @pytest.mark.parametrize("new_trade", [False, True])
    def test_new_same_symbol_position_survives_old_close(self, owned_conn, sealed_close_callbacks, new_trade):
        owned_conn.execute("UPDATE open_positions SET entry_time='2026-09-03T11:01:00-04:00', avg_entry_price=103")
        if new_trade:
            owned_conn.execute("INSERT INTO trade_records (id, symbol, direction, entry_time, entry_price, quantity, source_type) "
                               "VALUES (2, 'US.TEST', 'LONG', '2026-09-03T11:01:00-04:00', 103, 10, 'PLAYBOOK_SIM')")
        owned_conn.commit()
        assert self.book(owned_conn) is True
        row = owned_conn.execute("SELECT avg_entry_price FROM open_positions WHERE symbol='US.TEST'").fetchone()
        assert row[0] == 103
        assert "tombstone" not in sealed_close_callbacks.calls
        assert "unregister" not in sealed_close_callbacks.calls

    def test_replacement_entry_order_survives_identical_snapshot(self, owned_conn, sealed_close_callbacks):
        # upsert_position_simple preserves old entry_time on symbol conflict but changes this ID.
        owned_conn.execute("ALTER TABLE open_positions ADD COLUMN sl_broker_order_id TEXT")
        owned_conn.execute("UPDATE open_positions SET sl_broker_order_id='entry-2'")
        owned_conn.commit()
        assert self.book(owned_conn) is True
        assert owned_conn.execute("SELECT sl_broker_order_id FROM open_positions").fetchone()[0] == "entry-2"
        assert "tombstone" not in sealed_close_callbacks.calls
        assert "unregister" not in sealed_close_callbacks.calls

    def test_callbacks_see_commit_and_failures_do_not_undo_booking(self, owned_conn, sealed_close_callbacks):
        observations = []
        def fail_after_commit(*args, **kwargs):
            row = owned_conn.execute("SELECT exit_time, metadata_json FROM trade_records WHERE id=1").fetchone()
            # Assert outside callbacks: production deliberately catches callback failures.
            observations.append((owned_conn.in_transaction, row[0], json.loads(row[1]).get("exit_intent_id")))
            raise RuntimeError("callback failure")
        names = ("shadow", "reentry", "acb", "pdt", "tombstone", "unregister", "attribution", "push")
        for name in names:
            sealed_close_callbacks.callbacks[name].side_effect = fail_after_commit
        assert self.book(owned_conn) is True
        assert observations == [(False, "2026-09-03T11:00:00-04:00", "intent-1")] * len(names)
        assert self.book(owned_conn) is False
        assert len(observations) == len(names)
        assert not owned_conn.in_transaction

    def test_writer_lock_precedes_receipt_read(self, owned_conn):
        statements = []
        owned_conn.set_trace_callback(statements.append)
        assert self.book(owned_conn) is True
        statements = [s.upper().strip() for s in statements]
        begin = next(i for i, s in enumerate(statements) if s == "BEGIN IMMEDIATE")
        receipt_read = next(i for i, s in enumerate(statements) if s.startswith("SELECT") and "FROM TRADE_RECORDS" in s)
        assert begin < receipt_read

    def test_source_database_path_is_canonical_and_verified(self, owned_conn, tmp_path):
        path = tmp_path / "bookkeeping.db"
        disk = sqlite3.connect(path)
        try:
            owned_conn.backup(disk)
            with pytest.raises(ValueError, match="database identity"):
                self.book(disk, exit_owner=self.owner(db_path=str(tmp_path / "wrong.db")))
            assert not disk.in_transaction
            assert self.book(disk, exit_owner=self.owner(db_path=str(tmp_path) + "/./bookkeeping.db")) is True
        finally:
            disk.close()

    @pytest.mark.parametrize("second_intent", ["intent-1", "intent-2"])
    def test_separate_connections_serialize_receipt_claims(self, owned_conn, tmp_path, sealed_close_callbacks, second_intent):
        path = tmp_path / "concurrent-bookkeeping.db"
        disk = sqlite3.connect(path)
        owned_conn.backup(disk)
        disk.close()
        barrier = Barrier(2)
        owner = self.owner(db_path=str(path))

        def book_concurrently(intent):
            connection = sqlite3.connect(path)
            try:
                barrier.wait(timeout=5)
                try:
                    return self.book(connection, exit_intent_id=intent, exit_owner=owner)
                except ValueError as error:
                    return str(error)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(book_concurrently, ("intent-1", second_intent)))
        assert sum(result is True for result in results) == 1
        if second_intent == "intent-1":
            assert sum(result is False for result in results) == 1
        else:
            assert any(isinstance(result, str) and "receipt conflict" in result for result in results)
        assert sealed_close_callbacks.calls.count("acb") == 1
        assert sealed_close_callbacks.calls.count("push") == 1
        disk = sqlite3.connect(path)
        try:
            row = disk.execute("SELECT realized_pnl, metadata_json FROM trade_records WHERE id=1").fetchone()
            assert row[0] == 10
            assert json.loads(row[1])["exit_intent_id"] in ("intent-1", second_intent)
        finally:
            disk.close()

    def test_commit_failure_rolls_back_and_has_no_callbacks(self, owned_conn, sealed_close_callbacks):
        def reject_commit(action, arg1, arg2, db, trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        owned_conn.set_authorizer(reject_commit)
        with pytest.raises(sqlite3.DatabaseError):
            self.book(owned_conn)
        owned_conn.set_authorizer(None)
        assert not owned_conn.in_transaction
        row = owned_conn.execute("SELECT exit_time, metadata_json FROM trade_records WHERE id=1").fetchone()
        assert row[0] is None and "exit_intent_id" not in json.loads(row[1])
        assert sealed_close_callbacks.calls == []
        assert self.book(owned_conn) is True

    def test_attribution_write_failure_cannot_leave_partial_transaction(self, owned_conn):
        owned_conn.execute("CREATE TRIGGER reject_attribution BEFORE UPDATE OF metadata_json ON trade_records "
                           "WHEN NEW.metadata_json LIKE '%attribution%' "
                           "BEGIN SELECT RAISE(ABORT, 'attribution write failure'); END")
        assert self.book(owned_conn) is True
        assert not owned_conn.in_transaction
        assert self.book(owned_conn) is False

    def test_recheck_replacement_entry_created_after_commit(self, owned_conn, sealed_close_callbacks):
        def create_replacement(**kwargs):
            owned_conn.execute("INSERT INTO open_positions VALUES ('US.TEST', 'synced', 'LONG', 10, 103, ?)",
                               ("2026-09-03T11:01:00-04:00",))
            owned_conn.commit()
        sealed_close_callbacks.callbacks["shadow"].side_effect = create_replacement
        assert self.book(owned_conn) is True
        assert owned_conn.execute("SELECT avg_entry_price FROM open_positions").fetchone()[0] == 103
        assert "tombstone" not in sealed_close_callbacks.calls
        assert "unregister" not in sealed_close_callbacks.calls

    @pytest.mark.parametrize("metadata", ["[]", "null", "broken-json"])
    def test_invalid_metadata_cannot_destroy_receipt_history(self, owned_conn, metadata):
        owned_conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=1", (metadata,))
        owned_conn.commit()
        with pytest.raises(ValueError, match="metadata"):
            self.book(owned_conn)
        assert not owned_conn.in_transaction
        assert owned_conn.execute("SELECT metadata_json FROM trade_records WHERE id=1").fetchone()[0] == metadata

    def test_open_row_with_receipt_is_conflict_not_replay(self, owned_conn):
        owned_conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=1",
                           (json.dumps({"exit_intent_id": "intent-1"}),))
        owned_conn.commit()
        with pytest.raises(ValueError, match="receipt conflict"):
            self.book(owned_conn)
        assert not owned_conn.in_transaction

    def test_reused_trade_id_cannot_redeem_old_receipt(self, owned_conn):
        assert self.book(owned_conn) is True
        owned_conn.execute("UPDATE trade_records SET entry_time='2026-09-03T11:01:00-04:00', futu_order_id='entry-2' WHERE id=1")
        owned_conn.commit()
        with pytest.raises(ValueError, match="identity mismatch"):
            self.book(owned_conn)
        assert not owned_conn.in_transaction


class TestCheckIntradayTrigger:
    """_check_intraday_trigger 触发条件边界测试 (2026-06-29 后改为方向穿越触发).

    2026-07 密封: 生产新增 14:00 ET 触发截止/财报门/低ATR门/量能门等 wall-clock
    与外部数据依赖的门 — 测试注入固定 pb_cfg 关闭这些门, 只验证触发带核心逻辑.
    """

    SEALED_CFG = {
        "entry_time_cutoff_enabled": False,      # 密封 wall-clock (14:00 ET 截止)
        # 2026-09-14: 补密封同类 wall-clock 门 (开盘禁触窗, yaml true/0935)。
        #   漏密封 ⇒ ET 00:00-09:35 (= CST 12:00-21:35) 触发判定被 pipeline.py:5405
        #   静默拦掉, 本类断言随挂钟漂移; 全量跑批跨 CST 正午时会分裂成部分红。
        #   门本体另有专属测试 (TestEntryOpenBlackoutGate), 此处只密封不验证。
        "entry_open_blackout_enabled": False,
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
        # 2026-09-17 对齐 P0 宽松化 (commit 5b9948d06, 距 chase_zone 边界 ±1% 内也触发):
        #   entry=100 → 追涨带 +5% + 容忍 1% = 上界 +6% 闭区间内触发; 本用例取 +7% 真出带。
        #   (原用例 price=106.0 恰好落在新闭区间边缘 ⇒ 语义变了, 断言项保留、价位外移)
        scheduler = self._make_scheduler(price=107.0, day_high=108.0, day_low=99.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_long_price_at_chase_near_miss_edge_trigger(self, monkeypatch):
        """P0 宽松化边界 (上界含): entry=100, +6% = 追涨带 5% + 容忍 1% 的闭区间边缘 → 触发。

        自密封禁触窗: 本用例断言 True, 而 ET 00:00-09:35 禁触窗会拦掉触发判定
        (随挂钟漂移), 故在用例内显式密封该门, 不依赖类级夹具补丁。
        """
        monkeypatch.setattr("app.playbook.config.get_playbook_config",
                            lambda: {**self.SEALED_CFG, "entry_open_blackout_enabled": False})
        scheduler = self._make_scheduler(price=106.0, day_high=107.0, day_low=99.0)
        inst = {"symbol": "US.TEST", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

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
        # 2026-09-17 对齐 P0 宽松化 (commit 5b9948d06): entry=100 → 追空带 -5% + 容忍 1%
        #   = 下界 -6% 闭区间内触发; 本用例取 -7% 真出带 (原 price=94.0 恰落新边界)。
        scheduler = self._make_scheduler(price=93.0, day_high=101.0, day_low=92.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

    def test_short_price_at_chase_near_miss_edge_trigger(self, monkeypatch):
        """P0 宽松化边界 (下界含): entry=100, -6% = 追空带 5% + 容忍 1% 的闭区间边缘 → 触发。
        (禁触窗同上一用例自密封。)"""
        monkeypatch.setattr("app.playbook.config.get_playbook_config",
                            lambda: {**self.SEALED_CFG, "entry_open_blackout_enabled": False})
        scheduler = self._make_scheduler(price=94.0, day_high=101.0, day_low=93.0)
        inst = {"symbol": "US.TEST", "direction": "SELL_SHORT", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is True

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


class TestEntryOpenBlackoutGate:
    """开盘禁触窗门 (pipeline.py:5395-5407) 专属测试 — 2026-09-14 补零覆盖缺口.

    缺口背景 (为何必须补): 该门在生产 yaml 是**开启**的
    (`entry_open_blackout_enabled: true` / `entry_open_blackout_hhmm: 0935`,
    config/unified_config.yaml:1179), 但全仓此前无任何测试覆盖, 且 3 处密封夹具
    漏了这个键 ⇒ 每天 ET 00:00-09:35 (= CST 12:00-21:35) 触发判定被 :5405
    静默拦掉, 全量跑批跨 CST 正午会分裂成部分红 (2026-09-14 全量 11 failed 根因)。
    夹具侧已补密封 (只密封不验证), 本类钉门本体语义:
      1. 边界是**严格小于** (`hour*100+minute < hhmm`) → 配置当分即放行,
         与 14:00 cutoff 门的 `>=` 恰好相反, 两者不可混写;
      2. 翻转点跟随配置 hhmm (改配置边界同步移动) → 排除"别的门恰好返回 False"
         造成的假阳性 (这是本类的变异杀伤主力);
      3. 遥测 reason=before_open_blackout 且 ts/date 走 ET 尺 (R-8a);
      4. 开关关闭 / 配置畸形 / 取时异常 → 一律 fail-open 不拦 (回滚路径可验)。
    """

    # 与 TestCheckIntradayTrigger.SEALED_CFG 同值 (该类已实证可触发), 仅把禁触窗打开。
    CFG = {
        "entry_time_cutoff_enabled": False,      # 密封 14:00 ET cutoff (同类 wall-clock 门)
        "earnings_blackout_gate_enabled": False,
        "low_atr_gate_enabled": False,
        "symbol_streak_ban_enabled": False,
        "intraday_vol_gate_enabled": False,
        "entry_vwap_gate_enabled": False,
        "entry_reanchor_enabled": True,
        "entry_reanchor_max_drift_pct": 4.0,
        "thesis_drift_invalidate_enabled": True,
        "falsification_gates_enabled": False,
        "entry_open_blackout_enabled": True,     # ← 被测门
        "entry_open_blackout_hhmm": 935,
    }

    @pytest.fixture(autouse=True)
    def _sealed_pb_cfg(self, monkeypatch):
        monkeypatch.setattr(
            "app.playbook.config.get_playbook_config", lambda: dict(self.CFG)
        )

    @staticmethod
    def _freeze(monkeypatch, hour, minute, day=14):
        """把 pipeline 的 ET 墙钟冻结到 2026-09-<day> hour:minute.

        注: `_now_et()` 返回的是**时区对象**而非 datetime (见 pipeline.py:5209 注释),
        门体用 `datetime.now(_now_et())` 取墙钟, 且 `datetime` 是函数内局部导入 ⇒
        唯一注入点是让该时区的 `fromutc()` 忽略真实 UTC now。
        """
        from app.core.scheduler_triggers import ET as _ET_TZ
        frozen = _ET_TZ.localize(datetime(2026, 9, day, hour, minute, 30))

        class _FrozenZone(tzinfo):
            def utcoffset(self, dt):
                return frozen.utcoffset()

            def dst(self, dt):
                return frozen.dst()

            def tzname(self, dt):
                return frozen.tzname()

            def fromutc(self, dt):
                return frozen

        monkeypatch.setattr("app.playbook.pipeline._now_et", lambda: _FrozenZone())
        return frozen

    def _make_scheduler(self, price, day_high, day_low):
        scheduler = MagicMock()
        scheduler.data_router.get_full_market_context.return_value = {
            "price": price,
            "day_high": day_high,
            "day_low": day_low,
            "volume_5d_avg_ratio": 2.0,
        }
        return scheduler

    def _triggering(self, direction="BUY"):
        """构造一个"若无禁触窗则必然触发"的指令 (entry=100, price=101 在带上)."""
        return {"symbol": "US.TEST", "direction": direction, "entry_price": 100.0}

    # ── 1. 边界: 严格小于 ──

    def test_blocked_one_minute_before_hhmm(self, monkeypatch):
        self._freeze(monkeypatch, 9, 34)          # 934 < 935
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is False

    def test_allowed_exactly_at_hhmm(self, monkeypatch):
        """配置当分即放行 — `<` 而非 `<=` (与 cutoff 门的 `>=` 反向, 勿混写)."""
        self._freeze(monkeypatch, 9, 35)          # 935 < 935 → False ⇒ 不拦
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is True

    def test_allowed_after_hhmm(self, monkeypatch):
        self._freeze(monkeypatch, 9, 36)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is True

    def test_blocked_at_et_midnight(self, monkeypatch):
        """00:23 ET — 即夹具漏密封时每天恒红的那个真实时刻 (bridge 盘前写单后)."""
        self._freeze(monkeypatch, 0, 23)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is False

    def test_short_direction_also_blocked(self, monkeypatch):
        """门与方向无关: SHORT 同样被拦 (不是只护多头)."""
        self._freeze(monkeypatch, 9, 34)
        scheduler = self._make_scheduler(price=99.0, day_high=101.0, day_low=98.0)
        assert _check_intraday_trigger(scheduler, self._triggering("SELL_SHORT")) is False

    # ── 2. 翻转点跟随配置 (假阳性排除) ──

    @pytest.mark.parametrize("hhmm,hour,minute,expected", [
        (1015, 10, 14, False),   # 旧生产值 1015 (2026-08-15 前): 10:14 仍拦
        (1015, 10, 15, True),    # 边界随配置右移 → 10:15 放行
        (935, 10, 14, True),     # 现生产值 0935: 10:14 早已放行
        (1000, 9, 59, False),    # 缺省回落值 1000 语义一致
        (1000, 10, 0, True),
    ])
    def test_flip_point_tracks_configured_hhmm(self, monkeypatch, hhmm, hour, minute, expected):
        monkeypatch.setattr(
            "app.playbook.config.get_playbook_config",
            lambda: {**self.CFG, "entry_open_blackout_hhmm": hhmm},
        )
        self._freeze(monkeypatch, hour, minute)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is expected

    def test_missing_hhmm_falls_back_to_1000(self, monkeypatch):
        """键缺失 → 代码默认 1000 (不是 yaml 的 0935) — 钉住默认值本身."""
        cfg = {k: v for k, v in self.CFG.items() if k != "entry_open_blackout_hhmm"}
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: dict(cfg))
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        self._freeze(monkeypatch, 9, 59)
        assert _check_intraday_trigger(scheduler, self._triggering()) is False
        self._freeze(monkeypatch, 10, 0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is True

    # ── 3. 遥测口径 ──

    def test_telemetry_reason_and_et_scale(self, monkeypatch, tmp_path):
        """拦截须落 reason=before_open_blackout (与 after_cutoff 可区分), ts/date 走 ET.

        遥测路径由 `Path(__file__).parents[2]/data/` 推导 ⇒ 改模块 `__file__` 指到
        tmp 即可重定向, 既不污染生产 trigger_miss_telemetry.jsonl, 又能用非 TEST
        标的 (TEST 标的被 :5189 守卫提前 return, 不落盘)。
        """
        (tmp_path / "app" / "playbook").mkdir(parents=True)
        (tmp_path / "data").mkdir()
        monkeypatch.setattr(
            "app.playbook.pipeline.__file__", str(tmp_path / "app" / "playbook" / "pipeline.py")
        )
        frozen = self._freeze(monkeypatch, 0, 23)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        inst = {"id": 4242, "symbol": "US.OOB", "direction": "BUY", "entry_price": 100.0}
        assert _check_intraday_trigger(scheduler, inst) is False

        fp = tmp_path / "data" / "trigger_miss_telemetry.jsonl"
        assert fp.exists(), "禁触窗拦截必须留遥测痕 (否则门效果不可度量)"
        rows = [json.loads(line) for line in fp.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["reason"] == "before_open_blackout"
        assert row["inst_id"] == 4242
        assert row["symbol"] == "US.OOB"
        # ET 尺 (R-8a 2026-08-23): date 必须是 ET 当日, 不是宿主 CST 次日
        assert row["date"] == frozen.strftime("%Y-%m-%d")
        assert row["ts"].startswith(frozen.strftime("%Y-%m-%dT%H:%M"))
        assert row["tz"] == "ET"

    # ── 4. fail-open / 回滚路径 ──

    def test_disabled_flag_allows_open_window(self, monkeypatch):
        """开关关闭 → 09:34 照常触发 (yaml 注明的回滚路径: enabled 改 false)."""
        monkeypatch.setattr(
            "app.playbook.config.get_playbook_config",
            lambda: {**self.CFG, "entry_open_blackout_enabled": False},
        )
        self._freeze(monkeypatch, 9, 34)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is True

    def test_malformed_config_fails_open(self, monkeypatch):
        """hhmm 畸形 (int() 抛) → except 把 enabled 一并置 False, 不拦也不炸扫描线程."""
        monkeypatch.setattr(
            "app.playbook.config.get_playbook_config",
            lambda: {**self.CFG, "entry_open_blackout_hhmm": "09:35"},
        )
        self._freeze(monkeypatch, 9, 34)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is True

    def test_clock_exception_fails_open(self, monkeypatch):
        """取时异常 → fail-open (与既有异常处理一致, 时钟故障不得停掉全部入场)."""
        def _boom():
            raise RuntimeError("clock unavailable")
        monkeypatch.setattr("app.playbook.pipeline._now_et", _boom)
        scheduler = self._make_scheduler(price=101.0, day_high=102.0, day_low=99.0)
        assert _check_intraday_trigger(scheduler, self._triggering()) is True
