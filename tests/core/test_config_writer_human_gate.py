"""ConfigWriter 人工确认门 + 注释保真写入 (2026-08-28 全链路复查).

两条契约:
1. 无人值守写盘默认拒绝 — 用户裁决"自调器不做, 需要实战人工确认才行"。review_loop 的
   5 处 update_param(approval_level="L1") 此前直接改 unified_config.yaml (审计日志实证
   7/24 曾把 min_confidence 0.7→0.8 静默落盘)。现 L1 一律抛 AutoWriteNotConfirmed,
   仅 L0 (rollback_param / 人工显式) 或人工打开 config_writer_auto_apply 才写。
2. 写入不得再毁注释 — 旧 _atomic_write 整本 yaml.dump, 326 行决策注释一并抹掉。
   现只改值变的那一行, 写前校验, 落不了点就 ConfigWriteRefused (原文件字节不动)。

回滚: git revert 本文件对应的 app/core/config_writer.py 改动即恢复 dump + L1 直写。
"""

import os
import sys

import pytest
import yaml

sys.path.insert(0, str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))))

from app.core import feature_flags as ff  # noqa: E402
from app.core.config_writer import (  # noqa: E402
    AutoWriteNotConfirmed,
    ConfigWriteRefused,
    ConfigWriter,
    _apply_scalar_edit,
    _index_mapping_lines,
    _leaf_diffs,
)

_BASE_YAML = """\
# 顶部决策注释: 为什么有这个文件
risk:
  # 信号质量阈值 (2026-07-24 从 0.7 抬到 0.8, 见 DEB-XXXX)
  signal_quality:
    min_confidence: 0.7   # 人工裁决前不得自动改
    min_score: 60
  position:
    max_per_symbol: 0.08

blank_after: true

prompt_block: |
  # 这不是配置键, 是块标量正文
  risk:
    fake_key: 1
"""


class _StubFlags:
    def __init__(self, enabled=False, boom=False):
        self._enabled = enabled
        self._boom = boom

    def is_enabled(self, name):
        if self._boom:
            raise RuntimeError("flags unavailable")
        return self._enabled


@pytest.fixture
def writer(tmp_path, monkeypatch):
    cfg = tmp_path / "unified_config.yaml"
    cfg.write_text(_BASE_YAML, encoding="utf-8")
    w = ConfigWriter(config_path=str(cfg), audit_path=str(tmp_path / "audit" / "config_audit_log.json"))
    monkeypatch.setattr(w, "_trigger_reload", lambda: None)  # 不碰真 ConfigContainer
    # 缺省: 自动写盘未放行
    monkeypatch.setattr(ff, "get_feature_flags", lambda: _StubFlags(enabled=False))
    return w


def _read(writer):
    with open(writer.config_path, "r", encoding="utf-8") as f:
        return f.read()


# ── 1. 人工确认门 ──────────────────────────────────────────────────────────

def test_L1_auto_write_is_refused_and_file_untouched(writer):
    before = _read(writer)
    with pytest.raises(AutoWriteNotConfirmed):
        writer.update_param("MIN_CONFIDENCE", 0.75, reason="auto_fix: high_losing_ratio")
    assert _read(writer) == before, "被拒的写盘仍改了文件"
    assert not os.path.exists(writer.audit_path), "被拒的提案不该进审计日志 (会污染 rollback 历史)"


def test_L1_allowed_when_human_opened_the_switch(writer, monkeypatch):
    monkeypatch.setattr(ff, "get_feature_flags", lambda: _StubFlags(enabled=True))
    rec = writer.update_param("MIN_CONFIDENCE", 0.75, reason="auto_fix: x")
    assert rec.old_value == 0.7 and rec.new_value == 0.75
    assert yaml.safe_load(_read(writer))["risk"]["signal_quality"]["min_confidence"] == 0.75


def test_feature_flag_failure_fails_closed(writer, monkeypatch):
    """开关读不到 = 未放行 (防线不因基础设施抖动自解)."""
    monkeypatch.setattr(ff, "get_feature_flags", lambda: _StubFlags(boom=True))
    with pytest.raises(AutoWriteNotConfirmed):
        writer.update_param("MIN_CONFIDENCE", 0.75, reason="auto_fix: x")


def test_L0_human_path_still_writes(writer):
    """rollback_param 走 L0 — 人工路径不受门影响."""
    writer.update_param("MIN_CONFIDENCE", 0.75, reason="manual", approval_level="L0")
    assert yaml.safe_load(_read(writer))["risk"]["signal_quality"]["min_confidence"] == 0.75


def test_bounds_still_enforced_on_L0(writer):
    with pytest.raises(ValueError):
        writer.update_param("MIN_CONFIDENCE", 0.99, reason="manual", approval_level="L0")


# ── 2. 注释保真 ────────────────────────────────────────────────────────────

