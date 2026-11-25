"""LIVE 工程探针 admission 豁免契约 (2026-10-06, 用户裁「1只股真实试运行」).

钉住三条边界, 任何一条被放宽都必须显式改本文件:
  ① 豁免只对带 dual_arm/target_env=real/mode=engineering_probe 标记的镜像行生效;
  ② SIM 行 (无该标记) 的 admission 判定**逐值不变** — 不得放宽任何生产门;
  ③ 豁免面精确 = 只剥 NOTIONAL_TOO_SMALL/FEE_BPS_TOO_HIGH; 安全性/质量门 (SL_STOP_TOO_WIDE /
     PRICE_TOO_LOW / SHARES_ABOVE_FEE_KNEE / FEE_INEFFICIENT / LOW_CONFIDENCE)
     即使与 NOTIONAL 同时命中, 仍必须保留并让探针 REJECT。

立项根因见 app/playbook/admission.py 模块头 _PROBE_EXEMPT_REASONS 注释。
"""

import pytest

import app.playbook.admission as A
from app.playbook.admission import apply, evaluate


def _cfg_with(**over):
    """生产 config 实值 + 覆盖。必须在 monkeypatch get_admission_config **之前**取值 —
    否则覆盖后的 lambda 会回调本函数 → 无限递归 (实测 RecursionError)."""
    base = _PROD_CFG.value
    return {**base, **over}


class _Holder:
    """fixture → helper 传值 (helper 定义在 fixture 之前, 故用可变容器)."""
    value = {}


_PROD_CFG = _Holder()

# 真函数引用 (fixture 会用 monkeypatch 顶掉 A.get_admission_config): 供"生产现状"用例
# 绕过 fixture 覆盖直接读 config。
_REAL_GET_ADMISSION_CONFIG = A.get_admission_config


@pytest.fixture(autouse=True)
def _prod_cfg(monkeypatch, tmp_path):
    """用生产 config 实值, 但**显式打开费用门**后注入。

    2026-11-12 (核查修复, 5 例长期恒红): 生产自 2026-11-06 起 `fee_gate_enforce=false`
    (用户裁「费用门拦截关闭」), 于是 NOTIONAL_TOO_SMALL / FEE_BPS_TOO_HIGH 只进
    `metrics.would_reject`, 不再 reject ⇒ 本文件的"探针豁免"场景**无拦可豁**,
    `exempted_reasons` 恒空。红的是场景, 不是豁免机制。

    本文件守的是"豁免机制的边界"(只剥两项经济门/不吃安全门/伪标记不生效), 故在测试侧
    把费用门打开以复现可拦场景; **生产当前"费用门已关"这一事实**由
    `test_production_fee_gate_is_currently_unenforced` 单独钉住 —— 一旦有人把开关改回
    true, 该用例会红, 提醒把这里的覆盖删掉、让测试直接跑生产口径。
    """
    from app.risk import capital_safety
    capital = capital_safety.CapitalSafetySystem(db_path=str(tmp_path / "capital.sqlite"))
    monkeypatch.setattr(capital_safety, "get_capital_safety_system", lambda: capital)
    A.reset_cache()
    base = dict(_REAL_GET_ADMISSION_CONFIG(refresh=True))
    enforced = {**base, "enforce": True, "fee_gate_enforce": True}
    _PROD_CFG.value = enforced
    monkeypatch.setattr(A, "get_admission_config", lambda refresh=False: enforced)
    yield
    A.reset_cache()


def _inst(symbol="US.GEO", ep=33.15, sl=31.49, tp=35.64, qty=1,
          probe=False, direction="BUY"):
    inst = dict(date="2026-10-06", symbol=symbol, direction=direction, entry_price=ep,
                stop_loss=sl, take_profit=tp, position_size=qty, confidence=45)
    if probe:
        inst["metadata"] = {"dual_arm": {
            "target_env": "real", "source_instruction_id": 99,
            "decision_id": "DA-TEST", "mode": "engineering_probe"}}
    return inst


