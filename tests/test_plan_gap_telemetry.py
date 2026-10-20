"""DA 计划一致性遥测: confirmation.price_level vs entry_price 的有符号偏离。

10-19 事故形态: ARM 确认位 238.5 vs 执行锚 232.18 (gap -2.65%), COIN 178 vs 174.14。
本测试只钉纯函数;  warning/raw 字段接线由 instruction_builder 的现有调用面覆盖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook.pipeline_parts.instruction_builder import _confirmation_gap_pct  # noqa: E402


def test_confirmation_gap_is_signed_percent():
    assert _confirmation_gap_pct(100.0, {"price_level": 102.0}) == 2.0
    assert _confirmation_gap_pct(100.0, {"price_level": 98.0}) == -2.0


def test_confirmation_gap_missing_or_invalid_is_none():
    assert _confirmation_gap_pct(0.0, {"price_level": 100.0}) is None
    assert _confirmation_gap_pct(100.0, None) is None
    assert _confirmation_gap_pct(100.0, {"price_level": "bad"}) is None
    assert _confirmation_gap_pct(100.0, []) is None