def test_write_changes_only_the_target_line(writer):
    before = _read(writer)
    writer.update_param("MIN_CONFIDENCE", 0.75, reason="manual", approval_level="L0")
    after = _read(writer)

    diff = [(b, a) for b, a in zip(before.split("\n"), after.split("\n")) if b != a]
    assert len(diff) == 1, f"预期只改 1 行, 实际 {len(diff)} 行: {diff}"
    assert diff[0] == ("    min_confidence: 0.7   # 人工裁决前不得自动改",
                       "    min_confidence: 0.75   # 人工裁决前不得自动改")


def test_comments_and_blank_lines_survive(writer):
    before = _read(writer)
    writer.update_param("SIGNAL_MIN_SCORE", 65, reason="manual", approval_level="L0")
    after = _read(writer)
    assert after.count("#") == before.count("#"), "行内/整行注释数量变了 = 决策记录被抹"
    assert "# 顶部决策注释: 为什么有这个文件" in after
    assert "blank_after: true" in after  # 空行分隔仍在原处
    assert before.count("\n \n") + before.count("\n\n") == after.count("\n \n") + after.count("\n\n")


def test_block_scalar_body_is_not_treated_as_keys(writer):
    """块标量里的 "risk: / fake_key:" 不能被当成配置键索引/改写."""
    text = _read(writer)
    idx = _index_mapping_lines(text)
    assert "prompt_block" in idx
    assert not any(k.endswith("fake_key") for k in idx), "块标量正文被误索引"
    writer.update_param("MIN_CONFIDENCE", 0.75, reason="manual", approval_level="L0")
    assert "  risk:\n    fake_key: 1" in _read(writer), "块标量正文被动过"


def test_noop_write_does_not_touch_file(writer):
    """auto_fix 反复提同一值时旧实现也会整本重写一次; 现在字节不变."""
    before_bytes = open(writer.config_path, "rb").read()
    before_mtime = os.stat(writer.config_path).st_mtime_ns
    writer._atomic_write(yaml.safe_load(before_bytes.decode("utf-8")))
    assert open(writer.config_path, "rb").read() == before_bytes
    assert os.stat(writer.config_path).st_mtime_ns == before_mtime


# ── 3. fail-closed 落点 ────────────────────────────────────────────────────

def test_structural_change_is_refused(writer):
    cfg = yaml.safe_load(_read(writer))
    cfg["risk"]["signal_quality"]["brand_new_key"] = 1
    before = _read(writer)
    with pytest.raises(ConfigWriteRefused):
        writer._atomic_write(cfg)
    assert _read(writer) == before


def test_scalar_to_mapping_change_is_refused(writer):
    cfg = yaml.safe_load(_read(writer))
    cfg["risk"]["position"]["max_per_symbol"] = {"a": 1}
    with pytest.raises(ConfigWriteRefused):
        writer._atomic_write(cfg)


def test_missing_key_path_is_refused():
    text = "risk:\n  position:\n    max_per_symbol: 0.08\n"
    with pytest.raises(ConfigWriteRefused):
        _apply_scalar_edit(text, "risk.position.does_not_exist", 0.1)


def test_parent_key_cannot_be_overwritten_as_scalar():
    text = "risk:\n  position:\n    max_per_symbol: 0.08\n"
    with pytest.raises(ConfigWriteRefused):
        _apply_scalar_edit(text, "risk.position", 0.1)


def test_leaf_diffs_marks_structure():
    diffs = _leaf_diffs({"a": {"b": 1}}, {"a": {"b": 2}})
    assert diffs == [("a.b", 2)]
    assert _leaf_diffs({"a": 1}, {"a": 1, "b": 2})[0][0] is None


# ── 4. 真文件回归: 外科式写入必须能在 2800 行生产 yaml 上落到点 ────────────

def test_all_param_map_paths_locatable_in_real_config():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.join(repo_root, "config", "unified_config.yaml")
    text = open(path, "r", encoding="utf-8").read()
    parsed = yaml.safe_load(text)
    idx = _index_mapping_lines(text)

    for key, spec in ConfigWriter.PARAM_MAP.items():
        dotted = spec["yaml_path"]
        if spec.get("yaml_file"):
            continue  # 非 unified_config.yaml (exit_policy 等), 另文件
        assert dotted in idx, f"{key}: {dotted} 在真实 yaml 中定位不到 → 该行写会被拒"
        # 行定位到的键, 其解析值必须与按路径取到的值一致 (防同名键误命中)
        line = text.split("\n")[idx[dotted]]
        leaf_value = line.split(":", 1)[1].split("#")[0].strip()
        real = parsed
        for part in dotted.split("."):
            real = real[part]
        assert yaml.safe_load(leaf_value) == real, f"{dotted}: 行值 {leaf_value!r} ≠ 解析值 {real!r}"
        edited = _apply_scalar_edit(text, dotted, real)
        assert yaml.safe_load(edited) == parsed, f"{dotted}: 同值编辑改变了文件语义"
