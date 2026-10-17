"""trail_lock_pyramid 注册表计数轴守卫 (2026-10-17 审计).

实锤: 台账 JSONL 逐轮追加 (~12k 行, tick 行占绝大多数), 判据 n 轴却是
"帽内可执行武装事件" (add_ratio>0, 结算器产物 n_executable)。注册表原缺
count_mode ⇒ 默认 lines ⇒ current_n 早早越过 min_n=40, 把逐轮轨迹读成
"样本已达标" 的虚假 READY。修法 = 与结算器同轴: 读 latest.json 的
n_executable (cron `41 6 * * 2-6` 每夜刷新)。
"""
import json
from pathlib import Path

import pytest

from app.playbook import shadow_registry as sr

REPO = Path(__file__).resolve().parents[1]


def _item():
    return next(i for i in sr.REGISTRY if i.id == "trail_lock_pyramid")


def test_registry_count_axis_is_settle_executable():
    item = _item()
    assert item.data_path.endswith("trail_lock_pyramid_latest.json"), (
        "计数源必须是结算器产物; 原始 JSONL 行数 ≠ 判据事件轴"
    )
    assert item.count_mode == "json_key:n_executable", (
        "count_mode 必须与 DEB① §七判据 n 轴同轴 (帽内可执行武装事件), 不得回退 lines"
    )
    assert item.min_n == 40


def test_count_samples_reads_n_executable(tmp_path):
    d = tmp_path / "data" / "shadow"
    d.mkdir(parents=True)
    (d / "trail_lock_pyramid_latest.json").write_text(json.dumps({
        "n_armed_total": 9999,
        "n_settled": 9998,
        "n_executable": 7,
    }), encoding="utf-8")
    assert sr._count_samples(_item(), tmp_path) == 7, (
        "应取结算器 n_executable, 不得取 n_armed_total/n_settled 等旁轴"
    )


def test_raw_jsonl_lines_are_not_counted(tmp_path):
    d = tmp_path / "data" / "shadow"
    d.mkdir(parents=True)
    # 只有原始台账 (1.2 万行 arm+tick), 没有结算产物 ⇒ 不得按行数计
    (d / "trail_lock_pyramid.jsonl").write_text(
        "\n".join('{"t": "tick", "price": 1.0}' for _ in range(12000)) + "\n",
        encoding="utf-8",
    )
    assert sr._count_samples(_item(), tmp_path) is None, (
        "无结算产物时应返回 None (保持原值), 不得 fallback 到 JSONL 行数"
    )


def test_missing_n_executable_returns_none(tmp_path):
    d = tmp_path / "data" / "shadow"
    d.mkdir(parents=True)
    (d / "trail_lock_pyramid_latest.json").write_text(json.dumps({
        "n_armed_total": 87,
        "n_settled": 83,
    }), encoding="utf-8")
    assert sr._count_samples(_item(), tmp_path) is None, (
        "键不存在返回 None (同 gap_chase_gate 教训), 不得静默换轴或伪装断供=0"
    )


def test_real_artifact_matches_settle_output():
    """真实产物在位时: 注册表读数必须等于结算器自己算出的 n_executable。"""
    latest = REPO / "data" / "shadow" / "trail_lock_pyramid_latest.json"
    if not latest.exists():
        pytest.skip("结算产物不在本机 (data/ 不入库的检出)")
    expected = json.loads(latest.read_text(encoding="utf-8")).get("n_executable")
    if not isinstance(expected, int):
        pytest.skip("产物缺 n_executable (旧版结算器)")
    assert sr._count_samples(_item(), REPO) == expected
