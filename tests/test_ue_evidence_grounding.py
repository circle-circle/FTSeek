"""U8 evidence grounding 单测 — 引证真实性核验 (纯代码, 不打 LLM)."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.llm_universe_scan import (  # noqa: E402
    _evidence_grounded,
    _norm_text,
)

MATERIALS = [
    "8-K NUMD: 7.01 自愿披露(Reg FD)(事件驱动) (filed 2026-08-25T07:51)",
    "伊朗制裁未推升油价反致油价新低, 能源板块轮出",
    "CEO离任8-K触发治理风险溢价, 小市值股流动性差",
    "BABA internal insider cluster buying reported",
]


class TestNormText:
    def test_strips_punctuation(self):
        assert _norm_text("8-K NUMD: filed!") == "8knumdfiled"

    def test_lowercases(self):
        assert _norm_text("BABA Insider") == "babainsider"

    def test_keeps_chinese(self):
        assert "能源板块" in _norm_text("能源板块轮出")


class TestGrounding:
    def test_real_quote_passes(self):
        # 材料中的整句引证 → 通过
        assert _evidence_grounded("伊朗制裁未推升油价反致油价新低", MATERIALS) is True

    def test_real_quote_substring_passes(self):
        assert _evidence_grounded("CEO离任8-K触发治理风险溢价", MATERIALS) is True

    def test_paraphrase_still_matches_bigrams(self):
        # 有少量改写但大量词面重叠 → 比例达标仍算真实引证
        assert _evidence_grounded("伊朗制裁未推升油价反致油价新低, 能源板块资金流出", MATERIALS) is True

    def test_fabricated_quote_fails(self):
        # 完全编造 (与材料零重叠) → 不通过
        assert _evidence_grounded("美联储突然宣布加息75个基点引发熔断", MATERIALS) is False

    def test_empty_materials_fails(self):
        assert _evidence_grounded("任何引证", []) is False

    def test_empty_evidence_fails(self):
        assert _evidence_grounded("", MATERIALS) is False

    def test_short_evidence_uses_containment(self):
        # 短引证退化为整串包含
        assert _evidence_grounded("能源板块", MATERIALS) is True
        assert _evidence_grounded("毫无关系", MATERIALS) is False

    def test_partial_overlap_below_ratio_fails(self):
        # 只有开头几个字重合, 比例远低于阈值 → 不通过
        assert _evidence_grounded(
            "伊朗制裁未推升油价反致油价新低但是随后原油暴涨三倍能源股狂欢",
            MATERIALS, k=6, ratio=0.5) is False


class TestIntegration:
    """_normalize_surfaced 留痕 evidence_grounded (观测模式不改判定)."""

    def test_grounded_flag_on_item(self):
        import scripts.llm_universe_scan as ue
        ue._LAST_BRIEF_MATERIALS = MATERIALS
        raw = [{"symbol": "XOM", "direction": "SHORT", "confidence": 0.7,
                "evidence_ref": "伊朗制裁未推升油价反致油价新低",
                "self_tier": "HARD_EVENT", "thesis": "test"}]
        with mock.patch.object(ue, "_cfg", side_effect=lambda k, d=None: False if "grounding_enforce" in k else d):
            out = ue._normalize_surfaced(raw, with_tier=True)
        assert out[0]["evidence_grounded"] is True
        assert out[0]["self_tier"] == "HARD_EVENT"

    def test_fabricated_ref_observable_no_downgrade(self):
        import scripts.llm_universe_scan as ue
        ue._LAST_BRIEF_MATERIALS = MATERIALS
        raw = [{"symbol": "XYZ", "direction": "LONG", "confidence": 0.9,
                "evidence_ref": "美联储突然宣布加息75个基点引发熔断",
                "self_tier": "HARD_EVENT", "thesis": "test"}]
        with mock.patch.object(ue, "_cfg", side_effect=lambda k, d=None: False if "grounding_enforce" in k else d):
            out = ue._normalize_surfaced(raw, with_tier=True)
        # 观测模式: 留痕为 False 但判定不变 (不误杀)
        assert out[0]["evidence_grounded"] is False
        assert out[0]["self_tier"] == "HARD_EVENT"

    def test_fabricated_ref_enforce_downgrades(self):
        import scripts.llm_universe_scan as ue
        ue._LAST_BRIEF_MATERIALS = MATERIALS
        raw = [{"symbol": "XYZ", "direction": "LONG", "confidence": 0.9,
                "evidence_ref": "美联储突然宣布加息75个基点引发熔断",
                "self_tier": "HARD_EVENT", "thesis": "test"}]
        with mock.patch.object(ue, "_cfg", side_effect=lambda k, d=None: True if "grounding_enforce" in k else d):
            out = ue._normalize_surfaced(raw, with_tier=True)
        # 强制模式: 视同无引证 → Track A 夹逼降级 + conf≤0.60
        assert out[0]["self_tier"] == "THESIS_ONLY"
        assert out[0]["confidence"] <= 0.60
        assert out[0]["clamped"] is True