def test_exemption_only_applies_to_live_probe_marker():
    """① 有探针标记 ⇒ 豁免; 无标记 ⇒ 恒拦。"""
    prb = evaluate(_inst(qty=1, probe=True))
    sim = evaluate(_inst(qty=1, probe=False))
    assert prb["verdict"] == "PASS"
    assert prb["exempted_reasons"] and all(
        r.startswith(("NOTIONAL_TOO_SMALL", "FEE_BPS_TOO_HIGH")) for r in prb["exempted_reasons"])
    assert sim["verdict"] == "REJECT"
    assert any(r.startswith("NOTIONAL_TOO_SMALL") for r in sim["reasons"])
    assert sim["exempted_reasons"] == []


def test_exemption_requires_one_share_buy_probe_shape():
    """防标记污染/未来镜像 bug: 非 BUY 或非 1 股不得吃探针豁免。"""
    short = evaluate(_inst(qty=1, probe=True, direction="SELL_SHORT"))
    oversized = evaluate(_inst(qty=2, probe=True))

    assert short["verdict"] == "REJECT"
    assert any(r.startswith("NOTIONAL_TOO_SMALL") for r in short["reasons"])
    assert short["exempted_reasons"] == []

    assert oversized["verdict"] == "REJECT"
    assert any(r.startswith("NOTIONAL_TOO_SMALL") for r in oversized["reasons"])
    assert oversized["exempted_reasons"] == []


def test_sim_arm_verdict_is_unchanged_by_exemption():
    """② SIM 臂判定逐值不变 — 豁免不得泄漏进生产门。"""
    # 原生 sizing (策略真会下的量) 与 1 股 (SIM 侧永不过门) 两种形态都验
    for qty in (1, 181):
        r = evaluate(_inst(qty=qty, probe=False))
        assert "exempted_reasons" in r
        assert r["exempted_reasons"] == []
        assert "probe_exemption" not in r["metrics"]


def test_exemption_surface_is_exactly_economic_gates():
    """③ 豁免面精确 = 只剥两项经济性判据, 其余判据全保留。"""
    safety = {"SL_STOP_TOO_WIDE", "PRICE_TOO_LOW", "SHARES_ABOVE_FEE_KNEE",
              "FEE_INEFFICIENT", "LOW_CONFIDENCE"}
    assert set(A._PROBE_EXEMPT_REASONS) == {"NOTIONAL_TOO_SMALL", "FEE_BPS_TOO_HIGH"}
    assert not (set(A._PROBE_EXEMPT_REASONS) & safety), "豁免面不得含安全性/质量门"


def test_probe_still_rejected_when_safety_gate_fires(monkeypatch):
    """③ 端到端: 强制 SL 门开启, 探针命中 SL_STOP_TOO_WIDE 仍须 REJECT。"""
    monkeypatch.setattr(A, "get_admission_config",
                        lambda refresh=False: _cfg_with(max_sl_pct=5.0, enforce=True))
    wide = evaluate(_inst(sl=20.0, qty=1, probe=True))   # SL 距 39.7% > 5%
    assert wide["verdict"] == "REJECT"
    assert any(r.startswith("SL_STOP_TOO_WIDE") for r in wide["reasons"])
    # 经济性那条被豁免了, 但安全性那条保住 ⇒ 探针仍被拒
    assert all(not r.startswith("NOTIONAL_TOO_SMALL") for r in wide["reasons"])


def test_probe_price_floor_still_enforced(monkeypatch):
    """③ 价格地板不豁免: 低价股探针仍 REJECT (commission 撞 0.3%×amt 上限)."""
    monkeypatch.setattr(A, "get_admission_config",
                        lambda refresh=False: _cfg_with(min_entry_price=5.0, enforce=True))
    cheap = evaluate(_inst(ep=3.20, sl=3.04, tp=3.52, qty=1, probe=True))
    assert cheap["verdict"] == "REJECT"
    assert any(r.startswith("PRICE_TOO_LOW") for r in cheap["reasons"])


