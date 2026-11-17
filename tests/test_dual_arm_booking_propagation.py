"""C13/C14 后半: dual_arm 标记传播到 trade_records 簿记层契约 (DEB-20261116).

钉住四件:
  ① helper 兼容 metadata(dict)/metadata_json(str) 两形态, metadata 优先;
  ② 缺失/损坏/非 dict ⇒ None (sim 行零变化);
  ③ 簿记 metadata_json 落 dual_arm 键 (接线在位);
  ④ 簿记级集成见 tests/playbook/test_exit_compensation.py
     ::test_dual_arm_mark_propagates_to_booking_metadata。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook.playbook_service import extract_dual_arm_mark  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

_MARK = {"target_env": "real", "source_instruction_id": 99,
         "decision_id": "DA-X", "mode": "strategy_probe", "s1_arm_enabled": True}


def test_extract_from_dict_metadata():
    assert extract_dual_arm_mark({"metadata": {"dual_arm": _MARK}}) == _MARK


def test_extract_from_str_metadata_json():
    import json
    assert extract_dual_arm_mark({"metadata_json": json.dumps({"dual_arm": _MARK})}) == _MARK


def test_extract_metadata_key_precedes_metadata_json():
    import json
    other = {**_MARK, "mode": "engineering_probe"}
    inst = {"metadata": {"dual_arm": _MARK},
            "metadata_json": json.dumps({"dual_arm": other})}
    assert extract_dual_arm_mark(inst) == _MARK


def test_extract_missing_or_invalid_returns_none():
    import json
    assert extract_dual_arm_mark({}) is None
    assert extract_dual_arm_mark({"metadata_json": ""}) is None
    assert extract_dual_arm_mark({"metadata_json": "{bad json"}) is None
    assert extract_dual_arm_mark({"metadata_json": json.dumps({"dual_arm": "not-a-dict"})}) is None
    assert extract_dual_arm_mark({"metadata_json": json.dumps({"other": 1})}) is None
    assert extract_dual_arm_mark({"metadata": "not-a-dict"}) is None
    assert extract_dual_arm_mark({"metadata": None, "metadata_json": None}) is None


def test_wiring_booking_metadata_carries_dual_arm():
    src = (ROOT / "app/playbook/playbook_service.py").read_text(encoding="utf-8")
    assert "_dual_arm_mark = extract_dual_arm_mark(inst)" in src
    assert '"dual_arm": _dual_arm_mark' in src