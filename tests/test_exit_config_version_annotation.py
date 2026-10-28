"""exit_config_version: 标注类键不得进入退出参数指纹 (2026-10-28, DEB-20261028 §15.8).

背景: E1 往 playbook_horizon.yaml 加了 `bucket_schema_version` / `deprecated_unreachable`
两个**纯标注**键, 而 `get_exit_config_version()` 哈希整本 YAML ⇒ 版本号前进一格
(ca4487172a6f → 0590b8359fd6), 实测后果是 scripts/okr_status.py 的 versioned_stats
按当前版本过滤, 30 天窗口样本 98 笔直接归零。

本测试钉三件事:
  ① 纯标注键的增减不改变指纹 (否则每加一个标签就造一次假断代);
  ② 任何真实退出参数变化仍然改变指纹 (R-4b 整文件哈希的覆盖不能被削弱);
  ③ 现役 YAML 的指纹 == 该批之前的字面量 (断点已回收; 故意改退出参数时,
     必须同步改这一行并在 DEB 留痕 —— 这是唯一的"版本基线"钉子)。
"""
import copy

import pytest
import yaml

from app.playbook.config import PlaybookConfig, _strip_annotation_keys

CONFIG_PATH = "config/playbook_horizon.yaml"
# 2026-10-28 之前 (E1 标注键落地前) 的退出参数指纹
BASELINE_VERSION = "exit-ca4487172a6f"


def _version_for(horizon_config):
    cfg = PlaybookConfig()
    cfg._horizon_config = horizon_config
    cfg._exit_config_version = None
    return cfg.get_exit_config_version()


@pytest.fixture(scope="module")
def live_horizon_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_live_config_matches_pre_annotation_baseline(live_horizon_config):
    assert _version_for(live_horizon_config) == BASELINE_VERSION


def test_annotation_keys_do_not_change_fingerprint(live_horizon_config):
    mutated = copy.deepcopy(live_horizon_config)
    mutated["short_horizon"]["bucket_schema_version"] = "short_v9_probe"
    mutated["long_horizon"]["bucket_schema_version"] = "long_v9_probe"
    mutated["short_horizon"]["vol_buckets"]["low"]["deprecated_unreachable"] = False
    assert _version_for(mutated) == BASELINE_VERSION


def test_annotation_keys_are_stripped_recursively(live_horizon_config):
    stripped = _strip_annotation_keys(live_horizon_config)
    assert "bucket_schema_version" not in stripped["short_horizon"]
    assert "deprecated_unreachable" not in stripped["short_horizon"]["vol_buckets"]["low"]
    # 真实参数必须原样保留
    assert stripped["short_horizon"]["vol_buckets"]["high"]["sl_ceil_pct"] == pytest.approx(5.0)


def test_real_exit_parameter_change_still_moves_fingerprint(live_horizon_config):
    mutated = copy.deepcopy(live_horizon_config)
    mutated["short_horizon"]["vol_buckets"]["high"]["sl_ceil_pct"] = 5.5
    assert _version_for(mutated) != BASELINE_VERSION
    mutated2 = copy.deepcopy(live_horizon_config)
    mutated2["short_horizon"]["overnight_gap_stop_pct"] = 3.5
    assert _version_for(mutated2) != BASELINE_VERSION