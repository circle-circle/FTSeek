"""轨道B B1 单测: 持仓期 thesis 复核 ExitPolicy。

用注入的 llm_call + context 隔离 DB / 真实 LLM, 验证:
- 三种 verdict → 三种 action 映射
- 禁用 / LLM 失败 / LLM 异常 / 无 thesis → NO_ACTION (安全零退化)
- 节流生效
- 安全不变量: 永不产出 CLOSE/REDUCE 之外放松止损的动作
"""
from types import SimpleNamespace

import pytest

from app.strategy import thesis_recheck_policy as trp


def _pos(symbol="AAPL"):
    return SimpleNamespace(
        symbol=symbol,
        trace_id="t-1",
        avg_entry_price=100.0,
        direction="LONG",
        holding_hours=3.0,
    )


def _ctx(**kw):
    base = {
        "symbol": "AAPL",
        "direction": "LONG",
        "thesis": "breakout",
        "invalidation": "below 95",
        "thesis_validity_check": "hold above 20d high",
        "entry_price": 100.0,
        "current_price": 101.0,
        "pnl_pct": 0.01,
        "holding_hours": 3.0,
        "recent_news": "",
    }
    base.update(kw)
    return base


def _llm(verdict, reason="ref: price $101, news x"):
    def _call(_c):
        return {"success": True, "verdict": verdict, "reason": reason, "model": "fake"}
    return _call


@pytest.fixture(autouse=True)
def _clean_throttle():
    trp.reset_throttle()
    yield
    trp.reset_throttle()


def test_thesis_broken_maps_to_close():
    d = trp.recheck_thesis(_pos(), {}, force=True, llm_call=_llm("thesis_broken"), context=_ctx())
    assert d.action == "CLOSE"
    assert d.qty_pct == 1.0
    assert "THESIS_BROKEN" in d.reason


def test_catalyst_played_out_maps_to_reduce():
    d = trp.recheck_thesis(_pos(), {}, force=True, llm_call=_llm("catalyst_played_out"), context=_ctx())
    assert d.action == "REDUCE"
    assert d.qty_pct == 0.5
    assert "CATALYST_PLAYED_OUT" in d.reason


def test_thesis_intact_maps_to_no_action():
    d = trp.recheck_thesis(_pos(), {}, force=True, llm_call=_llm("thesis_intact"), context=_ctx())
    assert d.action == "NO_ACTION"


def test_unknown_verdict_defaults_to_no_action():
    # 安全: 非法枚举也绝不误触发平仓
    d = trp.recheck_thesis(_pos(), {}, force=True, llm_call=_llm("garbage"), context=_ctx())
    assert d.action == "NO_ACTION"


def test_disabled_returns_no_action(monkeypatch):
    monkeypatch.setattr(trp, "is_enabled", lambda _f: False)
    d = trp.recheck_thesis(_pos(), {}, llm_call=_llm("thesis_broken"), context=_ctx())
    assert d.action == "NO_ACTION"
    assert d.reason == "thesis_recheck_disabled"


def test_llm_failure_returns_no_action():
    d = trp.recheck_thesis(
        _pos(), {}, force=True,
        llm_call=lambda _c: {"success": False}, context=_ctx(),
    )
    assert d.action == "NO_ACTION"
    assert d.reason == "thesis_recheck_llm_fail"


def test_llm_exception_returns_no_action():
    def _boom(_c):
        raise RuntimeError("llm down")
    d = trp.recheck_thesis(_pos(), {}, force=True, llm_call=_boom, context=_ctx())
    assert d.action == "NO_ACTION"
    assert d.reason == "thesis_recheck_llm_error"


def test_no_thesis_returns_no_action():
    d = trp.recheck_thesis(
        _pos(), {}, force=True,
        llm_call=_llm("thesis_broken"), context={"thesis": ""},
    )
    assert d.action == "NO_ACTION"
    assert d.reason == "thesis_recheck_no_context"


def test_throttle_blocks_second_call(monkeypatch):
    # 开启 flag, 不 force → 走节流逻辑
    monkeypatch.setattr(trp, "is_enabled", lambda _f: True)
    # 首调命中 (返回 intact, 但会 _mark_recheck)
    d1 = trp.recheck_thesis(_pos(), {}, llm_call=_llm("thesis_intact"), context=_ctx())
    assert d1.action == "NO_ACTION" and d1.reason.startswith("THESIS_INTACT")
    # 立即二调 → 被节流拦下, 不再调用 LLM
    d2 = trp.recheck_thesis(_pos(), {}, llm_call=_llm("thesis_broken"), context=_ctx())
    assert d2.action == "NO_ACTION"
    assert d2.reason == "thesis_recheck_throttled"


def test_policy_class_delegates_to_recheck(monkeypatch):
    monkeypatch.setattr(trp, "is_enabled", lambda _f: False)
    policy = trp.ThesisRecheckExitPolicy()
    d = policy.evaluate(_pos(), {"current_price": 101.0})
    assert d.action == "NO_ACTION"
    assert policy.owner == "thesis_recheck"


def test_only_ever_tightens():
    # 安全不变量: 遍历所有枚举, 产出的 action 只可能是 {NO_ACTION, REDUCE, CLOSE}
    for v in trp.ALLOWED_VERDICTS + ["garbage", ""]:
        d = trp.recheck_thesis(_pos(), {}, force=True, llm_call=_llm(v), context=_ctx())
        assert d.action in {"NO_ACTION", "REDUCE", "CLOSE"}
