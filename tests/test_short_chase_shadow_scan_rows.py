"""short_chase_ban_shadow 采样设计修复的契约测试 (2026-09-18)。

背景: 该影子原写者只在 chg>阈值 时落行 ⇒ 命中仅 9 条指令 (配到成交 6 笔), 10/31 死线前
到不了 n≥20; 且无对照臂。修复 = 命中行语义不变 + 新增每日首轮扫描行 (对照组) + 存量补
date/decision_id/over_threshold/scan 字段 + 注册表计数轴改为「命中指令数」。
"""
import json
import re
from pathlib import Path

import pytest

from app.playbook.pipeline import _short_chase_shadow_row
from app.playbook import shadow_registry as sr

REPO = Path(__file__).resolve().parents[1]
PIPELINE_SRC = (REPO / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
TRIGGER_SRC = (REPO / "app" / "playbook" / "pipeline_parts" / "trigger_flow.py").read_text(
    encoding="utf-8")


def test_hit_row_contract():
    """命中行: over_threshold=True, scan=False, 且 date/decision_id 落库 (join 键修复)。"""
    row = _short_chase_shadow_row(
        symbol="US.COIN", chg_pct=5.7543, threshold=3.0, entry_price=177.8,
        current_price=180.06, triggered=True, entry_path="pullback",
        inst_id=2917, decision_id="DA-20260918202538-US.COIN-986f",
        date_str="2026-09-18", ts_str="2026-09-18T09:37:52-04:00")
    assert row["over_threshold"] is True and row["scan"] is False
    assert row["direction"] == "SHORT"
    assert row["chg_pct"] == 5.754 and isinstance(row["chg_pct"], float)
    assert row["date"] == "2026-09-18"
    assert row["decision_id"] == "DA-20260918202538-US.COIN-986f"
    assert row["inst_id"] == 2917 and row["triggered"] is True


def test_scan_row_contract():
    """扫描行 (对照臂): over_threshold=False, scan=True, 仍带 chg_pct 原值。"""
    row = _short_chase_shadow_row(
        symbol="US.ARM", chg_pct=1.25, threshold=3.0, entry_price=248.5,
        current_price=242.775, triggered=False, entry_path="chase",
        inst_id=2697, decision_id="DA-20260915202849-US.ARM-a4aa",
        date_str="2026-09-15", ts_str="2026-09-15T09:38:48-04:00", scan=True)
    assert row["over_threshold"] is False and row["scan"] is True
    assert row["chg_pct"] == 1.25


def test_writer_wires_helper_and_dedup():
    """接线守卫: 写者必须用纯函数构造行 + 扫描行经 (inst_id,ET日) 去重集合。

    防「静默恢复旧行为」(只写过阈值行 = 无对照臂) 或「扫描行逐轮灌水」。
    """
    assert "_scb_scan_logged" in TRIGGER_SRC
    assert "_short_chase_shadow_row(" in TRIGGER_SRC
    body = TRIGGER_SRC.split("SHORT 禁追涨 shadow", 1)[1]
    assert "_scb_scan_logged.add(_scb_key)" in body, "扫描行去重集合未接线"
    assert "_scb_key = (_scb_inst_id, _scb_date)" in body, "去重键必须含 ET 日期"
    # 旧行为「只有在过阈值时才落行」必须已消失
    assert "_chg_now is not None and float(_chg_now) > _scb_pct" not in body


def test_count_axis_is_distinct_hit_instructions(tmp_path):
    """计数轴: 命中行按指令去重 (逐轮写不灌水), 扫描行不计入判据 n。"""
    d = tmp_path / "data"
    d.mkdir()
    fp = d / "short_chase_ban_shadow.jsonl"
    rows = [
        {"inst_id": 1, "over_threshold": True, "scan": False},   # 逐轮写 3 行 = 1 指令
        {"inst_id": 1, "over_threshold": True, "scan": False},
        {"inst_id": 1, "over_threshold": True, "scan": False},
        {"inst_id": 2, "over_threshold": True, "scan": False},
        {"inst_id": 3, "over_threshold": False, "scan": True},   # 对照臂, 不计
        {"inst_id": 4, "over_threshold": False, "scan": True},
    ]
    fp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    item = sr.ShadowItem(id="t", name="t", description="t", status=sr.ShadowStatus.OBSERVING,
                         data_path="data/short_chase_ban_shadow.jsonl",
                         count_mode="distinct_where:over_threshold=True", min_n=20)
    assert sr._count_samples(item, tmp_path) == 2
    # 对照: 旧 lines 口径会把逐轮重复行当样本 (121 行 vs 9 指令 => 虚假达标)
    item2 = sr.ShadowItem(id="t2", name="t2", description="t2", status=sr.ShadowStatus.OBSERVING,
                          data_path="data/short_chase_ban_shadow.jsonl", count_mode="lines",
                          min_n=20)
    assert sr._count_samples(item2, tmp_path) == 6


def test_registry_item_registered_axis_and_no_false_ready():
    """注册表: 该项计数轴 = distinct_where:over_threshold=True, 且不得因扫描行/重复行虚假达标。"""
    item = {s.id: s for s in sr.REGISTRY}["short_chase_ban_shadow"]
    assert item.count_mode == "distinct_where:over_threshold=True"
    assert sr._count_samples(item, REPO) < item.min_n, (
        "命中指令数仍 < min_n ⇒ 不得 READY (若 >=min_n 请人工确认判据已跑)")


def test_backfilled_rows_have_required_fields():
    """存量行字段补齐且值未被改写: 每行要么是命中行 (over_threshold=True ∧ scan=False),
    要么是扫描行对照臂 (over_threshold=False ∧ scan=True)。
    2026-09-23 修: 原断言"全部行 over_threshold=True" 与 9/18 落地的扫描行语义冲突
    (扫描行按设计落 over_threshold=False), 文件里 9/18/9/21 各 1 行扫描行 ⇒ 恒红。
    改为一律按行内自洽判 (命中行被写成 over_threshold=False 或扫描行被当作命中行仍会红),
    断言不放松。"""
    fp = REPO / "data" / "short_chase_ban_shadow.jsonl"
    if not fp.exists():
        pytest.skip("影子数据文件不存在 (测试环境)")
    rows = [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows, "影子文件不应为空"
    for r in rows:
        assert r.get("date"), "date 字段缺失 (join 键修复回归)"
        assert r["over_threshold"] is (not r["scan"]), (
            f"{r.get('symbol')} {r.get('date')} 行内口径自相矛盾: "
            f"over_threshold={r['over_threshold']} scan={r['scan']}")
