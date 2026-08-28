"""反向配置普查门禁回归测试 (2026-08-28, D3 + A类隐形开关收尾)。

背景: config_key_census.py 只查"yaml 写了的键有没有人读" (死键方向); 反方向
"代码读的键有没有人写"无人守 → 行为整个扛在代码字面量默认值上, 配置面/人工审计
/死键普查三面同时看不见。首跑抓出 11 处 (含 exit_policy.use_exit_registry ——
全部持仓用哪套退出引擎挂在代码默认值上), 已在 unified_config.yaml 逐一声明为
与代码默认值逐字节等值 (零行为变更)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "config_key_reverse_census", PROJECT_ROOT / "scripts" / "config_key_reverse_census.py"
)


def _fresh_module():
    """每个测试独立模块实例 (BASE/OUT/BASELINE 是模块级常量, 需按 tmp 目录重绑)。"""
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sandbox(tmp_path):
    """合成仓: BASE/app + BASE/config, 读写点与配置面完全由测试控制。"""
    mod = _fresh_module()
    (tmp_path / "app").mkdir()
    (tmp_path / "config").mkdir()
    mod.BASE = tmp_path
    mod.OUT = tmp_path / "out.json"
    mod.BASELINE = tmp_path / "baseline.json"
    (tmp_path / "config" / "unified_config.yaml").write_text(
        "risk:\n  caps:\n    max_notional: 50000\n", encoding="utf-8")
    return mod


def _write_code(mod, rel, src):
    p = mod.BASE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


# ── 判据本体 ──────────────────────────────────────────────────────────

def test_undeclared_literal_default_is_flagged(sandbox):
    """代码读 get_dotted("a.b", True) 而 yaml 无 a.b → 隐形开关。"""
    _write_code(sandbox, "app/x.py",
                'v = cfg.get_dotted("risk.hidden_switch", True)\n')
    r = sandbox.run()
    paths = [x["path"] for x in r["undeclared_literal"]]
    assert "risk.hidden_switch" in paths


def test_declared_key_is_not_flagged(sandbox):
    """同一路径在 yaml 里有声明 → 不是隐形开关 (旋钮可见)。"""
    (sandbox.BASE / "config" / "unified_config.yaml").write_text(
        "risk:\n  hidden_switch: true\n", encoding="utf-8")
    _write_code(sandbox, "app/x.py", 'v = cfg.get_dotted("risk.hidden_switch", True)\n')
    assert sandbox.run()["undeclared_literal"] == []


def test_non_literal_default_is_transitive_not_undeclared(sandbox):
    """默认值是变量/下标 (回退到别处已声明的可见旋钮) → 归豁免, 不算隐形常数。"""
    _write_code(sandbox, "app/x.py",
                'v = cfg.get_dotted("risk.other", limits["max_per_direction"])\n')
    r = sandbox.run()
    assert [x["path"] for x in r["undeclared_literal"]] == []
    assert "risk.other" in [x["path"] for x in r["transitive_exempt"]]


def test_comment_and_docstring_reads_are_not_counted(sandbox):
    """注释/说明文字里的 get_dotted 不是读点 (否则文档改动会凭空造出告警)。"""
    _write_code(sandbox, "app/x.py",
                '# cfg.get_dotted("risk.in_comment", True)\n'
                'def f():\n'
                '    """docstring: cfg.get_dotted("risk.in_doc", 5)"""\n'
                '    return 1\n')
    assert sandbox.run()["undeclared_literal"] == []


def test_leaf_read_needs_exact_declaration_not_just_parent_section(sandbox):
    """父 section 存在 ≠ 叶子已声明 — 否则 capital.initial 这类隐形常数会被父节点掩盖。"""
    (sandbox.BASE / "config" / "unified_config.yaml").write_text(
        "capital:\n  fixed_amount: 82000\n", encoding="utf-8")
    _write_code(sandbox, "app/x.py", 'v = cfg.get_dotted("capital.initial", 1_000_000.0)\n')
    paths = [x["path"] for x in sandbox.run()["undeclared_literal"]]
    assert "capital.initial" in paths


def test_section_type_read_matched_by_parent_section(sandbox):
    """section 型读点 (默认 {}) 由父 section 声明即可 — 语义是"整节没配就用代码 DEFAULTS"。"""
    _write_code(sandbox, "app/x.py", 'v = cfg.get_dotted("risk.unregistered_block", {})\n')
    (sandbox.BASE / "config" / "unified_config.yaml").write_text(
        "risk:\n  caps:\n    max_notional: 1\n", encoding="utf-8")
    assert sandbox.run()["undeclared_literal"] == []


# ── 棘轮门禁 ──────────────────────────────────────────────────────────

def test_gate_blocks_key_absent_from_baseline(sandbox, monkeypatch):
    _write_code(sandbox, "app/x.py", 'v = cfg.get_dotted("risk.brand_new", 3)\n')
    sandbox.BASELINE.write_text('{"undeclared": []}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["prog", "--gate"])
    assert sandbox.main() == 1


def test_gate_passes_for_frozen_baseline_entry(sandbox, monkeypatch):
    """基线内存量不阻塞 (存量需人工逐步声明, 棘轮只挡新增)。"""
    _write_code(sandbox, "app/x.py", 'v = cfg.get_dotted("risk.brand_new", 3)\n')
    sandbox.BASELINE.write_text('{"undeclared": ["risk.brand_new"]}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["prog", "--gate"])
    assert sandbox.main() == 0


def test_update_baseline_then_gate_passes(sandbox, monkeypatch):
    _write_code(sandbox, "app/x.py", 'v = cfg.get_dotted("risk.brand_new", 3)\n')
    monkeypatch.setattr(sys, "argv", ["prog", "--update-baseline"])
    sandbox.main()
    assert "risk.brand_new" in sandbox._baseline_set()
    monkeypatch.setattr(sys, "argv", ["prog", "--gate"])
    assert sandbox.main() == 0


# ── 真仓不变量 (防止回归到"读点比声明多") ─────────────────────────────

def test_real_repo_has_no_new_invisible_switch():
    """真实仓门禁必须通过: 11 处存量已于 2026-08-28 全部在配置面声明 (等值),
    基线仅剩二级面 1 条 (exit_rules.scale_out.trailing_remainder, 规则本身已停用)。"""
    mod = _fresh_module()
    import json
    real_baseline = json.loads(mod.BASELINE.read_text(encoding="utf-8"))["undeclared"]
    for p in real_baseline:
        assert p == "app/risk/exit_policy.yaml::exit_rules.scale_out.trailing_remainder"
    r = mod.run()
    found = [x["path"] for x in r["undeclared_literal"]]
    assert found == [], f"新增隐形开关, 需在配置面声明或登记理由: {found}"


def test_declared_switches_match_code_defaults():
    """A类声明必须与代码默认值一致 (否则"声明"本身就成了一次静默行为变更)。"""
    import yaml
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "unified_config.yaml")
                         .read_text(encoding="utf-8"))

    def get(dotted):
        node = cfg
        for part in dotted.split("."):
            node = node[part]
        return node

    # (点号路径, 代码里的字面量默认) — 与 app/ 读点逐一对应
    pairs = {
        "capital.initial": 1000000.0,
        "opend.enable_lv3": True,
        "push.send_retries": 1,
        "risk.position_sizing.method": "fixed_risk",
        "strategies.fast_lane.priority": 8,
        "exit_policy.use_exit_registry": True,
        "exit_policy.registry_fallback_on_error": True,
    }
    for path, code_default in pairs.items():
        assert get(path) == code_default, f"{path}: yaml≠代码默认 → 声明即行为变更"
