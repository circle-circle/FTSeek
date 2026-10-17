"""preflight fail-mode 的环境感知回落 (2026-10-17 审计修复)

病形: LIVE 启动断言要求 `risk.preflight_fail_mode=closed`, 但运行期两处异常分支
(`auto_execute_flow` 持仓预检 / `risk_gates` regime·risk 预检) 直接回落 `"open"`。
启动后配置读取失败 ⇒ 启动时锁上的 fail-close 在运行期被悄悄放开。
契约: 配置可读 → 原值; 不可读 → LIVE-capable 回落 closed, SIM 回落 open
(SIM 默认 open 是 DEB-round2 Ω.3-3 的既定设计, 不改)。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core import live_readiness as lr


class _FakeConfig:
    def __init__(self, value: str = "open", raises: bool = False):
        self._value = value
        self._raises = raises

    def get_dotted(self, key, default=None):
        if self._raises:
            raise RuntimeError("config unreadable")
        return self._value


class _FakeContainer:
    def __init__(self, fake: _FakeConfig):
        self._fake = fake

    def get_instance(self):
        return self._fake


def _patch_config(monkeypatch, fake: _FakeConfig) -> None:
    import app.core.config_container as cc
    monkeypatch.setattr(cc, "ConfigContainer", _FakeContainer(fake))


def _set_env(monkeypatch, auto: str | None, allow: str | None) -> None:
    for name, value in (("AUTO_TRADE_ACCOUNT", auto), ("ALLOW_REAL_TRADING", allow)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_readable_config_wins(monkeypatch):
    _patch_config(monkeypatch, _FakeConfig("closed"))
    assert lr.preflight_fail_mode() == "closed"

    _patch_config(monkeypatch, _FakeConfig("OPEN"))
    assert lr.preflight_fail_mode() == "open", "读取成功时按配置 (归一化大小写)"


def test_unreadable_config_sim_defaults_open(monkeypatch):
    _set_env(monkeypatch, None, None)
    _patch_config(monkeypatch, _FakeConfig(raises=True))
    assert lr.preflight_fail_mode() == "open"


def test_unreadable_config_live_capable_defaults_closed(monkeypatch):
    _set_env(monkeypatch, "real", "true")
    _patch_config(monkeypatch, _FakeConfig(raises=True))
    assert lr.preflight_fail_mode() == "closed", \
        "LIVE-capable 下配置读失败必须维持启动断言的 fail-close"


def test_live_capable_requires_both_flags(monkeypatch):
    """AUTO=real 但 ALLOW_REAL_TRADING!=true 在 di_container 会被降级 SIM ⇒ 这里同口径。"""
    _set_env(monkeypatch, "real", "false")
    _patch_config(monkeypatch, _FakeConfig(raises=True))
    assert lr.preflight_fail_mode() == "open"


def _break_regime_assess(monkeypatch) -> None:
    import app.us_pipeline.emergency_interceptor as ei
    import app.playbook.market_regime as mr

    monkeypatch.setattr(ei, "check_kill_switch", lambda: False)

    def _boom(self):
        raise RuntimeError("regime source down")

    monkeypatch.setattr(mr.MarketRegimeGate, "assess", _boom)


def test_preflight_fail_close_in_live_env(monkeypatch):
    _set_env(monkeypatch, "real", "true")
    _patch_config(monkeypatch, _FakeConfig(raises=True))
    _break_regime_assess(monkeypatch)

    from app.playbook.pipeline_parts.risk_gates import _auto_execute_preflight

    out = _auto_execute_preflight(
        "2026-10-17", check_daily_loss_circuit=lambda _d: None,
        logger=logging.getLogger("test.preflight"),
    )
    assert out and out["reason"] == "preflight_fail_close"


def test_preflight_fail_open_in_sim(monkeypatch):
    _set_env(monkeypatch, "sim", None)
    _patch_config(monkeypatch, _FakeConfig(raises=True))
    _break_regime_assess(monkeypatch)

    from app.playbook.pipeline_parts.risk_gates import _auto_execute_preflight

    out = _auto_execute_preflight(
        "2026-10-17", check_daily_loss_circuit=lambda _d: None,
        logger=logging.getLogger("test.preflight"),
    )
    assert out is None, "SIM 面既定设计是 fail-open"


def test_position_preflight_uses_env_aware_helper():
    """结构钉: 持仓预检失败分支不得再写死 `_r1c_mode = "open"`。"""
    from app.playbook.pipeline_parts import auto_execute_flow

    src = Path(auto_execute_flow.__file__).read_text(encoding="utf-8")
    assert "preflight_fail_mode" in src
    assert '_r1c_mode = "open"' not in src
