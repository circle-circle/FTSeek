from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pre_market_check.py"


@pytest.fixture
def premarket_module(monkeypatch):
    monkeypatch.setenv("PREMARKET_NO_REEXEC", "1")
    spec = importlib.util.spec_from_file_location("pre_market_check_report_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeConfig:
    def __init__(self, mode="futu_simulate"):
        self.mode = mode

    def get_dotted(self, key, default=None):
        if key == "playbook.execute_mode":
            return self.mode
        return default


def test_short_execution_check_reports_active_playbook_stock_short(premarket_module, monkeypatch):
    import app.core.config_container as config_container

    monkeypatch.setattr(config_container, "get_config", lambda: _FakeConfig())

    check = premarket_module._short_execution_check()

    assert check["name"] == "做空执行策略"
    assert check["status"] == "PASS"
    assert "stock_short（正股做空）" in check["detail"]
    assert "Playbook → DualBrokerAdapter" in check["detail"]
    assert "mode=futu_simulate" in check["detail"]
    assert "options_put" not in check["detail"]
    assert "做空策略路由器" not in check["detail"]


def test_short_execution_check_fails_unknown_execute_mode(premarket_module, monkeypatch):
    import app.core.config_container as config_container

    monkeypatch.setattr(config_container, "get_config", lambda: _FakeConfig("mystery_mode"))

    check = premarket_module._short_execution_check()

    assert check["status"] == "FAIL"
    assert "mystery_mode" in check["detail"]
    assert "不在允许值" in check["detail"]


def test_short_execution_check_fails_missing_execute_mode(premarket_module, monkeypatch):
    import app.core.config_container as config_container

    monkeypatch.setattr(config_container, "get_config", lambda: _FakeConfig(""))

    check = premarket_module._short_execution_check()

    assert check["status"] == "FAIL"
    assert "未配置或为空" in check["detail"]


def test_short_execution_check_warns_deprecated_simulate_mode(premarket_module, monkeypatch):
    import app.core.config_container as config_container

    monkeypatch.setattr(config_container, "get_config", lambda: _FakeConfig("simulate"))

    check = premarket_module._short_execution_check()

    assert check["status"] == "WARN"
    assert "已废弃" in check["detail"]
    assert "futu_simulate" in check["detail"]


def test_short_execution_check_fails_when_dual_broker_import_breaks(premarket_module, monkeypatch):
    import app.core.config_container as config_container
    import sys

    monkeypatch.setattr(config_container, "get_config", lambda: _FakeConfig())
    monkeypatch.setitem(sys.modules, "app.execution.dual_broker", None)

    with pytest.raises(ImportError):
        premarket_module._short_execution_check()


def test_record_short_execution_check_escalates_overall_status_on_fail(premarket_module, monkeypatch):
    monkeypatch.setattr(
        premarket_module,
        "_short_execution_check",
        lambda: {"name": "做空执行策略", "status": "FAIL", "detail": "boom"},
    )
    results = {"checks": [], "overall_status": "PASS", "errors": [], "warnings": []}

    premarket_module._record_short_execution_check(results)

    assert results["overall_status"] == "FAIL"
    assert results["errors"] == ["做空执行策略异常: boom"]


def test_record_short_execution_check_escalates_overall_status_on_exception(premarket_module, monkeypatch):
    def _raise():
        raise ImportError("dual broker unavailable")

    monkeypatch.setattr(premarket_module, "_short_execution_check", _raise)
    results = {"checks": [], "overall_status": "PASS", "errors": [], "warnings": []}

    premarket_module._record_short_execution_check(results)

    assert results["checks"][0]["status"] == "FAIL"
    assert results["overall_status"] == "FAIL"
    assert results["errors"] == ["做空策略异常: dual broker unavailable"]


def test_instruction_source_detail_marks_ue_direct_injection_disabled(premarket_module):
    detail = premarket_module._instruction_source_detail("2026-11-11", 27, 27, 0)

    assert detail == "2026-11-11: 共27条 | DA=27 | UE=0 (UE 当日直注已停用)"
    assert "21:10注入" not in detail


def test_report_rendering_drops_stale_router_and_ue_injection_wording(premarket_module, monkeypatch):
    import app.core.config_container as config_container

    monkeypatch.setattr(config_container, "get_config", lambda: _FakeConfig())
    results = {
        "timestamp": "2026-11-11T13:46:11.021320+00:00",
        "overall_status": "PASS",
        "checks": [
            premarket_module._short_execution_check(),
            {
                "name": "当日指令统计",
                "status": "PASS",
                "detail": premarket_module._instruction_source_detail("2026-11-11", 27, 27, 0),
            },
        ],
        "errors": [],
        "warnings": [],
    }

    report = premarket_module.format_report(results)

    assert "做空执行策略" in report
    assert "stock_short（正股做空）" in report
    assert "UE 当日直注已停用" in report
    assert "options_put" not in report
    assert "21:10注入" not in report
