# -*- coding: utf-8 -*-
"""ExitPolicy 启动门 LIVE fail-close 契约 (2026-10-08 运维体检批).

病根: main.py Layer 1.5 原实现校验失败只打 CRITICAL 继续启动 — 对 SIM 降级
可接受, 但对 LIVE-capable 模式 (broker/db real 或 ALLOW_REAL_TRADING=true,
含已授权工程探针窗口) 等于"止损保护可能缺失但照常开真钱仓"。

钉四件事:
  1. LIVE-capable + 校验失败 → SystemExit(拒绝启动);
  2. LIVE-capable + 校验器自身异常 → 同样 SystemExit (fail-close);
  3. SIM + 同两种失败形态 → 不抛 (保持既有降级运行语义);
  4. LIVE-capable + 校验通过 → 不抛 (正常路径零误伤)。
"""
from types import SimpleNamespace

import pytest

import main as main_mod


def _result(valid=True, violations=None, warnings=None):
    return SimpleNamespace(
        is_valid=valid,
        rules_violated=violations or [],
        rules_enabled=["atr_stop", "time_exit", "pre_close", "breakeven"],
        version="3.0",
        warnings=warnings or [],
    )


def _patch_validator(monkeypatch, behavior):
    import app.risk.exit_policy_guardian as guardian
    if isinstance(behavior, Exception):
        def _raise():
            raise behavior

        monkeypatch.setattr(guardian, "validate_exit_policy", _raise)
    else:
        monkeypatch.setattr(guardian, "validate_exit_policy", lambda: behavior)


def test_live_capable_invalid_result_blocks_startup(monkeypatch):
    _patch_validator(monkeypatch, _result(valid=False, violations=["atr_stop 未实现"]))
    with pytest.raises(SystemExit, match="ExitPolicy 校验失败"):
        main_mod._exit_policy_startup_gate(live_capable=True)


def test_live_capable_validator_error_blocks_startup(monkeypatch):
    _patch_validator(monkeypatch, RuntimeError("guardian boom"))
    with pytest.raises(SystemExit, match="ExitPolicy 校验异常"):
        main_mod._exit_policy_startup_gate(live_capable=True)


def test_sim_invalid_result_keeps_degraded_mode(monkeypatch):
    _patch_validator(monkeypatch, _result(valid=False, violations=["atr_stop 未实现"]))
    assert main_mod._exit_policy_startup_gate(live_capable=False) is None


def test_sim_validator_error_keeps_degraded_mode(monkeypatch):
    _patch_validator(monkeypatch, RuntimeError("guardian boom"))
    assert main_mod._exit_policy_startup_gate(live_capable=False) is None


def test_live_capable_valid_result_passes(monkeypatch):
    _patch_validator(monkeypatch, _result(valid=True))
    assert main_mod._exit_policy_startup_gate(live_capable=True) is None
