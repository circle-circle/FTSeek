"""[RFC-172] 占位候选 SKIP 文案归因分桶 (record-only) 契约测试。

守两件事:
1) 分桶正确: da_gate_decisions.jsonl 的"破位硬买拦截/观望/BLOCK"分别落对桶;
2) 文案前缀不变: 下游 observe_nightly_supply.py 按 startswith("非当前通道候选") 过滤
   占位行, 改前缀会把占位行算成真实供给 ⇒ 这里锁死前缀与"无归因=旧文案逐字一致"。
"""
import json

from app.playbook.pipeline import _da_gate_buckets, _placeholder_skip_reason

LEGACY = "非当前通道候选(bridge未提名/无方向)"


def _write(tmp_path, rows):
    p = tmp_path / "da_gate_decisions.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return str(p)


def test_buckets_classify_gate_block_skip_and_unknown(tmp_path):
    rows = [
        {"trade_date": "2026-10-26", "symbol": "US.ARM", "direction": "BUY",
         "decision": "BLOCK",
         "reasons": ["破位硬买拦截: 真实动量=空头排列但LLM=BUY → SKIP"]},
        {"trade_date": "2026-10-26", "symbol": "US.CRM", "direction": "BUY",
         "decision": "BLOCK", "reasons": ["质量分25过低，建议SKIP"]},
        {"trade_date": "2026-10-26", "symbol": "US.LLY", "direction": "SKIP",
         "decision": "BLOCK", "reasons": ["分析结论排序=SKIP"]},
        {"trade_date": "2026-10-26", "symbol": "US.VZ", "direction": "SELL_SHORT",
         "decision": "PASS", "reasons": []},
        {"trade_date": "2026-10-25", "symbol": "US.OLD", "direction": "BUY",
         "decision": "BLOCK", "reasons": ["破位硬买拦截"]},   # 非当日 → 忽略
    ]
    b = _da_gate_buckets("2026-10-26", path_override=_write(tmp_path, rows))
    assert b["US.ARM"] == "gate:破位硬买"
    assert b["US.CRM"] == "gate:其它拦截"
    assert b["US.LLY"] == "DA观望"
    assert b["US.VZ"] == "DA其它"
    assert "US.OLD" not in b
    assert "US.NVDA" not in b            # 未进 DA 评分 → 无桶


def test_missing_file_falls_back_to_empty(tmp_path):
    assert _da_gate_buckets("2026-10-26", path_override=str(tmp_path / "nope.jsonl")) == {}


def test_label_keeps_legacy_prefix_and_text():
    buckets = {"US.ARM": "gate:破位硬买", "US.LLY": "DA观望"}
    arm = _placeholder_skip_reason("US.ARM", buckets)
    lly = _placeholder_skip_reason("US.LLY", buckets)
    nvda = _placeholder_skip_reason("US.NVDA", buckets)
    for lab in (arm, lly, nvda):
        assert lab.startswith("非当前通道候选")     # 下游 startswith 过滤契约
        assert len(lab) < 80                        # 不炸推送行长
    assert "DA=gate:破位硬买" in arm
    assert "DA=DA观望" in lly
    assert nvda == LEGACY                           # 无归因 → 原文案逐字一致
    assert _placeholder_skip_reason("US.NVDA", None) == LEGACY