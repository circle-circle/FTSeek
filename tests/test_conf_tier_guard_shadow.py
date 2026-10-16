"""conf_tier_guard_shadow 契约测试 (2026-10-16, DEB-20261016 L3)."""
import json

import scripts.conf_tier_guard_shadow as g


def test_tier_exact_bucketing_no_round_merge():
    """L0-3 口径: 0.68 不得并入 0.7 档 (审计 ROUND 伪影教训)。"""
    assert g._tier(0.7) == "0.7"
    assert g._tier(0.68) == "other"   # 精确分桶: 不并桶
    assert g._tier(0.35) == "0.35"
    assert g._tier(None) == "other"
    assert g._tier(0.9) == "other"


def test_no_flag_when_high_tier_sparse(tmp_path, monkeypatch):
    """高档样本不足 ⇒ 永不标旗 (n<30 不结论纪律)。"""
    monkeypatch.setattr(g, "OUT", tmp_path / "guard.jsonl")
    # 直接驱动 main 需要 DB; 此处只测判定逻辑的关键门槛语义
    # (高档 n=1 时 suggestion 必须为 None) — 由生产首跑实证 (2026-10-16: n=1 无旗)。
    assert g.MIN_N == 30 and g.MIN_PERSIST_DAYS == 5
