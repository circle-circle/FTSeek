"""Tests for app.playbook.playbook_service — 业务编排层"""
import pytest
import sqlite3
from unittest.mock import MagicMock, patch

from app.playbook.db import PlaybookDB
from app.playbook.playbook_service import PlaybookService


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def svc(db):
    return PlaybookService(playbook_db=db)


def _save_inst(db, **kwargs):
    defaults = {
        "date": "2026-06-18",
        "symbol": "US.NTES",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": 50,
        "confidence": 72,
        "conditions": ["above_ma20", "insider_cluster_buy"],
    }
    defaults.update(kwargs)
    return db.save_instruction(defaults)


class TestQueryMethods:
    def test_get_today_instructions(self, svc, db):
        _save_inst(db, symbol="US.NTES")
        _save_inst(db, symbol="US.ARM")
        result = svc.get_today_instructions("2026-06-18")
        assert len(result) == 2

    def test_get_today_empty(self, svc):
        result = svc.get_today_instructions("2020-01-01")
        assert result == []

    def test_get_summary(self, svc, db):
        _save_inst(db)
        summary = svc.get_playbook_summary(days=30)
        assert "today_date" in summary
        assert "today_instructions_count" in summary

    def test_get_factor_stats(self, svc, db):
        db.upsert_factor_stat({
            "factor_key": "insider_cluster_buy",
            "analysis_level": "single",
            "verdict": "positive",
        })
        stats = svc.get_factor_stats()
        assert "single" in stats
        assert len(stats["single"]) == 1

    def test_get_patterns(self, svc, db):
        db.upsert_pattern({
            "pattern_key": "hash_abc",
            "conditions": ["a", "b"],
            "status": "active",
        })
        patterns = svc.get_patterns(status="active")
        assert len(patterns) == 1


class TestSkipInstruction:
    def test_skip_basic(self, svc, db):
        inst_id = _save_inst(db)
        result = svc.skip_instruction(inst_id, reason="risk_too_high")
        assert result["success"] is True

        # Verify DB updated
        inst = db.get_instruction("2026-06-18", "US.NTES")
        assert inst["outcome"] == "skipped"

        # Verify feedback saved
        feedbacks = db.get_user_feedback(inst_id)
        assert len(feedbacks) == 1
        assert feedbacks[0]["action"] == "skipped"

    def test_skip_with_custom_reason(self, svc, db):
        inst_id = _save_inst(db)
        result = svc.skip_instruction(inst_id, custom_reason="我有内部消息")
        assert result["success"] is True

        feedbacks = db.get_user_feedback(inst_id)
        assert "内部消息" in feedbacks[0]["skip_reason"]

    def test_skip_nonexistent(self, svc):
        result = svc.skip_instruction(99999, reason="test")
        assert result["success"] is False


class TestModifyInstruction:
    def test_modify_stop_loss(self, svc, db):
        inst_id = _save_inst(db, stop_loss=95.0)
        result = svc.modify_instruction(inst_id, "stop_loss", "92.0")
        assert result["success"] is True

        feedbacks = db.get_user_feedback(inst_id)
        assert len(feedbacks) == 1
        assert feedbacks[0]["action"] == "modified"
        assert feedbacks[0]["modified_field"] == "stop_loss"
        assert feedbacks[0]["original_value"] == "95.0"
        assert feedbacks[0]["modified_value"] == "92.0"

    def test_modify_nonexistent(self, svc):
        result = svc.modify_instruction(99999, "stop_loss", "90")
        assert result["success"] is False


class TestExecuteInstruction:
    @patch("app.playbook.playbook_service.PlaybookService._find_instruction_by_id")
    def test_execute_skip_fails(self, mock_find, svc, db):
        """SKIP 指令不可执行"""
        inst_id = _save_inst(db, direction="SKIP")
        mock_find.return_value = db.get_instruction("2026-06-18", "US.NTES")
        result = svc.execute_instruction(inst_id)
        assert result["success"] is False
        assert "SKIP" in result["message"]

    @patch("app.playbook.playbook_service.PlaybookService._find_instruction_by_id")
    def test_execute_already_executed(self, mock_find, svc, db):
        """已执行的指令不可重复执行"""
        inst_id = _save_inst(db)
        db.update_instruction_tracking(inst_id, {"executed": 1})
        mock_find.return_value = db.get_instruction("2026-06-18", "US.NTES")
        result = svc.execute_instruction(inst_id)
        assert result["success"] is False
        assert "已执行" in result["message"]

    def test_execute_nonexistent(self, svc):
        result = svc.execute_instruction(99999)
        assert result["success"] is False


