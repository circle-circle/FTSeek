"""P11 入场追价 bps 采集落库契约 (RFC-212 §5.5 / DEB-20261116, record-only).

钉住六件:
  ① 量纲: bps (10000 制, 1% 偏离 = 100bps), 非百分数;
  ② 符号: 正=不利追价 (LONG 成交更高 / SHORT 成交更低), 与 exit_slippage_bps 同约定;
  ③ 锚/价缺失或 ≤0 ⇒ None (判不出不写假值);
  ④ 锚点优先 reprice_from (指令行 entry_price 会被 reprice tracking 改写);
  ⑤ 簿记落 metadata_json 三键 (值/锚价/锚来源);
  ⑥ record-only: S3 未接线 (s1_arm 不得消费本字段)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook.playbook_service import compute_entry_chase_bps  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ①② 量纲与符号
@pytest.mark.parametrize("direction,fill,anchor,expected", [
    ("LONG", 101.0, 100.0, 100.0),     # 追高 1% = +100bps (不利)
    ("LONG", 99.0, 100.0, -100.0),     # 低吸 1% = −100bps (有利)
    ("SHORT", 99.0, 100.0, 100.0),     # 追低 1% = +100bps (不利, 符号翻转)
    ("SHORT", 101.0, 100.0, -100.0),   # 高卖 1% = −100bps (有利)
    ("LONG", 100.05, 100.0, 5.0),      # 舍入到 0.01bps
    ("LONG", 100.0, 100.0, 0.0),       # 零偏离
])
def test_chase_bps_magnitude_and_sign(direction, fill, anchor, expected):
    assert compute_entry_chase_bps(fill, anchor, direction) == expected


def test_chase_sign_convention_matches_exit_slippage():
    """与 exit_slippage_bps 同约定: SHORT = −LONG (同锚同价)。"""
    for fill, anchor in ((101.0, 100.0), (99.0, 100.0), (100.0, 100.0)):
        assert compute_entry_chase_bps(fill, anchor, "SHORT") == \
            -compute_entry_chase_bps(fill, anchor, "LONG")


# ③ 判不出不写假值
@pytest.mark.parametrize("fill,anchor", [
    (0, 100.0), (-1, 100.0), (None, 100.0),
    (100.0, 0), (100.0, -1), (100.0, None),
    ("bad", 100.0), (100.0, "bad"),
])
def test_chase_invalid_inputs_return_none(fill, anchor):
    assert compute_entry_chase_bps(fill, anchor, "LONG") is None


# ④⑤ 接线契约
def test_wiring_anchor_prefers_reprice_from():
    """指令行 entry_price 被 _TRACKING_FIELDS 白名单 reprice 改写 ⇒
    计划锚必须优先 reprice_from (改写前原值)。_cp 是 execute_instruction 局部变量,
    簿记在 inner 层 ⇒ reprice_from 经三层签名穿透 (可选参, 旧调用方不受影响)。"""
    src = (ROOT / "app/playbook/playbook_service.py").read_text(encoding="utf-8")
    assert "if reprice_from is not None:" in src
    assert "_chase_anchor = reprice_from" in src
    assert '_chase_anchor_src = "reprice_from"' in src
    assert src.count("reprice_from=_cp.get(\"reprice_from\")") == 2   # 两个执行调用点
    assert "archetype_route=None, reprice_from=None," in src          # 两层签名
    # 白名单事实前提仍在 (若未来移除 entry_price, 锚点取法须重审)
    db_src = (ROOT / "app/playbook/db.py").read_text(encoding="utf-8")
    tf = db_src.split("_TRACKING_FIELDS = {")[1].split("}")[0]
    assert '"entry_price"' in tf


def test_wiring_metadata_three_keys():
    src = (ROOT / "app/playbook/playbook_service.py").read_text(encoding="utf-8")
    assert '"entry_chase_bps": _entry_chase_bps' in src
    assert '"entry_chase_anchor_price":' in src
    assert '"entry_chase_anchor_source": _chase_anchor_src' in src
    assert "compute_entry_chase_bps(_fill, _chase_anchor, mos_direction)" in src


# ⑥ record-only: S3 未接线 (攒样阶段, 接线=重新裁决)
def test_record_only_s3_not_wired():
    s1_src = (ROOT / "app/playbook/s1_arm.py").read_text(encoding="utf-8")
    # 消费语法不得出现 (docstring 提及属状态留痕, 不算消费)
    assert 'get("entry_chase_bps")' not in s1_src
    assert "['entry_chase_bps']" not in s1_src
    assert '["entry_chase_bps"]' not in s1_src
    assert "S3_SLIPPAGE" not in s1_src.split("def kill_reason")[1].split("def ")[0]