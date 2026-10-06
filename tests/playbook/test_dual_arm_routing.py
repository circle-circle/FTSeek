"""RFC-131: explicit SIM/LIVE lanes and the 1-share C-lite contract."""

import inspect
from types import SimpleNamespace

from app.playbook import pipeline
from app.playbook.db import PlaybookDB
from app.core.paths import get_trade_records_db_for_arm
from app.playbook.db import route_playbook_db
from app.playbook.live_arm_gate import evaluate_live_arm_gate


_ARM_CONFIG = {
    "enabled": True,
    "mode": "engineering_probe",
    "max_order_qty": 1,
    "max_open_positions": 3,
    "session": "RTH",
    "probe": {
        "authorized": True,
        "authorization_id": "TEST-AUTH",
        "direction": "LONG",
    },
}


def _valid_gate_kwargs():
    return {
        "allow_real_trading": True,
        "real_account_id": "123456789",
        "lane_db_name": "ftseek_us_live",
        "instruction_direction": "BUY",
        "market_phase": "TRADING",
        "open_live_positions": 0,
    }


def test_route_playbook_db_uses_explicit_lanes():
    assert route_playbook_db("sim").db_name == "ftseek_research"
    assert route_playbook_db("real").db_name == "ftseek_us_live"


def test_trade_ledger_routing_ignores_legacy_trade_env(monkeypatch, tmp_path):
    sim_override = tmp_path / "sim-lane.sqlite"
    monkeypatch.setenv("TRADE_ENV", "0")
    monkeypatch.setenv("FTSEEK_DB_FILE", str(sim_override))

    assert get_trade_records_db_for_arm("sim") == sim_override
    assert get_trade_records_db_for_arm("real").name == "ftseek_us_live.db"


def test_generation_and_execution_use_the_same_sim_lane(monkeypatch):
    monkeypatch.setenv("TRADE_ENV", "0")
    monkeypatch.delenv("FTSEEK_DB_FILE", raising=False)
    monkeypatch.setattr(
        "app.core.paths._get_config",
        lambda: SimpleNamespace(get_dotted=lambda key, default=None: "data/ftseek_us.db"),
    )

    assert PlaybookDB().db_name == "ftseek_research"
    assert route_playbook_db("sim").db_name == "ftseek_research"
    assert get_trade_records_db_for_arm("sim").name == "ftseek_us.db"


def test_research_lane_preserves_file_override(monkeypatch, tmp_path):
    override = tmp_path / "ab-playbook.sqlite"
    monkeypatch.setenv("FTSEEK_DB_FILE", str(override))

    from app.storage.db_pool import _resolve_research_db

    assert _resolve_research_db() == override


def test_auto_execute_mirrors_same_decision_to_live_probe():
    source = inspect.getsource(pipeline.run_playbook_auto_execute)
    assert 'route_playbook_db("sim")' in source
    assert 'target_env="real"' in source
    assert "_mirror_instruction_to_live" in source


def test_live_arm_gate_is_the_single_decision_surface():
    cases = [
        ("LIVE_ARM_NOT_ENABLED", {"enabled": False}),
        ("LIVE_MODE_UNSUPPORTED", {"mode": "normal"}),
        ("LIVE_PREREQ_INCOMPLETE", {"real_account_id": ""}),
        ("LIVE_PROBE_NOT_AUTHORIZED", {"arm_config": {**_ARM_CONFIG, "probe": {}}}),
        ("LIVE_PROBE_DIRECTION_REJECTED", {"instruction_direction": "SELL_SHORT"}),
        ("LIVE_PROBE_LIMIT_INVALID", {"max_order_qty": 2}),
        ("LIVE_PROBE_LIMIT_INVALID", {"max_open_positions": 4}),
        ("LIVE_PROBE_LIMIT_INVALID", {"max_open_positions": 0}),
        ("LIVE_SESSION_UNSUPPORTED", {"session": "OVERNIGHT"}),
        ("LIVE_MARKET_PHASE_REJECTED", {"market_phase": "PRE_MARKET"}),
        ("LIVE_PROBE_OPEN_CAP_REACHED", {"open_live_positions": 3}),
        ("LIVE_OPEN_COUNT_UNAVAILABLE", {"open_live_positions": None}),
    ]
    for expected_code, mutation in cases:
        kwargs = _valid_gate_kwargs()
        arm_config = dict(_ARM_CONFIG)
        if "arm_config" in mutation:
            arm_config = mutation["arm_config"]
        else:
            arm_config.update({key: value for key, value in mutation.items() if key in arm_config})
            kwargs.update({key: value for key, value in mutation.items() if key not in arm_config})
        decision = evaluate_live_arm_gate(arm_config, **kwargs)
        assert decision["allowed"] is False
        assert expected_code in {reason["code"] for reason in decision["reasons"]}