class TestDirectionGateC:
    """议题二C (2026-08-01): BUY 半码 + BUY 日执行配额."""

    @staticmethod
    def _patch_gates_ok():
        """放行 execute_instruction 内 direction_gate 之前的全部硬门.

        equity=0 跳过 OKR 熔断; ACB 直接放行;
        2026-08-23 (R-1/R-5, DEB-20260823) 新增两道前置 fail-close 门, 必须一并放行,
        否则这批只关心 BUY 配额的用例会被前置门拦下:
          - capital_safety.can_open_position() 水位硬门 (返回值必须是 2-tuple, 否则解包失败 → fail-close)
          - VIX 新鲜度 fail-close + VIXGuard 阈值硬拒
        """
        p_cs = patch("app.risk.capital_safety.get_capital_safety_system")
        p_acb = patch("app.risk.absolute_circuit_breaker.get_absolute_circuit_breaker")
        p_vix = patch("app.analysis.market_state.get_checker")
        p_vg = patch("app.risk.vix_guard.VIXGuard")
        mock_cs = p_cs.start()
        mock_cs.return_value._state.current_equity = 0
        mock_cs.return_value.can_open_position.return_value = (True, "")
        mock_acb = p_acb.start()
        mock_acb.return_value.can_open_new_position.return_value = (True, "")
        mock_vix = p_vix.start()
        mock_vix.return_value.get_vix_cached.return_value = 18.0
        mock_vg = p_vg.start()
        mock_vg.return_value.should_block.return_value = (False, "")
        return (p_cs, p_acb, p_vix, p_vg)

    def teardown_method(self):
        patch.stopall()

    def _find(self, db, symbol):
        return patch(
            "app.playbook.playbook_service.PlaybookService._find_instruction_by_id",
            return_value=db.get_instruction("2026-06-18", symbol),
        )

    @staticmethod
    def _patch_dg(quota=2, factor=1.0):
        """钉死 direction_gate 配置 (与 yaml 现值解耦 — 裁决会把 quota 2→4).

        2026-08-06 (8e281cd3, 用户指示): buy_position_factor 0.5→1.0 恢复全码 —
        半码致名义 $2,500 < admission min_notional=3000 被物理拦截。默认 factor=1.0。
        """
        mock_cfg = MagicMock()
        mock_cfg.get_dotted.side_effect = lambda k, d=None: {
            "playbook.direction_gate.buy_daily_exec_quota": quota,
            "playbook.direction_gate.buy_position_factor": factor,
        }.get(k, d)
        return patch("app.core.config_container.get_config", return_value=mock_cfg)

    def test_buy_quota_blocks_third(self, svc, db):
        """配额=2 时当日已有 2 笔 executed BUY → 第 3 笔被拦且不置 executed."""
        self._patch_gates_ok()
        for sym in ("US.QA", "US.QB"):
            iid = _save_inst(db, symbol=sym)
            db.update_instruction_tracking(iid, {"executed": 1})
        iid3 = _save_inst(db, symbol="US.QC")
        with self._patch_dg(quota=2), self._find(db, "US.QC"):
            result = svc.execute_instruction(iid3)
        assert result["success"] is False
        assert result.get("skipped_buy_quota") is True
        row = db.get_instruction("2026-06-18", "US.QC")
        assert not row["executed"]
        assert "buy_quota" in (row["skip_reason"] or "")

    def test_buy_quota_rollback_999(self, svc, db):
        """配额=999 (回滚态) → 第 3 笔 BUY 通过配额门."""
        self._patch_gates_ok()
        for sym in ("US.RA", "US.RB"):
            iid = _save_inst(db, symbol=sym)
            db.update_instruction_tracking(iid, {"executed": 1})
        _save_inst(db, symbol="US.RC")
        with self._patch_dg(quota=999), \
             self._find(db, "US.RC"), \
             patch("app.playbook.playbook_service.PlaybookService._execute_via_dual_broker",
                   return_value={"success": True}) as mock_broker:
            result = svc.execute_instruction(1)
        assert result["success"] is True
        assert mock_broker.called

    def test_buy_position_factor_full_size(self, svc, db):
        """2026-08-06 裁决 factor=1.0 恢复全码: BUY size=50 原样传入下单路径 (不再半码)."""
        self._patch_gates_ok()
        _save_inst(db, symbol="US.PA", position_size=50)
        with self._patch_dg(), self._find(db, "US.PA"), \
             patch("app.playbook.playbook_service.PlaybookService._execute_via_dual_broker",
                   return_value={"success": True}) as mock_broker:
            svc.execute_instruction(1)
        # _execute_via_dual_broker 第 8 个位置参数 = position_size
        assert mock_broker.call_args[0][7] == 50

    def test_short_size_untouched(self, svc, db):
        """SHORT 不受方向门影响: size 50 原样通过, 配额门不计 SHORT."""
        self._patch_gates_ok()
        _save_inst(db, symbol="US.PS", direction="SHORT", position_size=50)
        with self._patch_dg(), self._find(db, "US.PS"), \
             patch("app.playbook.playbook_service.PlaybookService._execute_via_dual_broker",
                   return_value={"success": True}) as mock_broker:
            svc.execute_instruction(1)
        assert mock_broker.call_args[0][7] == 50

    def test_buy_quota_not_counting_short(self, svc, db):
        """当日 executed SHORT 不占用 BUY 配额."""
        self._patch_gates_ok()
        for sym in ("US.SA", "US.SB", "US.SC"):
            iid = _save_inst(db, symbol=sym, direction="SHORT")
            db.update_instruction_tracking(iid, {"executed": 1})
        _save_inst(db, symbol="US.SD", direction="BUY")
        with self._patch_dg(quota=2), self._find(db, "US.SD"), \
             patch("app.playbook.playbook_service.PlaybookService._execute_via_dual_broker",
                   return_value={"success": True}) as mock_broker:
            result = svc.execute_instruction(1)
        assert result["success"] is True
        assert mock_broker.called


class TestInstructionDetail:
    def test_detail_with_feedback(self, svc, db):
        inst_id = _save_inst(db)
        db.save_user_feedback({"instruction_id": inst_id, "action": "executed"})

        detail = svc.get_instruction_detail(inst_id)
        assert detail is not None
        assert len(detail["user_feedback"]) == 1

    def test_detail_not_found(self, svc):
        detail = svc.get_instruction_detail(99999)
        assert detail is None
