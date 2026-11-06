"""P2c 回归: entry_confirm 终态必须留存方向 (DEB-20261106 §P2c)。

背景（2026-11-06 实测）: 80 行弃单里 48 行 `metadata.entry_confirm.direction` 缺失，
`raw.direction` 也被 guardrail 改写成 SKIP ⇒ 结算器只能吃到 31 行，n≥100 的裁决线几乎不可达。

根因: `PlaybookDB.merge_instruction_metadata` 是**顶层浅合并**，patch 里的 `entry_confirm`
整块替换旧对象；ABANDONED/CONFIRMED 的 patch 未带 `direction` ⇒ PENDING 时写入的方向被静默抹掉。

本测试做两件事（都不依赖真实 DB/网络）:
  1. 行为锁: 用假 db 复现"patch 不带 direction ⇒ 方向丢失"，证明这是真回归点（防止有人把
     merge 改成深合并后误以为已修好，或反向把写点删掉）。
  2. 结构锁: AST 扫描 `entry_confirm_gate.py` 里 status 为 ABANDONED / CONFIRMED 的
     `_write_state` 字面量，要求必含 `direction` 与 `original_direction` 两键。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "app" / "playbook" / "entry_confirm_gate.py"


class _FakeDB:
    """最小浅合并语义复刻 (与 PlaybookDB.merge_instruction_metadata 一致: 顶层键覆盖)。"""

    def __init__(self, metadata):
        self.metadata = metadata

    def merge_instruction_metadata(self, _inst_id, patch):
        self.metadata.update(patch)          # 顶层浅合并: entry_confirm 整块被替换
        return True


def test_shallow_merge_loses_direction_when_patch_omits_it():
    db = _FakeDB({"entry_confirm": {"status": "PENDING", "direction": "BUY", "anchor": 100.0}})
    # 旧写法 (不带 direction) —— 方向被抹掉
    db.merge_instruction_metadata(1, {"entry_confirm": {"status": "ABANDONED", "anchor": 100.0}})
    assert "direction" not in db.metadata["entry_confirm"], "浅合并语义未复现"

    # 新写法 (带 direction) —— 方向留存
    db2 = _FakeDB({"entry_confirm": {"status": "PENDING", "direction": "SELL_SHORT", "anchor": 50.0}})
    db2.merge_instruction_metadata(2, {"entry_confirm": {
        "status": "ABANDONED", "anchor": 50.0,
        "direction": "SELL_SHORT", "original_direction": "SELL_SHORT"}})
    assert db2.metadata["entry_confirm"]["direction"] == "SELL_SHORT"
    assert db2.metadata["entry_confirm"]["original_direction"] == "SELL_SHORT"


def _write_state_patch_keys(tree):
    """返回 {status_const: set(dict_keys)} —— 找出 _write_state(...) 里带 status 的字面量。"""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_write_state"):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            keys, status = set(), None
            for k, v in zip(arg.keys, arg.values):
                if not isinstance(k, ast.Constant):
                    continue
                keys.add(k.value)
                if k.value == "status":
                    # ST_ABANDONED 是模块级常量 ⇒ ast.Name; 也兼容 x.ST_xxx 的 Attribute 形态
                    if isinstance(v, ast.Name):
                        status = v.id
                    elif isinstance(v, ast.Attribute):
                        status = v.attr
            if status:
                out.setdefault(status, set()).update(keys)
    return out


def test_terminal_states_persist_direction():
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    patches = _write_state_patch_keys(tree)
    for status in ("ST_ABANDONED", "ST_CONFIRMED"):
        assert status in patches, f"{status} 的 _write_state 调用未找到 (改写了写点?)"
        keys = patches[status]
        assert "direction" in keys, f"{status} patch 缺 direction ⇒ 浅合并会抹掉方向"
        assert "original_direction" in keys, f"{status} patch 缺 original_direction ⇒ 结算器无法归因"