def test_rejected_live_gate_never_writes_mirror(monkeypatch):
    live_db = SimpleNamespace(db_name="ftseek_us_live")
    instruction = {"symbol": "US.TEST", "direction": "BUY"}

    def fail_if_called(inst, target_db):
        raise AssertionError("rejected LIVE gate must not write a mirror row")

    monkeypatch.setattr(pipeline, "_mirror_instruction_to_live", fail_if_called)
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")
    monkeypatch.setattr(
        "app.core.config_container.get_config",
        lambda: SimpleNamespace(get_dotted=lambda key, default=None: _ARM_CONFIG if key == "playbook.live_manual_arm" else default),
    )
    monkeypatch.setattr(
        "app.core.scheduler_triggers.get_market_phase",
        lambda: SimpleNamespace(value="TRADING"),
    )
    monkeypatch.setattr(
        "app.playbook.live_arm_gate.count_open_live_positions",
        lambda ledger: 3,
    )

    mirror_id, decision = pipeline._mirror_instruction_to_live_if_allowed(instruction, live_db)

    assert mirror_id is None
    assert decision["allowed"] is False
    assert "LIVE_PROBE_OPEN_CAP_REACHED" in {
        reason["code"] for reason in decision["reasons"]
    }


def test_engineering_probe_allows_two_open_positions():
    kwargs = _valid_gate_kwargs()
    kwargs["open_live_positions"] = 2

    decision = evaluate_live_arm_gate(_ARM_CONFIG, **kwargs)

    assert decision["allowed"] is True
    assert decision["reasons"] == []


def test_allowed_live_gate_writes_mirror_once(monkeypatch):
    live_db = SimpleNamespace(db_name="ftseek_us_live")
    instruction = {"symbol": "US.TEST", "direction": "BUY"}

    monkeypatch.setattr(pipeline, "_mirror_instruction_to_live", lambda inst, target_db: 42)
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")
    monkeypatch.setattr(
        "app.core.config_container.get_config",
        lambda: SimpleNamespace(get_dotted=lambda key, default=None: _ARM_CONFIG if key == "playbook.live_manual_arm" else default),
    )
    monkeypatch.setattr(
        "app.core.scheduler_triggers.get_market_phase",
        lambda: SimpleNamespace(value="TRADING"),
    )
    monkeypatch.setattr(
        "app.playbook.live_arm_gate.count_open_live_positions",
        lambda ledger: 0,
    )

    mirror_id, decision = pipeline._mirror_instruction_to_live_if_allowed(instruction, live_db)

    assert mirror_id == 42
    assert decision["allowed"] is True
    assert decision["reasons"] == []


def test_exit_monitor_scans_and_aggregates_both_lanes_separately():
    source = inspect.getsource(pipeline.monitor_playbook_exits)
    assert 'for _arm_target in ("sim", "real")' in source
    assert "rows_by_arm" in source
    assert "for _bs_arm_path, _bs_arm_rows in rows_by_arm.items()" in source
