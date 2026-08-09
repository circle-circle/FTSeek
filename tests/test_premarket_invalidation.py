"""premarket_invalidation.evaluate_rules 单测 (2026-08-09)."""
from app.playbook.premarket_invalidation import evaluate_rules


def test_short_chase_rule_A():
    assert evaluate_rules("SELL_SHORT", 3.5) == ["A_short_chase"]
    assert evaluate_rules("SELL_SHORT", 3.0) == []      # 边界不含
    assert evaluate_rules("SELL_SHORT", -4.0) == []     # SHORT 顺向不打标


def test_long_knife_rule_B():
    assert evaluate_rules("BUY", -5.2) == ["B_long_knife"]
    assert evaluate_rules("BUY", -5.0) == []            # 边界不含


def test_long_chase_rule_C():
    assert evaluate_rules("BUY", 3.4) == ["C_long_chase"]


def test_pullback_day_not_flagged():
    # 回调日 (-2%~0) 是赢家模式, 明确不打标
    for chg in (-2.0, -1.0, -0.1, -4.9):
        assert evaluate_rules("BUY", chg) == [], f"chg={chg} 不应命中"


def test_double_impossible_and_unknown_direction():
    # BUY 不会命中 SHORT 规则; SKIP 方向永不命中
    assert evaluate_rules("BUY", 5.0) == ["C_long_chase"]  # 不叠加 A
    assert evaluate_rules("SKIP", 9.9) == []
