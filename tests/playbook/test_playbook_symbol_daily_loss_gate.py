"""playbook 执行链单标的日亏损门回归测试 (2026-08-28, 全链路复查 P0-1).

背景: 8/27 UBER 隔夜跳空 -307.13 之后复核发现, "$500/标的/日" 这道死亡螺旋防线
只写在主线 order_executor._check_symbol_daily_loss — 而主线开仓路径自 2026-07-02
冻结 (playbook 经 dual_broker 直连下单, 不经 order_executor), 近 30 日 237 笔全部
由 playbook 路径成交 → **实盘链路上此门根本不存在** (同标的当日连亏 5 笔仍可续开)。
修复 = 在 _execute_via_dual_broker_inner 的 ManualHoldRegistry 检查之后接入门,
口径与阈值共用 app/risk/symbol_day_loss。

覆盖点:
  1. 超限 → 跳过开仓 (success=False + skipped_symbol_daily_loss), 且不下单
  2. 未超限 → 放行到下游 (以 PDT 门为下游哨兵, 证明本门未误拦)
  3. 只作用于开仓方向 (SELL/BUY_BACK 平仓不查此门)
  4. 查询异常 → fail-open 放行但留 WARNING (与主线同名门一致)
  5. 配置总闸 symbol_daily_loss_gate_enabled=false → 完全旁路 (回滚路径)
  6. 拦截日志可见 (WARNING, 非 debug)

回滚: 删本文件 + playbook.symbol_daily_loss_gate_enabled=false 即可。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import app.execution.dual_broker as dual_broker_mod
import app.playbook.config as pb_cfg_mod
import app.risk.pdt_guard as pdt_guard_mod
import app.risk.symbol_day_loss as sdl_mod
from app.coordinator.manual_hold_registry import manual_hold_registry
from app.playbook.playbook_service import PlaybookService

_DOWNSTREAM_HIT = object()


class _StubPlaybookConfig:
    def __init__(self, overrides=None):
        self._o = overrides or {}

    def get(self, key, default=None):
        return self._o.get(key, default)


@pytest.fixture
def gate_env(monkeypatch):
    """隔离: 手动持仓放行 / 无真实 broker / 下游以 PDT 哨兵可见 / 日亏损尺可编."""
    svc = PlaybookService.__new__(PlaybookService)  # 绕开 PlaybookDB 初始化

    monkeypatch.setattr(
        manual_hold_registry, "is_auto_blocked", lambda symbol: False
    )

    monkeypatch.setattr(
        dual_broker_mod, "get_dual_broker",
        lambda *a, **k: pytest.fail("测试不应触达下单 (dual_broker 被调用)"),
    )

    # 下游哨兵: PDT 门 (fail-close 默认) — 命中即证明"已通过日亏损门"
    def _pdt_hit():
        raise RuntimeError(_DOWNSTREAM_HIT)

    monkeypatch.setattr(pdt_guard_mod, "get_pdt_guard", _pdt_hit)

    import app.core.config_container as cc

    class _Cfg:
        def get_dotted(self, key, default=None):
            if key == "risk.acb_fail_mode":
                return "closed"
            return default

    monkeypatch.setattr(cc, "get_config", lambda: _Cfg())

    monkeypatch.setattr(
        pb_cfg_mod, "get_playbook_config", lambda: _StubPlaybookConfig()
    )

    calls = []

    def _spy(symbol, limit_usd=None, et_date=None, db_path=None):
        calls.append(symbol)
        return (False, 0.0)

    monkeypatch.setattr(sdl_mod, "is_symbol_day_loss_exceeded", _spy)
    svc._dl_calls = calls
    return svc


def _run(svc, direction="BUY"):
    return svc._execute_via_dual_broker_inner(
        inst={"id": 9001, "symbol": "US.MU"},
        instruction_id=9001,
        symbol="US.MU",
        direction=direction,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        position_size=10,
        mos_direction=None,
        matched_decision_id=None,
        target_env="sim",
    )


def _hit_downstream(result):
    return "PDT" in (result.get("message") or "")


# ── 1. 超限拦截 ──
def test_gate_blocks_open_when_day_loss_exceeded(gate_env, monkeypatch, caplog):
    def _exceeded(symbol, **kw):
        gate_env._dl_calls.append(symbol)
        return (True, -600.0)

    monkeypatch.setattr(sdl_mod, "is_symbol_day_loss_exceeded", _exceeded)
    import logging
    with caplog.at_level(logging.WARNING):
        r = _run(gate_env)

    assert r["success"] is False
    assert r["skipped_symbol_daily_loss"] is True
    assert "-600.00" in r["message"]
    assert "日亏损" in caplog.text or "超限" in caplog.text
    assert gate_env._dl_calls == ["US.MU"]   # 尺按标的查, 不是全账户


# ── 2. 未超限放行 ──
def test_gate_passes_when_within_limit(gate_env, monkeypatch):
    monkeypatch.setattr(
        sdl_mod, "is_symbol_day_loss_exceeded",
        lambda symbol, **kw: (False, -499.0),
    )
    r = _run(gate_env)
    assert r.get("skipped_symbol_daily_loss") is None
    assert _hit_downstream(r), "应继续走到下游门 (证明本门未误拦)"


# ── 3. 只管开仓方向 ──
@pytest.mark.parametrize("direction", ["SELL", "BUY_BACK"])
def test_gate_not_applied_to_closing_directions(gate_env, direction):
    r = _run(gate_env, direction=direction)
    assert gate_env._dl_calls == []
    assert _hit_downstream(r)


# ── 4. 查询失败 fail-open + 可见 ──
def test_query_failure_fails_open_with_warning(gate_env, monkeypatch, caplog):
    def _boom(symbol, **kw):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sdl_mod, "is_symbol_day_loss_exceeded", _boom)
    import logging
    with caplog.at_level(logging.WARNING):
        r = _run(gate_env)

    assert r.get("skipped_symbol_daily_loss") is None
    assert _hit_downstream(r)
    assert "单标的日亏损门查询失败" in caplog.text


# ── 5. 总闸旁路 (回滚路径) ──
def test_config_flag_disables_gate(gate_env, monkeypatch):
    monkeypatch.setattr(
        pb_cfg_mod, "get_playbook_config",
        lambda: _StubPlaybookConfig({"symbol_daily_loss_gate_enabled": False}),
    )
    monkeypatch.setattr(
        sdl_mod, "is_symbol_day_loss_exceeded",
        lambda symbol, **kw: pytest.fail("总闸已关, 不应查询日亏损尺"),
    )
    r = _run(gate_env)
    assert _hit_downstream(r)
