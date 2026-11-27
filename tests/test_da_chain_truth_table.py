"""RFC-218 §29 Batch A3: DA 合并/gate/adapter 逐变量真值表契约 (record-only).

被测真实函数 (skill 仓): llm_synth._apply_score_band / _merge_debate_results,
signal_gate.evaluate_signal, adapter._compute_confidence. 本测试**钉死当前行为**,
不修改任何规则; 目的是把"设计目标 / 已实现 / 已激活"分离, 供审计与后续单变量裁决.

关键钉死点:
  1. score_band → {HIGH:8, MEDIUM:6, LOW:3} (llm_synth:1736).
  2. MEDIUM 结构性封顶「良」(score 6<7 恒); LOW 恒「差」.
  3. HIGH 不一定「优」: PARTIAL_AGREE / 明确反对 → decision_conf 0.50 → 「中」.
  4. |分差|>3→SKIP **仅在真反向分支** (BUY↔SELL_SHORT); 同向分差不触发 SKIP,
     且同向不覆写分析师 score (丙1, llm_synth:3300+).
  5. 一方 SKIP = 合法弃权, 保持有观点方向, confidence 不罚.
  6. **独立性判据分歧 (新发现, 钉死待裁)**: merge 用 _self_debate 判独立,
     gate/adapter 用 _fallback. T2/T3 换家族独立挑战者 (fallback_independent)
     AGREE 同向 → merge 中文 confidence 升档, 但 gate/adapter 不 +0.20
     → HIGH 落「中」而非「优」. 本测试钉死该现状, 修复须另案影子量化 + 裁决.
  7. gate vs adapter 弃权差异①: gate 按 score/10 抬升, adapter 保持 0.50.

I/O 加载器 (strategy_memory / claim_ic / regime) 中性化 —— 只喂 pattern/quality_score,
不喂 decision_conf/tier; _regime 注入避免盘中网络拉取. skill 不可用即 skip.
"""
import importlib.util
import sys

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)
try:
    import llm_synth as L
    import signal_gate as G
    import adapter as A
    _HAVE = True
except Exception:
    _HAVE = False

pytestmark = pytest.mark.skipif(not _HAVE, reason="skill scripts unavailable")

_BAND = {"HIGH": 8.0, "MEDIUM": 6.0, "LOW": 3.0}
_INDEP = {
    "primary": dict(_challenger_fallback=False, _challenger_self_debate=False,
                    _challenger_tier="primary"),
    "fallback_independent": dict(_challenger_fallback=True, _challenger_self_debate=False,
                                 _challenger_tier="fallback_independent"),
    "self_debate": dict(_challenger_fallback=True, _challenger_self_debate=True,
                        _challenger_tier="self_debate"),
}


@pytest.fixture(autouse=True)
def _neutral_io(monkeypatch):
    if not _HAVE:
        yield
        return
    monkeypatch.setattr(G.strategy_memory, "get_strategy_hint", lambda **k: {})
    monkeypatch.setattr(G, "_load_claim_ic_for_gate", lambda: {})
    monkeypatch.setattr(G, "_load_regime_for_gate", lambda: None)
    yield


def _run(a_dir, a_band, c_dir, c_band, verdict, indep):
    analyst = {"direction": a_dir, "score_band": a_band, "confidence": "中", "score": 7.0}
    L._apply_score_band(analyst)
    chall = {"my_direction": c_dir, "my_score_band": c_band, "verdict": verdict,
             "agreed": verdict == "AGREE", "my_score": 7.0,
             "_challenger_model_used": "test-model", **_INDEP[indep]}
    merged = L._merge_debate_results(dict(analyst), dict(chall))
    md, ms = merged.get("direction"), merged.get("score")
    scan = {"BUY": "bullish", "SELL_SHORT": "bearish"}.get(md, "")
    analysis = {"symbol": "US.__TT__", "action": md, "score": ms, "_llm_output": merged,
                "_scanner_direction": scan, "_regime": "range",
                "_ftseek": {"voting_result": {"entry_price": 100, "stop_loss": 95,
                                              "take_profit": 110}}}
    gate = G.evaluate_signal(analysis)
    ac = A._compute_confidence(analysis)
    return merged, gate, ac


def test_score_band_map():
    assert L._SCORE_BAND_MAP == _BAND


@pytest.mark.parametrize("band", ["HIGH", "MEDIUM", "LOW"])
def test_apply_score_band(band):
    r = {"direction": "BUY", "score_band": band, "score": 7.0}
    L._apply_score_band(r)
    assert r["score"] == _BAND[band]
    assert r["score_legacy"] == 7.0


