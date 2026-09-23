"""RFC-100 量闸按时段分档 — 纯函数与接线契约。

动机 (实测): 04:00-07:59 ET 候选量中位仅 398~2,640 股, 200,000 门槛在该时段 ≈ p88
(只有 12~26% 能过) ⇒ 盘前早段结构性不下单, 而 bmo 财报的盘前跳空正落在这里。
判据纪律: 未配置 ⇒ 行为 byte 级不变; 解析失败 ⇒ 回落 MIN_VOLUME (绝不把门槛降到 0)。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location(
        "ext_vol_bucket", ROOT / "scripts" / "extended_hours_entry_executor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _no_bucket_env(monkeypatch):
    """生产 .env.us 会带上该键 ⇒ 用例必须自钉, 否则断言随 env 漂移。"""
    monkeypatch.delenv("EXT_ENTRY_MIN_VOLUME_BUCKETS", raising=False)
    monkeypatch.delenv("EXT_ENTRY_MIN_VOLUME_BUCKETS_SHADOW", raising=False)


def test_no_config_means_unchanged(mod, monkeypatch):
    monkeypatch.setattr(mod, "MIN_VOLUME_BUCKETS_RAW", "")
    assert mod._volume_buckets() == []
    for h in range(24):
        assert mod._min_volume_now(hour=h) == mod.MIN_VOLUME == 200000


def test_bucket_lookup_live(mod, monkeypatch):
    monkeypatch.setattr(mod, "MIN_VOLUME_BUCKETS_RAW", "4-7:20000,8-9:200000")
    for h in (4, 5, 6, 7):
        assert mod._min_volume_now(hour=h) == 20000
    for h in (8, 9):
        assert mod._min_volume_now(hour=h) == 200000
    for h in (0, 3, 10, 15, 23):
        assert mod._min_volume_now(hour=h) == mod.MIN_VOLUME    # 未覆盖时段 = 原门槛


def test_single_hour_form(mod):
    assert mod._min_volume_for("16:20000", 16) == 20000
    assert mod._min_volume_for("16:20000", 17) == mod.MIN_VOLUME


def test_malformed_entries_are_ignored_fail_safe(mod):
    """非法片段忽略 + 非法阈值**不得**生效为 0 (否则等于删掉量闸)。"""
    raw = "garbage,4-7:abc,99-100:5,-1-3:1000,8-9:200000"
    buckets = mod._volume_buckets(raw)
    assert buckets == [(8, 9, 200000)]
    assert mod._min_volume_for(raw, 4) == mod.MIN_VOLUME
    assert mod._min_volume_for("4-7:0", 4) == mod.MIN_VOLUME      # thr<=0 拒收
    assert mod._min_volume_for("abc", 4) == mod.MIN_VOLUME


def test_bad_hour_falls_back(mod):
    assert mod._min_volume_for("4-7:20000", None) == mod.MIN_VOLUME
    assert mod._min_volume_for("4-7:20000", "x") == mod.MIN_VOLUME


def test_both_passes_share_the_bucket_gate(mod):
    """dry 与 live 两个 pass 必须同门槛 —— 否则干跑漏斗读数与实跑不一致 (观测失真)。"""
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert src.count("_min_vol = _min_volume_now(session)") == 2      # dry + live 各一处
    assert src.count("volume >= MIN_VOLUME") == 0                     # 判定路径已无平门槛
    # 观测侧**有意**保留旧门槛 (只记账不判定): prefilter 影子 passes_current_gate + near-miss 台账。
    assert src.count("vol >= MIN_VOLUME") == 1
    assert "or volume < MIN_VOLUME" in src


def test_shadow_bucket_is_record_only(mod):
    """影子分档只进反事实读数, 不得出现在判定路径上。"""
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    # 3 处 = 常量定义 + 常量注释 + 反事实累加 (都在观测侧; 判定路径不读它)
    assert src.count("MIN_VOLUME_BUCKETS_SHADOW_RAW") == 3
    assert "_min_volume_now(" in src and "MIN_VOLUME_BUCKETS_SHADOW_RAW" not in \
        src.split("def _min_volume_now")[1].split("def ")[0]


def test_counterfactual_accumulates_and_prints(mod):
    """接线: 分支里累加 live/shadow 两个口径, 且打印行独立于闸计数主行。"""
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert src.count('_VOL_BUCKET_CF["live"] += 1') == 2
    assert src.count('_VOL_BUCKET_CF["shadow"] += 1') == 2
    assert '量闸分档[{session}]' in src and src.count("_VOL_BUCKET_CF[\"live\"] = 0") == 1