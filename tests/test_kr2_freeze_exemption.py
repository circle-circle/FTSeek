"""KR2 冻结豁免契约钉子 (2026-09-01 复查收口).

红绿纪律: config_freeze_baseline.json 的 exit_versions 是周检告警的抑制通道 —
豁免清单每多一键 = 一类版本分裂从此不再告警。键集一旦变化本测试必须红,
新增豁免被迫改测试 = 强制留痕; 每条豁免必须带裁决说明。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASELINE = Path(__file__).resolve().parents[1] / "data" / "config_freeze_baseline.json"

KNOWN_EXEMPT = {
    "exit-7f5263af3cfd",   # KR2 冻结基线版本 (8/24-8/28 唯一版本)
    "exit-2623be9c8879",   # 45e62f0f 8/29 early_tight_sl 毕业案, 用户已裁决
    "exit-d1120a105711",   # e32d376f 9/1 用户裁"A" ATR 比例紧 SL + 9/2 R-4b 漏键收尾
}


def test_exit_versions_keyset_pinned():
    bl = json.loads(BASELINE.read_text(encoding="utf-8"))
    keys = set(bl.get("exit_versions", {}).keys())
    assert keys == KNOWN_EXEMPT, (
        f"exit_versions 豁免键集变化: 新增 {keys - KNOWN_EXEMPT} / 移除 "
        f"{KNOWN_EXEMPT - keys} — 新豁免必须走用户裁决并同步本钉子+ADR 留痕"
    )


def test_each_exemption_carries_adjudication_note():
    bl = json.loads(BASELINE.read_text(encoding="utf-8"))
    for k, v in bl.get("exit_versions", {}).items():
        assert isinstance(v, str) and len(v.strip()) >= 8, (
            f"豁免 {k} 缺裁决说明 — 抑制 KR2 告警必须留 adjudication 依据"
        )
