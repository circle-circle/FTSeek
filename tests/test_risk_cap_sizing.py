"""DEB-20261031-risk-cap (P1) 契约测试。

钉住四件事:
  ① 帽生效时 每笔名义风险 (|entry−SL|×股数) ≤ 帽 (含整股取整的 1 股例外);
  ② risk_cap_usd<=0 时**逐位退回**旧行为 (0 = 关闭, 不是"默认拦");
  ③ 止损越宽 股数越少 (帽与止损宽度自适应, 不是固定名义);
  ④ 归因可读: 帽绑住时 size_basis == "risk_cap_usd", 否则仍是旧的两支。
"""
import pytest

from app.playbook.instruction_generator import compute_position_size

EQUITY = 82_000.0
RISK_PCT = 0.5          # 声明风险预算 = $410/笔
MAX_POS_PCT = 0.06      # 名义帽 = $4,920/笔


def risk_dollars(size, entry, sl):
    return abs(entry - sl) * size


def test_cap_binds_and_respects_budget():
    """典型 5% 止损: 旧行为 32 股 = $240 风险; 帽后必须 ≤ $80。"""
    old = compute_position_size(EQUITY, RISK_PCT, 150.0, 142.5, MAX_POS_PCT)
    new = compute_position_size(EQUITY, RISK_PCT, 150.0, 142.5, MAX_POS_PCT, risk_cap_usd=80.0)
    assert old == 32 and risk_dollars(old, 150.0, 142.5) == pytest.approx(240.0)
    assert new == 10
    assert risk_dollars(new, 150.0, 142.5) <= 80.0
    # 帽必须比名义帽更紧, 否则本测试没验到东西
    assert new < old


def test_wide_stop_shrinks_size_not_dollars():
    """8.5% 止损: 股数更少, 风险仍 ≤ 帽 (帽跟止损宽度走)。"""
    size = compute_position_size(EQUITY, RISK_PCT, 150.0, 137.25, MAX_POS_PCT, risk_cap_usd=80.0)
    assert size == 6
    assert risk_dollars(size, 150.0, 137.25) <= 80.0


def test_zero_or_negative_cap_is_noop():
    """0/负数 = 关闭 ⇒ 逐位等于旧行为 (回归红线)。"""
    base = compute_position_size(EQUITY, RISK_PCT, 150.0, 142.5, MAX_POS_PCT)
    assert compute_position_size(EQUITY, RISK_PCT, 150.0, 142.5, MAX_POS_PCT, risk_cap_usd=0.0) == base
    assert compute_position_size(EQUITY, RISK_PCT, 150.0, 142.5, MAX_POS_PCT, risk_cap_usd=-5) == base


def test_notional_cap_still_binds_when_tighter():
    """紧止损 (1%) 时名义帽才是约束 ⇒ 帽不得放大仓位。"""
    entry, sl = 150.0, 148.5
    size = compute_position_size(EQUITY, RISK_PCT, entry, sl, MAX_POS_PCT, risk_cap_usd=400.0)
    assert size * entry <= EQUITY * MAX_POS_PCT + entry   # 名义帽 (整股取整)
    assert size == int(EQUITY * MAX_POS_PCT / entry)


def test_one_share_floor_is_kept_and_documented():
    """高价股 1 股就超帽 ⇒ 仍给 1 股 (整股下限), 由调用方告警, 不静默返回 0。"""
    size = compute_position_size(EQUITY, RISK_PCT, 2000.0, 1900.0, MAX_POS_PCT, risk_cap_usd=80.0)
    assert size == 1
    assert risk_dollars(size, 2000.0, 1900.0) > 80.0      # 已知残差, 由调用方 warning 披露


def test_invalid_price_returns_zero():
    assert compute_position_size(EQUITY, RISK_PCT, 0.0, 142.5, MAX_POS_PCT, risk_cap_usd=80.0) == 0
    assert compute_position_size(EQUITY, RISK_PCT, 150.0, 150.0, MAX_POS_PCT, risk_cap_usd=80.0) == 0


def test_estimate_size_attributes_risk_cap():
    """影子侧归因: 帽绑住 ⇒ size_basis=risk_cap_usd (否则库里的 risk_amount 会被误读)。"""
    from app.playbook.planned_executability import estimate_size
    ctx = {"equity": EQUITY, "risk_pct": RISK_PCT, "max_position_pct": MAX_POS_PCT,
           "risk_cap_usd_per_trade": 80.0, "regime": None, "water_mult": 1.0,
           "confidence_sizing_enabled": False}
    out = estimate_size(entry=150.0, stop_loss=142.5, direction="BUY", ctx=ctx)
    assert out["state"] == "computed"
    assert out["size_basis"] == "risk_cap_usd"
    assert out["risk_cap_bound"] is True
    assert out["risk_amount_derived"] <= 80.0
    # 关闭帽 ⇒ 回到旧归因, 且不得残留 risk_cap_bound=True
    ctx_off = dict(ctx, risk_cap_usd_per_trade=0.0)
    off = estimate_size(entry=150.0, stop_loss=142.5, direction="BUY", ctx=ctx_off)
    assert off["risk_cap_bound"] is False
    assert off["size_basis"] in ("max_position_pct_cap", "risk_budget")
    assert off["size"] > out["size"]