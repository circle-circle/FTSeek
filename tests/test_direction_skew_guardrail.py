"""方向偏斜 guardrail 单测 (2026-09-01, RFC-071).

覆盖点 (按 8/31 实证事故排序):
  1. 砍半产物已废除: 偏斜触发时多数方直接 SKIP (不再 position_size *= 0.5)
  2. 死因如实: skip_reason 含 "方向偏斜超限", 不再被下游误读为降仓
  3. 只砍低 confidence 的多数方 (低 conv 优先), 高置信保留
  4. 偏斜达标后停手 (砍到 ≤ 阈值即止, 不多杀)
  5. 未超阈值零动作 (0.65 不触发, 0.85 触发)
  6. position_size 保留原值供审计 (砍半旧行为是清零/减半)
  7. 与 admission 地板的结构性冲突消除: SKIP 后不再生成 ~$2,950 名义额
"""
import pytest


class _Inst:
    """最小指令桩 (guardrail 只读 direction/confidence/position_size/skip_reason)."""

    def __init__(self, symbol, direction, confidence=70, position_size=10):
        self.symbol = symbol
        self.direction = direction
        self.confidence = confidence
        self.position_size = position_size
        self.skip_reason = ""
        self.risk_reward = 2.0
        self.sector = ""


def _trim():
    from app.playbook.pipeline import _trim_majority_direction
    return _trim_majority_direction


def _guardrails():
    from app.playbook.pipeline import _apply_portfolio_guardrails
    return _apply_portfolio_guardrails


# ── 1. 砍半已废除: 偏斜触发 → SKIP 而非减半 ──
def test_skew_triggers_skip_not_halving():
    trim = _trim()
    # 4 BUY + 0 SHORT = 100% 偏斜 (>80%)
    insts = [
        _Inst("US.AAAA", "BUY", confidence=70, position_size=50),
        _Inst("US.BBBB", "BUY", confidence=65, position_size=38),
        _Inst("US.CCCC", "BUY", confidence=60, position_size=5),
        _Inst("US.DDDD", "BUY", confidence=55, position_size=10),
    ]
    trimmed = trim(insts, "BUY", 0.80)
    assert trimmed >= 1
    skipped = [i for i in insts if i.direction == "SKIP"]
    assert skipped, "偏斜触发必须有信号被降级为 SKIP"
    for i in skipped:
        # 2. 死因如实
        assert "方向偏斜超限" in i.skip_reason
        assert "降仓50" not in i.skip_reason
        # 6. position_size 保留原值 (供审计), 不被砍半
        assert i.position_size > 0


def test_halving_is_gone():
    trim = _trim()
    """旧行为断言: 任何被处理的信号都不应出现 size 减半。"""
    orig_sizes = {}
    insts = [
        _Inst(f"US.S{n}", "BUY", confidence=50 + n, position_size=20 + n)
        for n in range(6)
    ]
    for i in insts:
        orig_sizes[i.symbol] = i.position_size
    trim(insts, "BUY", 0.80)
    for i in insts:
        assert i.position_size == orig_sizes[i.symbol], (
            f"{i.symbol} position_size 被改动 — 砍半逻辑应已废除"
        )


# ── 3. 低 confidence 优先被砍 ──
def test_lowest_confidence_victim_first():
    trim = _trim()
    # 5 BUY / 1 SHORT = 83.3% > 80% → 触发; 砍 1 只 → 4/5 = 80% 达标即止
    insts = [
        _Inst("US.LOW", "BUY", confidence=10, position_size=10),
        _Inst("US.MID1", "BUY", confidence=40, position_size=10),
        _Inst("US.MID2", "BUY", confidence=50, position_size=10),
        _Inst("US.MID3", "BUY", confidence=60, position_size=10),
        _Inst("US.HIGH", "BUY", confidence=99, position_size=10),
        _Inst("US.SHORT1", "SELL_SHORT", confidence=70, position_size=10),
    ]
    trimmed = trim(insts, "BUY", 0.80)
    assert trimmed == 1
    # 最低置信 (US.LOW, conf=10) 必须最先被砍
    assert insts[0].direction == "SKIP"
    # 高置信 (US.HIGH, conf=99) 必然存活
    assert insts[4].direction == "BUY"


