"""RFC-218 §19 P2: shadow_registry._count_samples 目录型 data_path 计数契约.

病灶 (§18.4 确认): ext_blacklist_guard_shadow 注册的 data_path 是目录 (按日 JSON 命中台账),
而 _count_samples 旧版只支持 .json/.jsonl 文件 ⇒ 目录永远落到末尾 return None ⇒ 自然命中
永不计数, current_n 恒 None, 首命中无法驱动待裁。本版按日聚合含 "rows" dict 的命中行数。

钉死:
  - 目录型: 跨日聚合 rows 行数; 坏 JSON 跳过; 空目录/无 rows 文件/目录不存在 → None (不伪装 0);
  - 文件型分支逐字不变 (回归);
  - 只影响目录型条目 (当前仅 ext_blacklist_guard_shadow)。
"""
from __future__ import annotations

import json
from dataclasses import replace

from app.playbook import shadow_registry as registry


def _ext_item():
    return next(i for i in registry.REGISTRY if i.id == "ext_blacklist_guard_shadow")


def _mkdir(tmp_path, item):
    d = tmp_path / item.data_path
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_ext_item_data_path_is_directory():
    """注册项确为目录型 (无 .json/.jsonl 后缀) — 旧版恒 None 的根因."""
    item = _ext_item()
    assert item.data_path == "data/shadow/ext_blacklist"
    assert not item.data_path.endswith((".json", ".jsonl"))


def test_dir_count_sums_rows_across_days(tmp_path):
    item = _ext_item()
    d = _mkdir(tmp_path, item)
    (d / "2026-11-25.json").write_text(json.dumps(
        {"rows": {"premarket:US.QCOM": {}, "afterhours:US.LCID": {}}}))
    (d / "2026-11-26.json").write_text(json.dumps(
        {"rows": {"premarket:US.TME": {}}}))
    assert registry._count_samples(replace(item), tmp_path) == 3


def test_dir_count_missing_dir_returns_none(tmp_path):
    """目录不存在 (无命中=通过态) → None, 不伪装 0."""
    assert registry._count_samples(replace(_ext_item()), tmp_path) is None


def test_dir_count_empty_dir_returns_none(tmp_path):
    item = _ext_item()
    _mkdir(tmp_path, item)
    assert registry._count_samples(replace(item), tmp_path) is None


def test_dir_count_no_rows_files_returns_none(tmp_path):
    """目录有 JSON 但无 rows dict (非命中台账形态) → None."""
    item = _ext_item()
    d = _mkdir(tmp_path, item)
    (d / "2026-11-25.json").write_text(json.dumps({"et_date": "2026-11-25"}))
    assert registry._count_samples(replace(item), tmp_path) is None


def test_dir_count_bad_json_skipped(tmp_path):
    item = _ext_item()
    d = _mkdir(tmp_path, item)
    (d / "bad.json").write_text("{not json")
    (d / "2026-11-25.json").write_text(json.dumps(
        {"rows": {"premarket:US.QCOM": {}}}))
    assert registry._count_samples(replace(item), tmp_path) == 1


def test_dir_count_empty_rows_file_returns_zero(tmp_path):
    """有 rows dict 但空 (文件存在, 0 命中) → 0 (区别于「无文件」的 None)."""
    item = _ext_item()
    d = _mkdir(tmp_path, item)
    (d / "2026-11-25.json").write_text(json.dumps({"rows": {}}))
    assert registry._count_samples(replace(item), tmp_path) == 0


def test_file_type_count_unchanged_regression(tmp_path):
    """回归: 文件型条目计数逐字不变 (目录分支只影响目录型)."""
    item = next(i for i in registry.REGISTRY if i.id == "da_event_context_shadow")
    p = tmp_path / item.data_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fact_count": 96}))
    assert registry._count_samples(replace(item), tmp_path) == 96


# ── RFC-218 §21: 半日市分歧台账 (按日单文档, ticks 形态) ⇒ 计数轴=分歧日数 ──────

def _halfday_item():
    return next(i for i in registry.REGISTRY
                if i.id == "ext_afterhours_halfday_guard_shadow")


def test_halfday_item_data_path_is_directory():
    item = _halfday_item()
    assert item.data_path == "data/shadow/ext_afterhours_halfday"
    assert not item.data_path.endswith((".json", ".jsonl"))


def test_halfday_counts_days_not_ticks(tmp_path):
    """每日 1 文档=1 样本 (分歧日轴): ticks 18+3 跨 2 日 → 2, 不是 21."""
    d = _mkdir(tmp_path, _halfday_item())
    (d / "2026-11-27.json").write_text(json.dumps({"ticks": 18, "mode": "shadow"}))
    (d / "2026-12-24.json").write_text(json.dumps({"ticks": 3, "mode": "shadow"}))
    assert registry._count_samples(replace(_halfday_item()), tmp_path) == 2


def test_halfday_invalid_ticks_not_counted(tmp_path):
    """ticks=0/bool/字符串/缺失 → 不计 (不伪装样本); 全目录非法 → None."""
    d = _mkdir(tmp_path, _halfday_item())
    (d / "a.json").write_text(json.dumps({"ticks": 0}))
    (d / "b.json").write_text(json.dumps({"ticks": True}))
    (d / "c.json").write_text(json.dumps({"ticks": "5"}))
    (d / "d.json").write_text(json.dumps({"mode": "shadow"}))
    assert registry._count_samples(replace(_halfday_item()), tmp_path) is None


def test_halfday_mixed_rows_and_ticks_forms(tmp_path):
    """rows 形态 (黑名单) 与 ticks 形态 (半日市) 并存: 2 行 + 1 分歧日 = 3."""
    d = _mkdir(tmp_path, _halfday_item())
    (d / "2026-11-25.json").write_text(json.dumps(
        {"rows": {"premarket:US.QCOM": {}, "afterhours:US.LCID": {}}}))
    (d / "2026-11-27.json").write_text(json.dumps({"ticks": 9}))
    assert registry._count_samples(replace(_halfday_item()), tmp_path) == 3


def test_halfday_missing_dir_returns_none(tmp_path):
    """无目录=通过态 (半日市每年约 2-4 天) → None, 不伪装 0."""
    assert registry._count_samples(replace(_halfday_item()), tmp_path) is None


def test_halfday_check_mounting_no_warning():
    """dormant_reason + stale_expected + eval_on_demand ⇒ 无产物也不报挂载告警."""
    warnings = registry.check_mounting()
    assert not any("ext_afterhours_halfday" in w for w in warnings)