@pytest.mark.parametrize("a_dir", ["BUY", "SELL_SHORT"])
@pytest.mark.parametrize("c_band", ["HIGH", "MEDIUM", "LOW"])
@pytest.mark.parametrize("verdict", ["AGREE", "PARTIAL_AGREE", "DISAGREE"])
def test_medium_never_top_tier(a_dir, c_band, verdict):
    _, gate, _ = _run(a_dir, "MEDIUM", a_dir, c_band, verdict, "primary")
    assert gate["tier"] != "优"
    assert gate["tier"] in ("良", "中", "差")


@pytest.mark.parametrize("a_dir", ["BUY", "SELL_SHORT"])
def test_low_always_bottom(a_dir):
    _, gate, _ = _run(a_dir, "LOW", a_dir, "HIGH", "AGREE", "primary")
    assert gate["tier"] == "差"
    assert gate["decision"] == "BLOCK"


def test_high_partial_primary_is_mid():
    _, gate, _ = _run("BUY", "HIGH", "BUY", "HIGH", "PARTIAL_AGREE", "primary")
    assert gate["decision_conf"] == 0.50
    assert gate["tier"] == "中"


def test_high_agree_primary_is_top():
    _, gate, ac = _run("BUY", "HIGH", "BUY", "HIGH", "AGREE", "primary")
    assert gate["decision_conf"] == 0.70
    assert gate["tier"] == "优"
    assert ac == 0.70


@pytest.mark.parametrize("a_dir", ["BUY", "SELL_SHORT"])
@pytest.mark.parametrize("c_band", ["HIGH", "MEDIUM", "LOW"])
def test_same_dir_no_skip_no_overwrite(a_dir, c_band):
    merged, _, _ = _run(a_dir, "HIGH", a_dir, c_band, "AGREE", "primary")
    assert merged["direction"] == a_dir           # 同向永不因分差改 SKIP
    assert merged["score"] == 8.0                 # 丙1: 不覆写分析师 score


@pytest.mark.parametrize("a_band,c_band,expect_skip", [
    ("HIGH", "LOW", True),    # |8-3|=5 >3
    ("LOW", "HIGH", True),
    ("HIGH", "MEDIUM", False),  # |8-6|=2 <=3
    ("MEDIUM", "LOW", False),   # |6-3|=3 <=3 (严格 >)
])
def test_true_reversal_diff_rule(a_band, c_band, expect_skip):
    merged, _, _ = _run("BUY", a_band, "SELL_SHORT", c_band, "DISAGREE", "primary")
    if expect_skip:
        assert merged["direction"] == "SKIP"
    else:
        assert merged["direction"] == "BUY"       # 保持原方向, 降 confidence


def test_one_side_skip_keeps_view_no_penalty():
    merged, gate, _ = _run("BUY", "HIGH", "SKIP", "LOW", "AGREE", "primary")
    assert merged["direction"] == "BUY"
    assert merged.get("_challenger_abstained") is True


def test_independence_divergence_fallback_independent():
    """新发现钉死: fallback_independent AGREE 同向 → merge 升档但 gate/adapter 平.

    这是 _fallback (gate/adapter) vs _self_debate (merge) 判据不一致的现状.
    修复须另案影子量化 + 用户裁决; 本测试只钉死现状, 不主张其正确.
    """
    m_pr, g_pr, a_pr = _run("BUY", "HIGH", "BUY", "HIGH", "AGREE", "primary")
    m_fi, g_fi, a_fi = _run("BUY", "HIGH", "BUY", "HIGH", "AGREE", "fallback_independent")
    # merge 层: 两者中文 confidence 都升档 (独立判据用 _self_debate)
    assert m_pr["confidence"] == "中高"
    assert m_fi["confidence"] == "中高"
    # gate/adapter 层: primary +0.20 → 优; fallback_independent 被 _fallback 拦 → 中
    assert g_pr["decision_conf"] == 0.70 and g_pr["tier"] == "优"
    assert g_fi["decision_conf"] == 0.50 and g_fi["tier"] == "中"
    assert a_pr == 0.70
    assert a_fi == 0.50


def test_self_debate_no_merge_bump():
    m_sd, _, _ = _run("BUY", "HIGH", "BUY", "HIGH", "AGREE", "self_debate")
    assert m_sd["confidence"] == "中"             # 同模型自辩不升档


def test_gate_vs_adapter_abstain_divergence():
    """差异①: 合法弃权 gate 按 score/10 抬升, adapter 保持 0.50."""
    _, gate, ac = _run("BUY", "HIGH", "SKIP", "LOW", "AGREE", "primary")
    assert gate["decision_conf"] == 0.80          # max(0.50, 8/10)
    assert ac == 0.50                             # adapter 弃权不抬升


def test_explicit_disagree_skip_no_lift():
    """B2: 明确 DISAGREE+SKIP 不按 score 抬升 (保持 0.50)."""
    _, gate, _ = _run("BUY", "HIGH", "SKIP", "LOW", "DISAGREE", "primary")
    assert gate["decision_conf"] == 0.50
