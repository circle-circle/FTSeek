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


# B门块锚点 — 必须锚**注释块标记**而非裸 "RFC-068" 字符串。
# 2026-09-26 实测两个同型失效 (由 [RFC-111] 在 docstring 写了一句散文「与 B门硬门
# RFC-068 的数据缺失处置一致」触发, 位置 156575, 早于真 B门块 183432):
#   ① test_knife_gate_wired_before_execution 直接红 (index() 取到散文 ⇒ 顺序断言翻车);
#   ② test_knife_gate_uses_ssot_rule_definition **假绿** — seg 从散文起算跨 31803 字节
#      (几乎整个 pipeline), 任何 evaluate_rules 都会命中 ⇒ 断言零判别力 (死门)。
# 真锚点 `# ── RFC-068` 全文件唯一, seg 只跨 4946 字节。任何人在别处写 "RFC-068" 散文
# 都不会再撞翻守卫; 守卫失效时必须红或假绿被这条注释拦住, 不得静默漂移。
_GATE_ANCHOR = "# ── RFC-068"


def test_gate_anchor_is_unique():
    """锚点自身守护: 若 `# ── RFC-068` 不再唯一 (或被删), 下面两个守卫同时失去意义。"""
    # [RFC-060 phase1] pipeline 重构: B门硬门块迁入 pipeline_parts/auto_execute_flow.py,
    # pipeline.py 不再带此锚; 源指针改指新宿主。
    from pathlib import Path
    src = (Path("app/playbook/pipeline_parts/auto_execute_flow.py")
           .read_text(encoding="utf-8"))
    assert src.count(_GATE_ANCHOR) == 1, (
        f"B门锚点 {_GATE_ANCHOR!r} 出现 {src.count(_GATE_ANCHOR)} 次 (须恰 1) — "
        "锚点漂移会让顺序/SSOT 两个守卫静默失效"
    )


def test_knife_gate_uses_ssot_rule_definition():
    """硬门判定必须复用 evaluate_rules (阈值唯一定义源), 不得在 pipeline 内联 -5.0。"""
    from pathlib import Path
    src = (Path("app/playbook/pipeline_parts/auto_execute_flow.py")
           .read_text(encoding="utf-8"))
    i_gate = src.index(_GATE_ANCHOR)
    i_reprice = src.index("2026-06-29: 触发后重新定价")
    seg = src[i_gate:i_reprice]
    # seg 必须有界 — 无上界时任何无关 evaluate_rules 都能命中 (死门假绿)
    assert 0 < len(seg) < 12000, f"B门 seg 长度 {len(seg)} 超界 ⇒ 锚点漂移, 断言已失去判别力"
    assert "evaluate_rules" in seg, "B门硬门必须复用 evaluate_rules (SSOT), 禁止内联阈值"
    assert '"B_long_knife" in' in seg, "硬门只取 B_long_knife 命中转正"


def test_knife_gate_wired_before_execution():
    """挂载顺序守护: 硬门拦截块必须位于触发判定之后、execute_instruction 之前。"""
    from pathlib import Path
    src = (Path("app/playbook/pipeline_parts/auto_execute_flow.py")
           .read_text(encoding="utf-8"))
    i_trigger = src.index("盘中触发")
    i_gate = src.index(_GATE_ANCHOR)
    i_exec = src.index("result = service.execute_instruction")
    assert i_trigger < i_gate < i_exec, "B门硬门必须挂在触发判定后、下单前"
    # 拦截须记入统一收口审计并计入 skipped (可追溯)
    seg = src[i_gate:i_exec]
    assert "skipped_knife_gate" in seg and "skipped_count += 1" in seg


def test_knife_gate_writes_precise_skip_reason():
    """源级契约 (2026-09-15 用户裁「都做」批②): B门拦截必须精确回写 skip_reason。

    根因: 原实现只写 exec_gate_audit jsonl 就 continue → shadow_tracker 收盘兜底
    盖误导标签"已触发未执行(资金护栏/时段/执行失败)" (三猜测全错), 且
    no_trigger_counterfactual_replay 按 LIKE '已触发未执行%' 分桶把飞刀拦截
    误归"资金护栏"桶。9/14 实锤 BE/SKHY/NOK 共 83 拦全被盖错标签。
    守护点: ① 回写发生在拦截块内 (audit 之前) ② 标签前缀 knife_gate_blocked
    不与 '已触发未执行%' / '未触发%' 桶前缀碰撞 ③ 回写失败不吞拦截动作
    (try/except 包裹, warning 级不静默)。
    """
    from pathlib import Path
    src = (Path("app/playbook/pipeline_parts/auto_execute_flow.py")
           .read_text(encoding="utf-8"))
    i_block = src.index('if _kg_block:')
    i_audit = src.index('action="skipped_knife_gate"', i_block)
    seg = src[i_block:i_audit]
    assert 'update_instruction_tracking' in seg, "B门拦截必须回写 skip_reason (照抄 chase_cap 模式)"
    assert 'knife_gate_blocked(' in seg, "skip_reason 前缀须为 knife_gate_blocked (与兜底标签可区分)"
    assert 'except Exception as _kg_sr_err' in seg, "回写失败须捕获且不吞拦截 (warning 级)"
    # 桶碰撞守护: seg 内 skip_reason 赋值恰一次, 且值为 knife_gate_blocked( 前缀
    # (不以 '已触发未执行'/'未触发' 开头 → 不落 shadow_tracker 兜底桶与 no_trigger 桶;
    #  注释里引用错误标签原文不算赋值, 故用计数而非全文 not-in)
    assert seg.count('"skip_reason"') == 1


def test_knife_gate_config_key_present():
    """配置键须落盘且默认启用 (回滚 = 置 false + 重启)。"""
    from pathlib import Path
    yml = Path("config/unified_config.yaml").read_text(encoding="utf-8")
    assert "premarket_invalidation_hard_gate: true" in yml
