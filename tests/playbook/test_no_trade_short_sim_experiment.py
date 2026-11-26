"""[DEB-20261125 §8] NO_TRADE SHORT 直接验证实验契约 (SIM-only)。

用户裁 (2026-11-25): SIM 无真实资金, 直接验证高 VIX 恐惧日 DA SHORT 成本后期望。
契约:
  1. 开关默认关; 开启仅 SIM 生效, LIVE-capable 恒不生效 (代码闸);
  2. 配置读取失败 = 关 (回落 NO_TRADE 全禁旧门);
  3. NO_TRADE 下 SHORT 乘数 = risk_off 同款 0.3, 非 SHORT 恒 0.0;
  4. preflight: 开关关/LIVE → regime_no_trade 旧拦截; 开关开 SIM → 放行本轮;
  5. 主循环有 LONG 方向过滤钉 (NO_TRADE_EXP_LONG_BLOCKED, 按轮暂缓不写终态
     skip_reason — 行为用例见 test_entry_data_bounded_fetch) 与 sizing 第二层。
"""
from __future__ import annotations

import logging
import pytest
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.playbook.config as pb_config
import app.us_pipeline.emergency_interceptor as ei
import app.playbook.market_regime as mr
import app.us_pipeline.risk_state_machine as rsm
from app.playbook.pipeline_parts.risk_gates import (
    _auto_execute_preflight,
    no_trade_short_regime_mult,
    no_trade_short_sim_experiment_active,
)


class _FakePBConfig:
    def __init__(self, value=False, raises=False):
        self._value = value
        self._raises = raises

    def get(self, key, default=None):
        if self._raises:
            raise RuntimeError("config unreadable")
        if key == "no_trade_short_sim_experiment_enabled":
            return self._value
        return default


def _patch_pb(monkeypatch, fake):
    monkeypatch.setattr(pb_config, "get_playbook_config", lambda: fake)


