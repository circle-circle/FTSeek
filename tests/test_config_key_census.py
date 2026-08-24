"""config_key_census 分类规则测试 (2026-08-22)

钉死普查口径防静默漂移 — 自验证锚点: C批人工核定的 4 个死键在真实语料上
必须落 orphan/definition_only (与 scripts/config_key_census.py 的
KNOWN_DEAD 自检同一契约, 但在 CI 里强制).
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SPEC = importlib.util.spec_from_file_location(
    "config_key_census", Path(__file__).resolve().parent.parent / "scripts" / "config_key_census.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_classify_referenced_via_get():
    corpus = [("app/x.py", '    v = cfg.get("vwap_threshold", 0.5)')]
    status, files = _MOD.classify_leaf("vwap_threshold", corpus)
    assert status == "referenced"
    assert files == {"app/x.py"}


def test_classify_definition_only_registry():
    # PlaybookConfig.DEFAULTS 形态: 行首 "k": default 且本行仅一次出现
    corpus = [("app/playbook/config.py", '        "rr_ratio": 1.0,')]
    status, _ = _MOD.classify_leaf("rr_ratio", corpus)
    assert status == "definition_only"


def test_classify_orphan_zero_hits():
    corpus = [("app/x.py", "print('hello')")]
    status, files = _MOD.classify_leaf("nonexistent_key_xyz", corpus)
    assert status == "orphan"
    assert files == set()


def test_snapshot_line_counts_as_reference():
    # 同行两次出现 (定义+取值) → 引用, 防把 get_exit_config_version 快照行误判定义
    corpus = [("app/playbook/config.py",
               '                "cooldown_sl_pct": self.get("cooldown_sl_pct", 4.0),')]
    status, _ = _MOD.classify_leaf("cooldown_sl_pct", corpus)
    assert status == "referenced"


def test_comment_line_skipped():
    corpus = [("app/x.py", "# TODO: wire up vwap_threshold someday")]
    status, _ = _MOD.classify_leaf("vwap_threshold", corpus)
    assert status == "orphan"


def test_word_boundary_no_substring_match():
    # _time_exit_enabled 不构成 time_exit_enabled 的消费 (词边界)
    corpus = [("app/pipeline.py", "    _time_exit_enabled = True")]
    status, _ = _MOD.classify_leaf("time_exit_enabled", corpus)
    assert status == "orphan"


def test_flatten_nested_and_list():
    out = []
    _MOD.flatten({"a": {"b": 1}, "c": [{"d": 2}]}, "", out)
    paths = {p for p, _ in out}
    leaves = {l for _, l in out}
    assert paths == {"a.b", "c[0].d"}
    assert leaves == {"b", "d"}


def test_known_dead_keys_still_non_referenced():
    """真实语料上 C批死键必须保持非 referenced (口径漂移即报警)."""
    corpus = _MOD.load_corpus()
    for leaf in _MOD.KNOWN_DEAD:
        status, _ = _MOD.classify_leaf(leaf, corpus)
        assert status in ("orphan", "definition_only"), (
            f"{leaf} 变成 {status} — 普查口径漂移或死键被复活, 先查清再动"
        )


# ── A3 死键门禁 (2026-08-24): 存量基线冻结, 只堵新增 ─────────────────────── #

def test_gate_no_new_dead_keys():
    """全量普查门禁: 非 referenced 叶子必须 ⊆ 基线 ∪ 豁免注册表.

    新增配置键而不消费 (或不登记) → 本测试红。存量死键以
    tests/fixtures/config_key_census_baseline.json 冻结, 十月解冻后清理时用
    --update-baseline 缩基线 (随清理 commit 一起进 diff)。
    """
    result = _MOD.run_census()
    violations = _MOD.gate_violations(result)
    assert not violations, (
        f"发现 {len(violations)} 个新增未登记死键: "
        + "; ".join(f"{f}:{k}" for f, k, _ in violations[:10])
        + " … 处置: 消费该键, 或在 config_key_census.py 豁免注册表登记实证理由"
    )


def test_gate_baseline_exists_and_shape():
    """基线必须存在且形态正确 ({file: {leaf: status}}), 否则门禁形同虚设."""
    import json
    assert _MOD.BASELINE.exists(), "死键基线缺失 — 门禁无冻结参照, 先跑 --update-baseline"
    snap = json.loads(_MOD.BASELINE.read_text(encoding="utf-8"))
    nr = snap.get("non_referenced")
    assert isinstance(nr, dict) and nr, "基线 non_referenced 为空或形态错误"
    for fname, leaves in nr.items():
        assert isinstance(leaves, dict) and leaves, f"基线 {fname} 形态错误"
        for leaf, status in leaves.items():
            assert status in ("orphan", "definition_only"), (
                f"基线 {fname}:{leaf} 状态非法: {status}")


def test_gate_exempt_entries_require_evidence():
    """豁免注册表纪律: 每项豁免都必须带非空实证理由 (防橡皮图章)."""
    for fname, reason in _MOD.FILE_EXEMPT.items():
        assert isinstance(reason, str) and len(reason) >= 10, (
            f"FILE_EXEMPT[{fname}] 理由缺失/过短")
    for key, reason in _MOD.LEAF_EXEMPT.items():
        assert isinstance(reason, str) and len(reason) >= 6, (
            f"LEAF_EXEMPT[{key}] 理由缺失/过短")
        assert ":" in key, f"LEAF_EXEMPT 键必须是 '文件名:叶子名' 形态: {key}"


def test_gate_sentinels_covered_by_exempt_or_dead():
    """哨兵死键必须同时落在 LEAF_EXEMPT (显式豁免) 且保持非 referenced."""
    for entry in _MOD.SELF_CHECK_DEAD:
        assert entry in _MOD.LEAF_EXEMPT, f"哨兵 {entry} 未落 LEAF_EXEMPT"


def test_gate_iteration_consumed_files_auto_excused():
    """泛型迭代消费文件的非 referenced 叶子必须被 exemption_for 自动豁免."""
    for fname in _MOD.ITERATION_CONSUMED:
        ok, why = _MOD.exemption_for(fname, "任意不存在键xyz")
        assert ok, f"{fname} 属迭代消费文件却未被自动豁免: {why}"
