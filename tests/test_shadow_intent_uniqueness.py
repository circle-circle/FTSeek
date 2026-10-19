"""意图唯一性守卫测试 (2026-09-11, DEB-20260911 P0-3①, 用户裁「按建议执行」).

红绿纪律 (对齐 test_shadow_mount_sentinel.py 的行为级钉法):
  ① 真实注册表必须自洽 (check_intent_uniqueness() == []) — 补登记当天的现状钉;
  ② 病灶原型逐条注入必须机械报警: 同意图未互指 / 悬空互引 / 单向互引 / id 重复;
  ③ 合法形态必须静默: 互指完整 / 指向终态留痕项不要求回指 / 无 intent 不归组;
  ④ 守卫自身崩了必须显性产出 detector_error (元环规则5: 「零违反」≠「守卫崩了」)。
另钉: short_chase_ban_shadow 补登记条目的预注册判据字段 (死线/污染期/config_key 真键),
以及周检 (ftseek_weekly_checkpoint.py) 已接入本守卫 — 防「函数建了周检没读」的断头传感。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.playbook.shadow_registry as sr  # noqa: E402


def _item(iid, intent="", cross_ref=(), status=sr.ShadowStatus.OBSERVING):
    return sr.ShadowItem(id=iid, name=iid, status=status, description="",
                         data_path="data/ftseek_us.db (x)",
                         intent=intent, cross_ref=cross_ref)


# ── ① 真实注册表现状钉 ──

def test_real_registry_self_consistent():
    assert sr.check_intent_uniqueness() == []


def test_real_registry_ids_unique():
    ids = [s.id for s in sr.REGISTRY]
    assert len(ids) == len(set(ids)), f"注册表 id 重复: {[i for i in ids if ids.count(i) > 1]}"


def test_ban_shadow_entry_pins():
    """补登记条目字段钉: 预注册判据/死线/污染期/config_key 真键 (嵌套层级写错=死键,
    unified_config.yaml:1098 注释实锤 top-level 不生效)."""
    b = [s for s in sr.REGISTRY if s.id == "short_chase_ban_shadow"]
    assert len(b) == 1, "ban-shadow 条目必须恰好一条 (2026-09-11 并发编辑曾产出双条目)"
    b = b[0]
    assert b.config_key == "playbook.execution_parameters.short_chase_ban_shadow_enabled"
    assert b.min_n == 20
    assert "2026-10-31" in b.graduation_criteria, "预注册死线丢失"
    assert "70%" in b.graduation_criteria, "反事实方向一致率判据丢失"
    assert "污染期" in b.notes, "昨收分母分裂污染期标注丢失"
    assert b.eval_on_demand is True, "无独立结算器, 必须按需评估 (挂载哨兵豁免)"
    assert b.intent == "short_chase_ban"
    assert "premarket_invalidation" in b.cross_ref
    assert len(b.verdict_history) == 4
    assert b.verdict_history[0]["verdict"] == "HARD_GATE_REJECTED_OLD_BASELINE"
    assert b.verdict_history[1]["verdict"] == "SSOT_FOR_SHORT_CHASE_INTENT"
    assert b.verdict_history[2]["verdict"] == "BLOCKED_DENOMINATOR_SPLIT"
    assert b.verdict_history[3]["verdict"] == "KILLED_REASONABLENESS"
    assert b.status == sr.ShadowStatus.KILLED
    assert b.collection_retired is True
    # 真停采 = config 键 false, 不是只改注册表账面
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml").read_text())
    assert cfg["playbook"]["execution_parameters"]["short_chase_ban_shadow_enabled"] is False


def test_premarket_entry_retirement_and_crossref():
    p = [s for s in sr.REGISTRY if s.id == "premarket_invalidation"]
    assert len(p) == 1
    p = p[0]
    assert "退役" in p.name and "A/C" in p.name
    assert "RETIRED_NO_PROMOTION" in p.graduation_criteria
    assert p.intent == "long_knife_gate"
    assert "short_chase_ban_shadow" in p.cross_ref


# ── ② 病灶注入必报 ──

def test_same_intent_without_crossref_warned(monkeypatch):
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", intent="chase_ban"), _item("a2", intent="chase_ban")])
    ws = sr.check_intent_uniqueness()
    assert any("意图不唯一" in w and "a1" in w for w in ws)
    assert any("意图不唯一" in w and "a2" in w for w in ws)


def test_same_intent_partial_crossref_warned(monkeypatch):
    """单向: a1 指了 a2, a2 没指回 — ①(同意图未互指) 必须仍对 a2 报."""
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", intent="chase_ban", cross_ref=("a2",)),
                         _item("a2", intent="chase_ban")])
    ws = sr.check_intent_uniqueness()
    assert any("意图不唯一" in w and "[a2]" in w for w in ws)


def test_dangling_crossref_warned(monkeypatch):
    monkeypatch.setattr(sr, "REGISTRY", [_item("a1", cross_ref=("ghost_item",))])
    assert any("悬空互引" in w and "ghost_item" in w for w in sr.check_intent_uniqueness())


def test_one_way_crossref_to_live_warned(monkeypatch):
    """病灶原型: premarket(long_knife_gate) ↔ ban(short_chase_ban) 意图标签不同 ⇒
    ①不触发, 单边引用只能靠③单向互引兜住."""
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", intent="gate_x", cross_ref=("a2",)),
                         _item("a2", intent="gate_y")])
    assert any("单向互引" in w and "a2" in w for w in sr.check_intent_uniqueness())


def test_duplicate_id_warned(monkeypatch):
    monkeypatch.setattr(sr, "REGISTRY", [_item("dup"), _item("dup")])
    assert any("id 重复" in w and "dup" in w for w in sr.check_intent_uniqueness())


def test_detector_error_is_explicit(monkeypatch):
    """元环规则5: 守卫自身异常必须产出 detector_error 行, 不得静默返回 []."""
    class _Bomb:
        @property
        def id(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(sr, "REGISTRY", [_Bomb()])
    ws = sr.check_intent_uniqueness()
    assert len(ws) == 1 and "intent_uniqueness_detector_error" in ws[0]
    assert "boom" in ws[0]


# ── ③ 合法形态必静默 ──

def test_mutual_crossref_silent(monkeypatch):
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", intent="chase_ban", cross_ref=("a2",)),
                         _item("a2", intent="chase_ban", cross_ref=("a1",))])
    assert sr.check_intent_uniqueness() == []


def test_crossref_to_terminal_item_needs_no_backref(monkeypatch):
    """指向 KILLED/SUPERSEDED 留痕项: 悬空检查过 (id 在注册表), 但不要求终态项回指."""
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", cross_ref=("a2",)),
                         _item("a2", status=sr.ShadowStatus.KILLED)])
    assert sr.check_intent_uniqueness() == []


def test_no_intent_not_grouped(monkeypatch):
    """intent 为空的现役项不参与同意图归组 (存量条目默认无 intent, 不得误报风暴)."""
    monkeypatch.setattr(sr, "REGISTRY", [_item("a1"), _item("a2")])
    assert sr.check_intent_uniqueness() == []


def test_terminal_same_intent_not_grouped(monkeypatch):
    """退役项即使 intent 相同也不算现役双影子 — 病灶是「同时裁决」, KILLED 无裁决 ⇒
    ①同意图归组只看现役, 本例不报「意图不唯一」."""
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", intent="chase_ban", cross_ref=("a2",)),
                         _item("a2", intent="chase_ban", cross_ref=("a1",),
                              status=sr.ShadowStatus.KILLED)])
    ws = sr.check_intent_uniqueness()
    assert not any("意图不唯一" in w for w in ws)
    assert ws == []


def test_terminal_source_to_live_target_still_needs_backref(monkeypatch):
    """③的豁免只按**目标**终态判: 退役项指向现役项时, 现役项仍须回指 —
    留痕链必须双向可见 (cross_ref 设计即「含已退役方, 供裁决日对照」)."""
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("a1", intent="chase_ban"),
                         _item("a2", intent="chase_ban", cross_ref=("a1",),
                              status=sr.ShadowStatus.KILLED)])
    assert any("单向互引" in w and "a1" in w for w in sr.check_intent_uniqueness())


# ── ④ 周检接入契约 (防断头传感器: 函数建了没人读) ──

def test_weekly_checkpoint_wired():
    src = (Path(__file__).resolve().parents[1]
           / "ops/hermes/ftseek_weekly_checkpoint.py").read_text(encoding="utf-8")
    assert "check_intent_uniqueness" in src, "周检 import 面缺守卫 — 告警无人读"
    assert "'intents': check_intent_uniqueness()" in src, "周检结果字典缺 intents 键"
    assert 'reg.get("intents", [])' in src, "周检渲染层缺 intents 循环 — 采了不呈报"
