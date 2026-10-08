"""RFC-139 L1 契约测试: 判据 schema 完整性 + 来源可溯 + 未收录回落.

纪律: spec 是已冻结预注册判据的机器可读投影 — 改 spec = 改判据 = 重新裁决。
本测试钉住: 每条 spec 必须有 source (出处)、sample_gates (机器可查门)、
与注册表 eval_script 一致 (防两处漂移)。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_registry_items():
    """从注册表 SSOT 读 (id, eval_script) — 防spec与注册表漂移。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from app.playbook.shadow_registry import REGISTRY
    return {s.id: s for s in REGISTRY}


def test_specs_have_required_fields():
    from app.review.criteria_spec import SPECS
    for sid, spec in SPECS.items():
        assert spec.id == sid
        assert spec.source.strip(), f"{sid}: spec 无 source — 不许自造判据"
        assert spec.metric.strip(), f"{sid}: spec 无 metric"
        assert isinstance(spec.sample_gates, dict), f"{sid}: sample_gates 必须可机器查"
        assert spec.predicted_sign in (-1, 0, 1)


def test_specs_eval_script_matches_registry():
    items = _load_registry_items()
    from app.review.criteria_spec import SPECS
    for sid, spec in SPECS.items():
        item = items.get(sid)
        assert item is not None, f"{sid}: spec id 不在注册表 — 不许脱离 SSOT"
        if spec.eval_script:
            assert spec.eval_script == item.eval_script, (
                f"{sid}: spec.eval_script 与注册表漂移 — 两处不一致 = 新的漂移面")


def test_specs_criteria_text_provenance():
    """spec.source 必须指向注册表判据文本的出处 (RFC/DEB/分析文档), 不许空泛。"""
    from app.review.criteria_spec import SPECS
    for sid, spec in SPECS.items():
        s = spec.source.lower()
        assert any(k in s for k in ("rfc", "deb", "docs/analysis", "docs/decisions")), (
            f"{sid}: source 无可溯源出处 ({spec.source})")


def test_get_spec_unknown_returns_none():
    from app.review.criteria_spec import get_spec
    assert get_spec("no_such_shadow_id") is None  # 回落 eval_script-only, 不臆造


def test_eth_gap_disclosed():
    """已知缺口必须如实披露: eth 的 eval_script 是采集器不是结算器。"""
    from app.review.criteria_spec import get_spec
    spec = get_spec("eth_relative_volume_gate")
    assert spec is not None
    assert "采集器" in spec.known_gap or "NO_SETTLER" in spec.known_gap
    assert spec.verdict_artifact is None
