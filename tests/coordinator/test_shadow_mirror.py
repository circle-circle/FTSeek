"""
Shadow 1:1 对照单测 (Z 系列, 2026-05-15)

2026-06-19: ShadowMirror (app.evolution.shadow_mirror) 退役, Z1 系列测试随之删除.
保留 Z3 (shadow_compare CLI) 与 Z4 (daily_review 集成) 测试.
"""


# ─── Z3 shadow_compare CLI ──────────────────────────────
def test_z3_compare_module_importable():
    """CLI 可 import (无 syntax error)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sc", "scripts/shadow_compare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "cmd_overview")
    assert hasattr(mod, "cmd_by_symbol")
    assert hasattr(mod, "cmd_variant_diff")
    assert hasattr(mod, "cmd_recent_pairs")


# ─── Z4 daily_review 集成 ────────────────────────────────
def test_z4_daily_review_has_shadow_section():
    src = open("app/review/daily_review.py").read()
    assert "_analyze_shadow_vs_real" in src
    assert "_analyze_pnl_outliers" in src
    assert "_analyze_loss_guard" in src
    assert "shadow_comparison" in src
