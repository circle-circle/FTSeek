#!/usr/bin/env python3
"""快速验证 ADR-008 技术定方向仲裁 (2026-06-28)"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.pipeline.stages.signal_stage import _apply_technical_direction_arbitration, _get_signal_stage_mode

# 配置日志输出到 stdout
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


def make_candidate(strategy_id, direction, strength, confidence):
    return SignalCandidate(
        symbol="TEST",
        direction=direction,
        strength=strength,
        confidence=confidence,
        strategy_id=strategy_id,
        source=strategy_id,
        rationale="test",
        context=SymbolContext(symbol="TEST"),
    )


def case(name, candidates, expected_outcome):
    print(f"\n=== {name} ===")
    result = _apply_technical_direction_arbitration(candidates, "TEST")
    print(f"outcome: {expected_outcome}, returned: {result}")
    if expected_outcome == "traded":
        assert result is not None, "expected trade but got None"
    elif expected_outcome == "blocked":
        assert result is None, "expected block but got a signal"
    else:
        raise ValueError(f"unknown expected_outcome: {expected_outcome}")


print(f"signal_stage mode: {_get_signal_stage_mode()}")

technical_long = make_candidate("TECHNICAL_MULTI_FACTOR", "LONG", 65, 0.72)
llm_long = make_candidate("voting_llm", "LONG", 62, 0.80)
llm_short = make_candidate("voting_llm", "SHORT", 70, 0.80)

# Case 1: technical LONG + LLM LONG -> 交易
case("case 1: technical LONG + LLM LONG", [technical_long, llm_long], "traded")

# Case 2: technical LONG + LLM SHORT (强反向) -> 拦截
case("case 2: technical LONG + LLM SHORT (conflict)", [technical_long, llm_short], "blocked")

# Case 3: only LLM -> 拦截
case("case 3: LLM only", [llm_long], "blocked")

print("\n✅ 技术定方向仲裁快速验证通过")