def test_apply_does_not_rewrite_probe_direction():
    """④ apply() 不再把探针指令改写成 SKIP (2026-10-05 污染根因)."""
    inst = _inst(qty=1, probe=True)
    out, res = apply(inst)
    assert res["verdict"] == "PASS"
    assert out["direction"] == "BUY"
    assert out.get("skip_reason") is None
    # 豁免事实必须留痕 (可审计, 不静默)
    assert out["raw_instruction"]["admission"]["metrics"]["probe_exemption"]
    assert out["raw_instruction"]["admission"]["metrics"]["probe_exemption"]["fee_bps"]


def test_apply_still_rewrites_sim_direction():
    """④ SIM 侧 1 股小额指令仍被改写 SKIP (生产行为不变)."""
    inst = _inst(qty=1, probe=False)
    out, res = apply(inst)
    assert res["verdict"] == "REJECT"
    assert out["direction"] == "SKIP"
    assert out["skip_reason"].startswith("admission: NOTIONAL_TOO_SMALL")
    assert out["raw_instruction"]["admission"]["original_direction"] == "BUY"


def test_probe_marker_requires_both_target_env_and_mode():
    """⑤ 标记判定严格: 缺 target_env 或 mode 不符 ⇒ 不豁免 (防伪标记绕过)."""
    base = dict(date="2026-10-06", symbol="US.GEO", direction="BUY", entry_price=33.15,
                stop_loss=31.49, take_profit=35.64, position_size=1, confidence=45)
    variants = {
        "无 metadata": {},
        "metadata 是坏 JSON 串": {"metadata": "{not json"},
        "metadata 非 dict": {"metadata": "plain-string"},
        "dual_arm 非 dict": {"metadata": {"dual_arm": "x"}},
        "只有 mode 无 target_env": {"metadata": {"dual_arm": {"mode": "engineering_probe"}}},
        "只有 target_env 无 mode": {"metadata": {"dual_arm": {"target_env": "real"}}},
        "target_env=sim": {"metadata": {"dual_arm": {
            "target_env": "sim", "mode": "engineering_probe"}}},
        "mode 是别的": {"metadata": {"dual_arm": {
            "target_env": "real", "mode": "graduated"}}},
    }
    for label, extra in variants.items():
        r = evaluate({**base, **extra})
        assert r["verdict"] == "REJECT", f"{label}: 竟被豁免 = 伪标记可绕过"
        assert r["exempted_reasons"] == [], label
    # 正例: 两个字段齐全才豁免
    ok = evaluate({**base, "metadata": {"dual_arm": {
        "target_env": "real", "mode": "engineering_probe"}}})
    assert ok["verdict"] == "PASS"


def test_production_fee_gate_is_currently_unenforced():
    """生产现状留痕 (2026-11-12): 费用门拦截**当前是关的** (2026-11-06 用户裁)。

    这条不是判据, 是防"测试被改绿灯"的哨兵: 上面 5 个豁免用例之所以需要测试侧打开
    `fee_gate_enforce`, 正是因为生产把它关了, 那一刻起探针豁免在生产里**无事可做**。
    若哪天有人把开关改回 true, 本用例会红 —— 正确响应是把 fixture 里的覆盖删掉,
    让用例直接跑生产口径 (而不是把断言删掉)。
    """
    cfg = _REAL_GET_ADMISSION_CONFIG(refresh=True)
    assert cfg.get("fee_gate_enforce") is False, (
        "生产费用门拦截已重新打开 — 请删除本文件 fixture 的 fee_gate_enforce 覆盖, "
        "让豁免用例直接跑生产口径 (而不是放宽断言)")
    assert cfg.get("enforce") is True, "admission 总闸被关: 所有判据都不生效, 需人工确认"
