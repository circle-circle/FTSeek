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
