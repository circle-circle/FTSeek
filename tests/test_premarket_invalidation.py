"""premarket_invalidation.evaluate_rules 单测 (2026-08-09; 2026-09-11 改版).

枚举校准 (2026-08-09 DB 实查): playbook_instructions.direction = BUY/SELL_SHORT/SKIP;
"SHORT" 仅存在于 trade_records。05b50d8f/af15897f 的"SHORT 化"系误改, 本文件恢复
正确枚举并加归一化回归断言。

2026-09-11 (DEB-20260911 两轮多模型辩论, 用户裁「按建议执行」):
A_short_chase / C_long_chase 采集退役 (昨收轴追高/追空侧; 理由见
premarket_invalidation.py _RULES 注), _RULES 仅存 B_long_knife。本文件相应
改为退役钉: A/C 对任何输入不得命中 (防有人把规则行加回来而不开新辩论);
B 的 SSOT 判定钉不动 (RFC-068 硬门复用本函数)。
退役回滚 = 另开辩论, 不是改本文件。
"""
from app.playbook.premarket_invalidation import evaluate_rules


def test_retired_rule_A_never_fires():
    # DEB-20260911: A 已退役 — 旧命中输入现在必须零命中 (SELL_SHORT 枚举与 SHORT 别名同判)
    for d in ("SELL_SHORT", "SHORT", "sell_short"):
        assert evaluate_rules(d, 3.5) == [], f"{d} +3.5% 不应命中已退役的 A"
        assert evaluate_rules(d, 99.0) == []


def test_retired_rule_C_never_fires():
    # DEB-20260911: C 已退役 — 旧命中输入现在必须零命中
    assert evaluate_rules("BUY", 3.4) == []
    assert evaluate_rules("BUY", 25.0) == []


def test_long_knife_rule_B():
    assert evaluate_rules("BUY", -5.2) == ["B_long_knife"]
    assert evaluate_rules("BUY", -5.0) == []            # 边界不含


def test_pullback_day_not_flagged():
    # 回调日 (-2%~0) 是赢家模式, 明确不打标
    for chg in (-2.0, -1.0, -0.1, -4.9):
        assert evaluate_rules("BUY", chg) == [], f"chg={chg} 不应命中"


def test_double_impossible_and_unknown_direction():
    # SKIP 方向永不命中; BUY 顺向大涨在 A/C 退役后零命中
    assert evaluate_rules("BUY", 5.0) == []
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


# ── RFC-068: B门硬门 (2026-08-29 用户批准, 接飞刀拒入场) ─────────────
# 影子规则 B 转正: BUY 执行时刻现价/昨收 < -5% 拒入场。挂载在
# run_playbook_auto_execute 的触发判定之后、execute_instruction 之前。
# 判定逻辑复用 evaluate_rules (SSOT), 不另写阈值。


def test_knife_gate_uses_ssot_rule_definition():
    """硬门判定必须复用 evaluate_rules (阈值唯一定义源), 不得在 pipeline 内联 -5.0。"""
    from pathlib import Path
    src = Path("app/playbook/pipeline.py").read_text(encoding="utf-8")
    i_gate = src.index("RFC-068")
    i_reprice = src.index("2026-06-29: 触发后重新定价")
    seg = src[i_gate:i_reprice]
    assert "evaluate_rules" in seg, "B门硬门必须复用 evaluate_rules (SSOT), 禁止内联阈值"
    assert '"B_long_knife" in' in seg, "硬门只取 B_long_knife 命中转正"


def test_knife_gate_wired_before_execution():
    """挂载顺序守护: 硬门拦截块必须位于触发判定之后、execute_instruction 之前。"""
    from pathlib import Path
    src = Path("app/playbook/pipeline.py").read_text(encoding="utf-8")
    i_trigger = src.index("盘中触发")
    i_gate = src.index("RFC-068")
    i_exec = src.index("result = service.execute_instruction")
    assert i_trigger < i_gate < i_exec, "B门硬门必须挂在触发判定后、下单前"
    # 拦截须记入统一收口审计并计入 skipped (可追溯)
    seg = src[i_gate:i_exec]
    assert "skipped_knife_gate" in seg and "skipped_count += 1" in seg


def test_knife_gate_config_key_present():
    """配置键须落盘且默认启用 (回滚 = 置 false + 重启)。"""
    from pathlib import Path
    yml = Path("config/unified_config.yaml").read_text(encoding="utf-8")
    assert "premarket_invalidation_hard_gate: true" in yml
