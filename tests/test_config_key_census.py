"""config_key_census 分类规则测试 (2026-08-22, 哨兵双向化改于 2026-08-28)

钉死普查口径防静默漂移 — 自验证锚点: 豁免注册表 (LEAF_EXEMPT 人工核定死键 +
EXTERNAL_CONSUMED 仓外真读开关) 派生的哨兵, 在真实语料上必须"键仍在且非
referenced"。两个方向都算漂移: 被判 referenced=规则坏了; 键整个消失=名单与配置
脱节 (原 KNOWN_DEAD 四键 8/28 清理后正是后者, 哨兵静默常绿, 故改派生+双向)。
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


def test_sentinel_missing_is_a_gate_violation():
    """哨兵必须双向: 键从 yaml 消失也要响 (2026-08-28 修前是静默常绿).

    旧行为只判"键在且被判 referenced", 哨兵键被删后永不再命中 → 自验证形同
    虚设。当天 4 个 KNOWN_DEAD 哨兵被清理, 恰好落进这个盲区。
    """
    fake = {
        "files": {},
        "self_check": {
            "self_check_dead_expected": [],
            "external_switch_expected": [],
            "unexpected_referenced": {},
            "sentinel_missing": ["unified_config.yaml:ghost_key"],
        },
    }
    vios = _MOD.gate_violations(fake)
    assert any(k == "unified_config.yaml:ghost_key" for _, k, _ in vios), (
        "哨兵键消失未被门禁拦住 → 注册表与配置可无限脱节")


def test_flipped_sentinel_is_a_gate_violation():
    fake = {
        "files": {},
        "self_check": {
            "self_check_dead_expected": [],
            "external_switch_expected": [],
            "unexpected_referenced": {"unified_config.yaml:login_password": "referenced"},
            "sentinel_missing": [],
        },
    }
    vios = _MOD.gate_violations(fake)
    assert vios and "口径漂移" in vios[0][2]


def test_sentinels_derive_from_registries():
    """哨兵名单派生自注册表, 不再手写 (手写名单会随清理静默失效)."""
    assert _MOD.SELF_CHECK_DEAD == sorted(_MOD.LEAF_EXEMPT)
    assert _MOD.SELF_CHECK_EXTERNAL == sorted(_MOD.EXTERNAL_CONSUMED)


def test_external_consumed_keys_are_exempt_and_still_present():
    """仓外开关: 语料判 orphan 但 Hermes 真读 → 必须豁免, 且键必须仍在配置里.

    键消失 = 仓外消费方 (不在本仓 review 视野内) 的总开关被误删。
    """
    import yaml
    cfg = yaml.safe_load(
        (_MOD.BASE / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
    paths = []
    _MOD.flatten(cfg, "", paths)
    present = {l for _, l in paths}
    for key, reason in _MOD.EXTERNAL_CONSUMED.items():
        fname, leaf = key.split(":", 1)
        assert fname == "unified_config.yaml"
        ok, why = _MOD.exemption_for(fname, leaf)
        assert ok and "仓外" in why, f"{key} 未走仓外豁免 → 会被当死键清掉"
        assert len(reason) >= 20, f"{key} 缺实证路径证据"
        assert leaf in present, f"{key} 已从 yaml 消失, 仓外消费方只能读到缺省值"


def test_extra_yaml_outside_config_dir_are_swept():
    """仓外活体配置必须进普查 (2026-08-28 补裸奔面).

    ConfigWriter 有 ("yaml_file": "exit_policy") 条目直接写 app/risk/exit_policy.yaml
    (出场参数 SSOT), 而普查原先只 glob config/*.yaml → 写盘通道有、死键门禁无。
    登记进 EXTRA_YAML 的文件若消失或未被扫到, 本测试即红 (防盲区静默重开)。
    """
    result = _MOD.run_census()
    files = result["files"]
    for p in _MOD.EXTRA_YAML:
        assert p.exists(), f"EXTRA_YAML 登记的 {p.name} 已不存在 — 名单需随之清理"
        assert p.name in files, f"{p.name} 未进普查 (sweep 漏掉仓外文件)"
    ep = files.get("app/risk/exit_policy.yaml") or files.get("exit_policy.yaml")
    assert ep and ep.get("leaves") and len(ep["leaves"]) > 20, \
        "exit_policy.yaml 叶子集异常小 → 解析失败被当成'无死键'"


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
