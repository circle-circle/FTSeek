"""[DEB-20260901 Phase B] pullback_limit_shadow 遥测 dict 兼容契约测试.

2026-09-01 首日全灭事故 (用户裁"立即修复"): db._row_to_dict (db.py) 自动把
raw_instruction_json 解析成 dict 进入 pipeline, 而 Phase B 三处新代码假设 str:
  ① trigger 影子行 json.loads(dict) TypeError 被 except 静默吞 → archetype 恒
     None → data/shadow/pullback_limit_shadow.jsonl 零落盘 (首日样本全灭)
  ② chase_cap 违例遥测 json.loads(dict) TypeError → 每轮 re-price 刷 warning
  ③ 触发特性影子 source 兜底同型 (fail-soft, 字段丢失)
本文件锁住: pipeline 不得再对 inst.get("raw_instruction_json") 直接 json.loads,
解析一律走 entry_routing.parse_raw_semantics (原生兼容 str|dict)。
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "app" / "playbook" / "pipeline.py"


def _src() -> str:
    return _SRC.read_text(encoding="utf-8")


def test_no_direct_json_loads_on_raw_instruction_json():
    s = _src()
    assert 'json.loads(inst.get("raw_instruction_json")' not in s, (
        "raw_instruction_json 经 _row_to_dict 已是 dict, 直接 json.loads 抛 "
        "TypeError (2026-09-01 首日遥测全灭根因); 应改用 "
        "entry_routing.parse_raw_semantics"
    )


def test_chase_cap_telemetry_uses_parse_raw_semantics():
    s = _src()
    idx = s.find("chase_cap遥测异常")
    assert idx > 0, "chase_cap 遥测异常兜底日志必须存在"
    block = s[max(0, idx - 2500):idx]
    assert "parse_raw_semantics" in block, (
        "chase_cap 遥测块必须用 parse_raw_semantics 解析 (dict 兼容)"
    )


def test_trigger_shadow_row_uses_parse_raw_semantics():
    s = _src()
    idx = s.find("[archetype影子]")
    assert idx > 0, "trigger 影子落盘日志标记必须存在"
    block = s[max(0, idx - 3000):idx]
    assert "parse_raw_semantics" in block, (
        "trigger 影子行必须用 parse_raw_semantics 解析 (dict 兼容, 防静默零落盘)"
    )


def test_source_fallback_isinstance_guard():
    s = _src()
    idx = s.find('_raw = inst.get("raw_instruction_json")')
    assert idx > 0, "source 兜底读取必须存在"
    block = s[idx:idx + 300]
    assert "isinstance(_raw, str)" in block, (
        "source 兜底必须先判 isinstance(str) 再 json.loads (dict 直通)"
    )
