"""premarket_invalidation.evaluate_rules 单测 (2026-08-09).

枚举校准 (2026-08-09 DB 实查): playbook_instructions.direction = BUY/SELL_SHORT/SKIP;
"SHORT" 仅存在于 trade_records。05b50d8f/af15897f 的"SHORT 化"系误改, 本文件恢复
正确枚举并加归一化回归断言。
"""
from app.playbook.premarket_invalidation import evaluate_rules


def test_short_chase_rule_A_canonical_enum():
    # 指令表实际枚举 SELL_SHORT (DB: 122 行) — 必须命中
    assert evaluate_rules("SELL_SHORT", 3.5) == ["A_short_chase"]
    assert evaluate_rules("SELL_SHORT", 3.0) == []      # 边界不含
    assert evaluate_rules("SELL_SHORT", -4.0) == []     # 顺向不打标


def test_short_chase_rule_A_alias_normalized():
    # trade_records 口径 SHORT 经归一化后同样命中 (防枚举漂移)
    assert evaluate_rules("SHORT", 3.5) == ["A_short_chase"]
    assert evaluate_rules("sell_short", 3.5) == ["A_short_chase"]  # 大小写不敏感


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


# ── 调度钩子放置回归 (2026-08-10) ─────────────────────────────
# 缺陷: run_check 曾挂在 PRE_MARKET 分支, 但评估窗 09:35-09:59 ET 在开盘
# (09:30) 之后, 此时 phase=TRADING — 该分支永远进不去, shadow 上线后零样本。
# 修复: 钩子挪到 TRADING 分支; 以下两条防再挂错。


def test_invalidation_window_lies_in_trading_phase(monkeypatch):
    """09:35-09:59 ET 评估窗内 get_market_phase() 必须是 TRADING (挂 PRE_MARKET 永不触发)。"""
    import app.core.scheduler_triggers as st

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            # 2026-08-11 周二 09:45 ET (窗内, 非节假日)
            from datetime import datetime as _dt
            return tz.localize(_dt(2026, 8, 11, 9, 45)) if tz else _dt(2026, 8, 11, 9, 45)

    monkeypatch.setattr(st, "datetime", _FakeDatetime)
    assert st.get_market_phase() is st.MarketPhase.TRADING


def test_hook_placed_in_trading_branch():
    """scheduler 源码守护: _pm_inv 调用须位于 TRADING 分支 (TRADING 标记之后、PRE_MARKET elif 之前)。"""
    from pathlib import Path
    src = Path("app/core/scheduler_core.py").read_text(encoding="utf-8")
    i_trading = src.index("if phase == MarketPhase.TRADING:")
    i_premarket = src.index("elif phase == MarketPhase.PRE_MARKET:")
    i_hook = src.index("_pm_inv(self)")
    assert i_trading < i_hook < i_premarket, (
        "premarket_invalidation 钩子必须挂在 TRADING 分支 (评估窗 09:35-09:59 ET 在开盘后)"
    )
