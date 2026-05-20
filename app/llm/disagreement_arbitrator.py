"""
Multi-Model 分歧仲裁 (L5, 2026-05-14)

voting_coordinator 的 N 个模型分歧度 > 阈值 → 启动 LLM 仲裁:
- 收集各模型 view (direction + confidence + reasoning)
- 调第三方仲裁 LLM (默认 Qwen-Max) 给出最终判定
- 输出 final_direction + final_confidence + reasoning_summary

可控:
- 默认 disabled
- 仅高分歧场景启用 (~10% 调用占比)
- max_rounds=2, 单次 < 60s
- 每日 cap (yaml: daily_max_calls)
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import parse_and_validate, clamp_numeric
from app.llm.features.llm_helper import call_llm
from app.llm.features.audit_db import get_feature_call_count

logger = logging.getLogger(__name__)

ALLOWED_DIRECTION = ["LONG", "SHORT", "NEUTRAL"]


@dataclass
class ArbitrationOutcome:
    final_direction: str
    final_confidence: float
    final_strength: int
    reasoning_summary: str
    rounds_used: int


def compute_disagreement(model_views: List[Dict[str, Any]]) -> float:
    """
    计算分歧度 (0=完全一致, 1=完全分歧).
    简单方法: direction 不同 + confidence 方差.
    """
    if not model_views or len(model_views) < 2:
        return 0.0
    # direction 投票
    dirs = [v.get("direction", "NEUTRAL") for v in model_views]
    long_count = dirs.count("LONG")
    short_count = dirs.count("SHORT")
    neutral_count = dirs.count("NEUTRAL")
    n = len(dirs)
    # 每个方向的占比, 均匀分布 (1/3 each) 时分歧最大
    max_count = max(long_count, short_count, neutral_count)
    direction_consensus = max_count / n  # 0.33-1.0
    # 分歧度 = 1 - 共识度, 但 bound [0, 0.67]
    direction_disagree = 1.0 - direction_consensus

    # confidence 方差
    confs = [float(v.get("confidence", 0.5)) for v in model_views]
    if len(confs) >= 2:
        mean_c = sum(confs) / len(confs)
        var_c = sum((c - mean_c) ** 2 for c in confs) / len(confs)
        conf_disagree = min(1.0, var_c * 4)  # 方差 0.25 → 1.0
    else:
        conf_disagree = 0.0

    return min(1.0, 0.7 * direction_disagree + 0.3 * conf_disagree)


def arbitrate(
    symbol: str,
    model_views: List[Dict[str, Any]],
) -> Optional[ArbitrationOutcome]:
    """
    Args:
        model_views: [
            {"model_name":"qwen","direction":"LONG","confidence":0.7,"reasoning":"..."},
            ...
        ]
    """
    if not is_enabled("disagreement_arbitrator"):
        return None
    if len(model_views) < 2:
        return None

    cfg = get_feature_config("disagreement_arbitrator")
    threshold = float(cfg.get("disagreement_threshold", 0.5))
    daily_cap = int(cfg.get("daily_max_calls", 10))

    # 分歧度检查
    disagreement = compute_disagreement(model_views)
    if disagreement < threshold:
        return None

    # 每日 cap
    today_calls = get_feature_call_count("disagreement_arbitrator", hours=24)
    if today_calls >= daily_cap:
        logger.warning(f"[DisagreementArbitrator] 当日 cap 耗尽 {today_calls}/{daily_cap}, 跳过")
        return None

    # Build prompt
    views_summary = "\n".join(
        f"- {v.get('model_name','?')}: direction={v.get('direction','NEUTRAL')} "
        f"conf={v.get('confidence',0.5):.2f} | {(v.get('reasoning','') or '')[:120]}"
        for v in model_views
    )

    system = (
        "You are FTSeek's senior arbitrator. Multiple models disagree on a trade. "
        "Weigh evidence, choose final direction. Output strict JSON only."
    )
    prompt = f"""Symbol: {symbol}

Model views:
{views_summary}

Disagreement level: {disagreement:.2f}

Arbitrate:
{{
  "final_direction": "LONG|SHORT|NEUTRAL",
  "final_confidence": 0.0-1.0,
  "final_strength": 0-100,
  "reasoning_summary": "<= 150 chars (which view wins, why)"
}}

Rules:
- If 2/N agree on direction with high confidence → tend to follow that direction
- If conflicting signals roughly balanced → NEUTRAL
- final_confidence should reflect lower than max(model_confidence) due to disagreement"""

    response = call_llm(
        feature="disagreement_arbitrator",
        prompt=prompt,
        system=system,
        model=cfg.get("arbitrator_model", "qwen"),
        temperature=0.0,
        max_tokens=300,
        timeout=int(cfg.get("timeout_sec", 30)),
    )
    if response is None:
        return None

    obj = parse_and_validate(
        response,
        required={
            "final_direction": str,
            "final_confidence": float,
            "final_strength": int,
            "reasoning_summary": str,
        },
        enums={"final_direction": ALLOWED_DIRECTION},
        feature="disagreement_arbitrator",
    )
    if obj is None:
        return None

    out = ArbitrationOutcome(
        final_direction=obj["final_direction"],
        final_confidence=clamp_numeric(obj["final_confidence"], 0.0, 1.0),
        final_strength=int(clamp_numeric(obj["final_strength"], 0, 100)),
        reasoning_summary=obj["reasoning_summary"][:300],
        rounds_used=1,
    )
    logger.info(
        f"[DisagreementArbitrator] {symbol} disagreement={disagreement:.2f} "
        f"→ {out.final_direction} conf={out.final_confidence:.2f} strength={out.final_strength}"
    )
    return out