def _set_env(monkeypatch, auto, allow):
    for name, value in (("AUTO_TRADE_ACCOUNT", auto), ("ALLOW_REAL_TRADING", allow)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_flag_default_off(monkeypatch):
    _set_env(monkeypatch, "sim", None)
    _patch_pb(monkeypatch, _FakePBConfig(value=False))
    assert no_trade_short_sim_experiment_active() is False


def test_flag_on_sim_active(monkeypatch):
    _set_env(monkeypatch, "sim", None)
    _patch_pb(monkeypatch, _FakePBConfig(value=True))
    assert no_trade_short_sim_experiment_active() is True


def test_flag_on_live_capable_never_active(monkeypatch):
    _set_env(monkeypatch, "real", "true")
    _patch_pb(monkeypatch, _FakePBConfig(value=True))
    assert no_trade_short_sim_experiment_active() is False


def test_config_error_falls_back_off(monkeypatch):
    _set_env(monkeypatch, "sim", None)
    _patch_pb(monkeypatch, _FakePBConfig(raises=True))
    assert no_trade_short_sim_experiment_active() is False


def test_flag_on_sim_allow_false_skips_live_arm_probe(monkeypatch):
    import app.core.config_container as config_container

    _set_env(monkeypatch, "sim", "false")
    _patch_pb(monkeypatch, _FakePBConfig(value=True))

    def unreadable_config():
        raise AssertionError("ALLOW=false must not probe live-arm capability")

    monkeypatch.setattr(config_container, "get_config", unreadable_config)
    assert no_trade_short_sim_experiment_active() is True


def test_flag_on_sim_with_long_only_live_arm_stays_active(monkeypatch):
    import app.core.config_container as config_container
    import app.core.live_readiness as live_readiness
    import app.playbook.live_arm_gate as live_arm_gate

    _set_env(monkeypatch, "sim", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")
    _patch_pb(monkeypatch, _FakePBConfig(value=True))
    arm = {
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
    monkeypatch.setattr(
        config_container, "get_config",
        lambda: SimpleNamespace(get_dotted=lambda path, default=None: arm),
    )
    monkeypatch.setattr(live_readiness, "live_fail_mode_issues", lambda: [])
    monkeypatch.setattr(
        live_arm_gate, "read_live_probe_waiver",
        lambda: {
            "active": True,
            "authorization_id": "TEST-AUTH",
            "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        },
    )
    assert no_trade_short_sim_experiment_active() is True


def test_flag_on_sim_with_short_capable_live_arm_inactive(monkeypatch):
    import app.core.config_container as config_container
    import app.playbook.live_arm_gate as live_arm_gate

    _set_env(monkeypatch, "sim", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")
    _patch_pb(monkeypatch, _FakePBConfig(value=True))
    arm = {"enabled": True, "probe": {"direction": "SHORT"}}
    calls = []

    def fake_gate(arm_config, **kwargs):
        calls.append(kwargs)
        return {"allowed": True, "reasons": []}

    monkeypatch.setattr(
        config_container, "get_config",
        lambda: SimpleNamespace(get_dotted=lambda path, default=None: arm),
    )
    monkeypatch.setattr(live_arm_gate, "evaluate_live_arm_gate", fake_gate)
    assert no_trade_short_sim_experiment_active() is False
    assert calls and calls[0]["instruction_direction"] == "SELL_SHORT"
    assert calls[0]["lane_db_name"] == "ftseek_us_live"


def test_live_arm_capability_error_falls_back_off(monkeypatch):
    import app.core.config_container as config_container
    import app.playbook.live_arm_gate as live_arm_gate

    _set_env(monkeypatch, "sim", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")
    _patch_pb(monkeypatch, _FakePBConfig(value=True))
    arm = {"enabled": True}

    def broken_gate(arm_config, **kwargs):
        raise RuntimeError("capability unreadable")

    monkeypatch.setattr(
        config_container, "get_config",
        lambda: SimpleNamespace(get_dotted=lambda path, default=None: arm),
    )
    monkeypatch.setattr(live_arm_gate, "evaluate_live_arm_gate", broken_gate)
    assert no_trade_short_sim_experiment_active() is False


def test_mult_short_active_gets_risk_off_sizing():
    assert no_trade_short_regime_mult("SELL_SHORT", True) == 0.3
    assert no_trade_short_regime_mult("SHORT", True) == 0.3
    assert no_trade_short_regime_mult("sell_short", True) == 0.3


def test_mult_non_short_or_inactive_zero():
    assert no_trade_short_regime_mult("BUY", True) == 0.0
    assert no_trade_short_regime_mult("LONG", True) == 0.0
    assert no_trade_short_regime_mult("SELL_SHORT", False) == 0.0
    assert no_trade_short_regime_mult(None, True) == 0.0


def _preflight_env(monkeypatch, *, flag, auto="sim", allow=None):
    _set_env(monkeypatch, auto, allow)
    _patch_pb(monkeypatch, _FakePBConfig(value=flag))
    monkeypatch.setattr(ei, "check_kill_switch", lambda: False)
    monkeypatch.setattr(
        mr.MarketRegimeGate, "assess",
        lambda self: SimpleNamespace(regime=mr.MarketRegime.NO_TRADE, vix=21.8),
    )
    monkeypatch.setattr(
        rsm, "RiskStateMachine",
        lambda: SimpleNamespace(get_state=lambda: rsm.RiskState.NORMAL),
    )


def _run_preflight():
    return _auto_execute_preflight(
        "2026-11-25", check_daily_loss_circuit=lambda _d: None,
        logger=logging.getLogger("test.nt_exp"),
    )


def test_preflight_no_trade_flag_off_keeps_old_block(monkeypatch):
    _preflight_env(monkeypatch, flag=False)
    out = _run_preflight()
    assert out and out["reason"] == "regime_no_trade"


def test_preflight_no_trade_flag_on_sim_passes(monkeypatch):
    _preflight_env(monkeypatch, flag=True)
    assert _run_preflight() is None


def test_preflight_no_trade_flag_on_live_keeps_block(monkeypatch):
    _preflight_env(monkeypatch, flag=True, auto="real", allow="true")
    out = _run_preflight()
    assert out and out["reason"] == "regime_no_trade"


def test_flow_has_direction_filter_and_sizing_second_layer():
    from app.playbook.pipeline_parts import auto_execute_flow
    src = Path(auto_execute_flow.__file__).read_text(encoding="utf-8")
    assert "NO_TRADE_EXP_LONG_BLOCKED" in src
    assert "no_trade_short_regime_mult" in src
    assert "_nt_short_exp" in src
    assert "no_trade_short_exp" in src, "cohort 标记必须落指令 metadata"


@pytest.mark.parametrize("flag,auto,allow,reason", [
    (False, "sim", None, "regime_no_trade"),
    (True, "sim", None, None),
    (True, "real", "true", "regime_no_trade"),
])
def test_preflight_audit_does_not_change_permissions(monkeypatch, flag, auto, allow, reason):
    _preflight_env(monkeypatch, flag=flag, auto=auto, allow=allow)
    context = mr.RegimeContext(
        regime=mr.MarketRegime.NO_TRADE, vix=21.8, spy_price=490, spy_ma20=540,
        spy_ma50=560, spy_ma200=520, spy_change_pct=0, score=-6.7, top_n=0,
        position_multiplier=0, min_strength=999, assessed_at="2026-11-25T15:00:00+00:00",
        vix_observation={"source": "vixy_proxy_live", "value": 21.8})
    calls = []
    monkeypatch.setattr(mr.MarketRegimeGate, "assess", lambda self: calls.append("assess") or context)
    audit = {}
    out = _auto_execute_preflight("2026-11-25", check_daily_loss_circuit=lambda today: None,
                                  logger=logging.getLogger("test.nt_exp"), regime_audit=audit)
    assert (out["reason"] if out else None) == reason
    assert calls == ["assess"]
    assert audit["preflight"] == context.to_dict()
    audit["preflight"]["vix_observation"]["source"] = "changed-audit"
    assert context.vix_observation["source"] == "vixy_proxy_live"


@pytest.mark.parametrize("gate", ["kill", "risk", "daily"])
def test_regime_audit_never_bypasses_existing_hard_gates(monkeypatch, gate):
    _preflight_env(monkeypatch, flag=True)
    if gate == "kill":
        monkeypatch.setattr(ei, "check_kill_switch", lambda: True)
    elif gate == "risk":
        monkeypatch.setattr(rsm, "RiskStateMachine", lambda: SimpleNamespace(get_state=lambda: rsm.RiskState.HALT))
    audit = {}
    out = _auto_execute_preflight(
        "2026-11-25", check_daily_loss_circuit=lambda today: {"triggered": gate == "daily"},
        logger=logging.getLogger("test.nt_exp"), regime_audit=audit)
    assert out is not None
    assert out["reason"] == {"kill": "kill_switch", "risk": f"risk_state_{rsm.RiskState.HALT}",
                              "daily": "daily_loss_circuit"}[gate]
    if gate == "kill":
        assert audit == {}


def test_failed_audit_cannot_change_preflight_result(monkeypatch):
    _preflight_env(monkeypatch, flag=True)
    def failed_dict():
        raise RuntimeError("audit failure")
    monkeypatch.setattr(mr.MarketRegimeGate, "assess", lambda self: SimpleNamespace(
        regime=mr.MarketRegime.NO_TRADE, vix=21.8, to_dict=failed_dict))
    audit = {}
    out = _auto_execute_preflight("2026-11-25", check_daily_loss_circuit=lambda today: None,
                                  logger=logging.getLogger("test.nt_exp"), regime_audit=audit)
    assert out is None and audit == {}