# ── 4. 砍到达标即止 ──
def test_stops_at_target_skew():
    trim = _trim()
    # 9 BUY / 1 SHORT = 90% > 80%; 砍 1 只 → 8/9 = 89%… 需砍到 ≤80%
    # 9 BUY / 1 SHORT: 砍 k 只 BUY 后 (9-k)/(10-k) ≤ 0.8 → k ≥ 5 (4/5=0.8)
    insts = [_Inst(f"US.B{n}", "BUY", confidence=50 + n, position_size=10)
             for n in range(9)]
    insts.append(_Inst("US.S0", "SELL_SHORT", confidence=70, position_size=10))
    trimmed = trim(insts, "BUY", 0.80)
    buys_left = sum(1 for i in insts if i.direction == "BUY")
    shorts_left = sum(1 for i in insts if i.direction == "SELL_SHORT")
    total_left = buys_left + shorts_left
    assert total_left > 0
    assert buys_left / total_left <= 0.80 + 1e-9
    assert trimmed == 9 - buys_left


# ── 5. 未超阈值零动作 ──
def test_no_action_below_threshold():
    trim = _trim()
    insts = [
        _Inst("US.AAAA", "BUY", confidence=70, position_size=50),
        _Inst("US.BBBB", "BUY", confidence=70, position_size=50),
        _Inst("US.CCCC", "SELL_SHORT", confidence=70, position_size=50),
    ]  # 2/3 = 67% < 80%
    assert trim(insts, "BUY", 0.80) == 0
    assert all(i.direction in ("BUY", "SELL_SHORT") for i in insts)
    assert all(not i.skip_reason for i in insts)


# ── 7. 端到端: 边界与事故场景 ──
def test_e2e_boundary_80pct_not_triggered():
    """边界: 4 BUY / 1 SHORT = 80% 恰等于阈值 → 不触发 (严格 > 才砍)。"""
    guardrails = _guardrails()
    insts = [
        _Inst("US.BABA", "BUY", confidence=70, position_size=25),
        _Inst("US.BWXT", "BUY", confidence=70, position_size=19),
        _Inst("US.LLY", "BUY", confidence=70, position_size=2),
        _Inst("US.META", "BUY", confidence=70, position_size=5),
        _Inst("US.EIX", "SELL_SHORT", confidence=70, position_size=103),
    ]
    out = guardrails(insts)
    assert all(i.direction in ("BUY", "SELL_SHORT") for i in out), (
        "80% 恰等于阈值不应触发降级"
    )


def test_e2e_all_buy_batch_skips_with_honest_reason(monkeypatch):
    """纯 BUY 批 (8/31 真实形态): 4/4 = 100% > 80% → 砍到 ≤80%。

    [DEB-20261008 F1] 摘闸后默认不再砍单 — 本用例显式打开开关, 钉住
    「开关打开时行为不变」; 关闸零砍单见 test_e2e_gate_off_means_zero_cull。"""
    monkeypatch.setattr(
        "app.playbook.pipeline.portfolio_guardrail_enabled", lambda: True
    )
    guardrails = _guardrails()
    insts = [
        _Inst("US.BABA", "BUY", confidence=70, position_size=50),
        _Inst("US.BWXT", "BUY", confidence=70, position_size=38),
        _Inst("US.LLY", "BUY", confidence=70, position_size=5),
        _Inst("US.META", "BUY", confidence=70, position_size=10),
    ]
    out = guardrails(insts)
    skipped = [i for i in out if i.direction == "SKIP"]
    actionable = [i for i in out if i.direction == "BUY"]
    assert skipped, "100% 偏斜必须触发降级"
    for i in skipped:
        assert "方向偏斜超限" in i.skip_reason
        assert i.position_size > 0  # 原值保留供审计
    # 砍后剩余偏斜 ≤ 80% (或只剩 1 只)
    if len(actionable) > 1:
        assert len(actionable) / len([x for x in out if x.direction in ("BUY", "SELL_SHORT")]) <= 0.80 + 1e-9


def test_e2e_gate_off_means_zero_cull(monkeypatch):
    """[DEB-20261008 F1, 2026-10-08 用户裁决摘闸] 开关关闭 (默认): 100% 纯 BUY 批
    也零砍单 — 方向偏斜不再是 SKIP 的理由 (影子仍由 vision 层照算)。"""
    monkeypatch.setattr(
        "app.playbook.pipeline.portfolio_guardrail_enabled", lambda: False
    )
    guardrails = _guardrails()
    insts = [
        _Inst("US.BABA", "BUY", confidence=70, position_size=50),
        _Inst("US.BWXT", "BUY", confidence=70, position_size=38),
        _Inst("US.LLY", "BUY", confidence=70, position_size=5),
        _Inst("US.META", "BUY", confidence=70, position_size=10),
    ]
    out = guardrails(insts)
    assert all(i.direction == "BUY" for i in out), "摘闸后 100% 偏斜也不再砍单"
    assert all(not i.skip_reason for i in out)
