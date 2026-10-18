"""[ADR-046 2026-10-14] UE brief 借券快照段 — _render_borrow_snapshot 契约测试。

病灶背景 (DEB-20261014 三线分析实证): ADR-044 规则10 要求"材料无借券数据 →
borrow_data_basis=UNKNOWN 且 borrow_feasible=false", 但 borrow_rate_series.jsonl
从未渲染进 brief ⇒ 10-02 起 UE 空头候选 11/11 全标 UNKNOWN/false ⇒ RFC-117
源头过滤全杀, 空头供给归零。修复 = 把已采集台账渲染进 brief (单一变量:
只补材料, prompt 规则文本与过滤器语义都不动)。

本测试只覆盖纯函数 _render_borrow_snapshot (rates 注入, 不触网/不触 LLM);
brief 整段接线由 test_brief_builder_wires_borrow_section 静态守卫 +
实盘探针 (health 行 "借券快照:N") 验证 — fixture 全绿 ≠ 接线活。
"""
import inspect
import os
import sys
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.llm_universe_scan import (  # noqa: E402
    BRIEF_TEMPLATE_VERSION,
    _build_clue_brief,
    _render_borrow_snapshot,
)

RATES = {
    "US.TMUS": {"rate_pct": 3.0, "rate_asof": "2026-10-13", "shortable": True,
                "pool": 168481.0, "age_days": 1, "stale": False,
                "origin": "borrow_rate_series_jsonl"},
    "US.ASTH": {"rate_pct": 1.94, "rate_asof": "2026-09-11", "shortable": True,
                "pool": 35200.0, "age_days": 33, "stale": True,
                "origin": "borrow_rate_series_jsonl"},
    "US.XYZ": {"rate_pct": None, "rate_asof": "2026-10-14", "shortable": False,
               "pool": None, "age_days": 0, "stale": False,
               "origin": "borrow_rate_series_jsonl"},
}
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "unified_config.yaml")


def test_renders_covered_symbols_with_borrow_fields():
    facts = _render_borrow_snapshot(["TMUS", "ASTH", "XYZ", "NOTINPOOL"], rates=RATES)
    texts = [f["text"] for f in facts]
    assert len(facts) == 3  # 只渲染快照覆盖到的; 未覆盖标的由节标题统一声明
    assert any(t.startswith("[借券] US.TMUS shortable=yes pool=168,481 fee=3.0% "
                             "(as of 2026-10-13)") for t in texts)
    # stale 标注诚实透传 (陈旧费率比编造诚实, 但不粉饰)
    asth = next(t for t in texts if "ASTH" in t)
    assert "shortable=yes" in asth and "数据陈旧慎用" in asth
    # 不可借的照实渲染 no (过滤器该杀的仍杀 — RFC-117 语义不变)
    xyz = next(t for t in texts if "XYZ" in t)
    assert "shortable=no" in xyz


def test_facts_registered_with_symbols_and_published_at():
    facts = _render_borrow_snapshot(["tmus"], rates=RATES)  # 小写/裸 ticker 归一
    assert len(facts) == 1
    f = facts[0]
    assert f["source"] == "borrow_snapshot"
    assert f["symbols"] == ["TMUS"]
    assert f["published_at"] == "2026-10-13"  # 数据时点 (非渲染日), 时效锚诚实
    assert f["event_key"] == "borrow:TMUS"


def test_empty_inputs_return_empty():
    assert _render_borrow_snapshot([], rates=RATES) == []
    assert _render_borrow_snapshot(["TMUS"], rates={}) == []


def test_fail_open_on_loader_error(monkeypatch):
    """现场加载失败 → [] (段落缺失 = 修复前行为, 不阻断扫描)。"""
    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("borrow loader down")

    monkeypatch.setitem(sys.modules, "app.playbook.planned_executability", _Boom())
    assert _render_borrow_snapshot(["TMUS"], rates=None) == []


def test_brief_builder_wires_borrow_section():
    """静态接线守卫: 借券段必须挂在 _build_clue_brief 且带 config 开关。

    (活体接线 = 实盘 universe_scan.log health 行 "借券快照:N" + scan jsonl
    borrow_data_basis=MATERIAL 出现 — 见 RFC-136 附录验证计划。)
    """
    src = inspect.getsource(_build_clue_brief)
    assert "borrow_snapshot_brief" in src          # config kill-switch 在位
    assert "_render_borrow_snapshot(_named)" in src  # 消费 prescreen 具名集合
    assert "ue_brief_v7" == BRIEF_TEMPLATE_VERSION   # 版本戳已升 (攒样切分键)
    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    assert config["universe_expansion"]["borrow_snapshot_brief"] is True
