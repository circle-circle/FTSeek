# -*- coding: utf-8 -*-
"""RFC-080 S2' historical contract, superseded by the RFC-131 shared LIVE gate."""

import inspect

from app.playbook.live_arm_gate import evaluate_live_arm_gate


_ARM_CONFIG = {
    "enabled": True,
    "mode": "engineering_probe",
    "max_order_qty": 1,
    "max_open_positions": 1,
    "session": "RTH",
    "probe": {
        "authorized": True,
        "authorization_id": "TEST-AUTH",
        "direction": "LONG",
    },
}


def _evaluate(allow_real: bool, lane_db_name: str):
    return evaluate_live_arm_gate(
        _ARM_CONFIG,
        allow_real_trading=allow_real,
        real_account_id="123456789",
        lane_db_name=lane_db_name,
        instruction_direction="BUY",
        market_phase="TRADING",
        open_live_positions=0,
    )


class TestHalfFlipInterlock:
    def test_halfflip_allow_false_db_sim_rejects(self):
        decision = _evaluate(False, "ftseek_research")
        assert decision["allowed"] is False
        assert "LIVE_PREREQ_INCOMPLETE" in {
            reason["code"] for reason in decision["reasons"]
        }

    def test_halfflip_allow_true_db_sim_rejects(self):
        decision = _evaluate(True, "ftseek_research")
        assert decision["allowed"] is False
        assert "LIVE_PREREQ_INCOMPLETE" in {
            reason["code"] for reason in decision["reasons"]
        }

    def test_halfflip_allow_false_db_live_rejects(self):
        decision = _evaluate(False, "ftseek_us_live")
        assert decision["allowed"] is False
        assert "LIVE_PREREQ_INCOMPLETE" in {
            reason["code"] for reason in decision["reasons"]
        }

    def test_explicit_live_lane_passes_shared_gate(self):
        assert _evaluate(True, "ftseek_us_live")["allowed"] is True


class TestSharedGateWiredInSource:
    def test_shared_gate_is_the_live_authority(self):
        # 2026-10-15 重钉: RFC-132 (10-13) 把共享门从 _execute_via_dual_broker_inner
        # 抽为 _evaluate_live_execution_gate 并前移到熔断之前 (新调用点更靠前);
        # 钉点随结构迁移, 意图不变 (共享门仍是 LIVE 唯一权威)。
        import app.playbook.playbook_service as playbook_service
        source = inspect.getsource(
            playbook_service.PlaybookService._evaluate_live_execution_gate
        )
        assert "evaluate_live_arm_gate(" in source
        assert "count_open_live_positions(" in source
        assert "_check_graduation_gate" not in source

    def test_legacy_global_real_path_is_retired(self):
        import app.playbook.playbook_service as playbook_service
        source = inspect.getsource(playbook_service.PlaybookService.execute_instruction)
        assert "LIVE_PATH_A_RETIRED" in source
        assert "_check_graduation_gate" not in source

    def test_service_does_not_downgrade_rejected_live_to_sim(self):
        import app.playbook.playbook_service as playbook_service
        source = inspect.getsource(
            playbook_service.PlaybookService._execute_via_dual_broker_inner
        )
        gate_index = source.find("if not _gate.get(\"allowed\")")
        assert gate_index >= 0
        assert "target_env = \"sim\"" not in source[gate_index:]
