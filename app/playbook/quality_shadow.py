# -*- coding: utf-8 -*-
"""指令质量影子标签 (2026-10-18, record-only).

目的: 把 10-18 归因发现的"高分 LONG 反向、中分+回踩更强、证据层级有差异"
落成可统计的 instruction-level 标签, 供后续影子裁决. 本模块只写 metadata,
不参与方向/入场/仓位/止损/出场任何判定.

口径:
  - direction: BUY/LONG -> LONG; SELL_SHORT/SHORT -> SHORT; 其余不打标.
  - score_band: raw_instruction.score_band; 无则按 score 推导 (>=8 HIGH, >=6 MEDIUM).
  - evidence_tier: UE self_tier / origin.self_tier / DA ue_self_tier.
  - market_corroborated: raw/origin corroborated_by_market (None=未知).
  - citation_state: evidence_citation_state / ue_evidence_citation_state.
  - borrow_basis: short_check/origin.short_check/ue_short_check.borrow_data_basis.
  - would_downgrade_high_score_long: LONG + HIGH + (挑战者降级/证据缺口>=2/市场未确认/THESIS_ONLY).
  - would_prefer_medium_pullback: LONG + MEDIUM + PULLBACK_LIMIT.
  - would_require_market_confirmation: LONG + (THESIS_ONLY/明确未确认/传导假设).
  - would_allow_unknown_short_small: SHORT + borrow UNKNOWN.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _load_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _origin_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    origin = raw.get("origin")
    return origin if isinstance(origin, dict) else {}


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _as_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _gap_count(value: Any) -> int:
    if isinstance(value, (list, dict)):
        return len(value)
    return 1 if value else 0


def build_quality_shadow(inst: Dict[str, Any]) -> Dict[str, Any]:
    """从一条指令计算 record-only 质量标签; SKIP/未知方向返回 {}."""
    raw = _load_obj(inst.get("raw_instruction") or inst.get("raw_instruction_json"))
    origin = _origin_dict(raw)
    direction_raw = str(inst.get("direction") or "").upper()
    direction = {"BUY": "LONG", "LONG": "LONG",
                 "SELL_SHORT": "SHORT", "SHORT": "SHORT"}.get(direction_raw)
    if not direction:
        return {}

    score = _as_float(raw.get("score"))
    score_band = str(raw.get("score_band") or "").upper().strip()
    if score_band not in ("HIGH", "MEDIUM"):
        score_band = "HIGH" if score is not None and score >= 8 else (
            "MEDIUM" if score is not None and score >= 6 else "UNKNOWN")

    confidence = _as_float(inst.get("confidence"))
    if confidence is None:
        raw_conf = _as_float(raw.get("confidence"))
        confidence = raw_conf * 100 if raw_conf is not None and raw_conf <= 1 else raw_conf

    evidence_tier = str(
        raw.get("self_tier") or origin.get("self_tier")
        or raw.get("ue_self_tier") or "UNKNOWN"
    ).upper()
    market_corroborated = _as_bool(
        raw.get("corroborated_by_market")
        if raw.get("corroborated_by_market") is not None
        else origin.get("corroborated_by_market")
    )
    confidence_basis = str(
        raw.get("ue_confidence_basis") or origin.get("confidence_basis") or ""
    ).upper().strip()
    if market_corroborated is None and confidence_basis:
        if confidence_basis == "MARKET_CONFIRMED":
            market_corroborated = True
        elif confidence_basis in ("THESIS_ONLY", "TRANSMISSION_ASSUMED"):
            market_corroborated = False
    citation_state = str(
        raw.get("evidence_citation_state")
        or origin.get("evidence_citation_state")
        or raw.get("ue_evidence_citation_state") or ""
    ).lower()
    archetype = str(raw.get("entry_archetype") or "UNKNOWN").upper()
    decision_id = str(inst.get("decision_id") or "")
    info_channel = str(origin.get("info_channel") or raw.get("info_channel") or "")
    first_news_id = str(origin.get("first_news_id") or raw.get("first_news_id") or "")
    evidence_ref = str(origin.get("evidence_ref") or raw.get("ue_evidence") or "")
    event_ids = sorted(set(re.findall(r"event_id=([A-Za-z0-9_-]{6,})", evidence_ref)))
    critique = raw.get("critique") if isinstance(raw.get("critique"), dict) else {}
    origin_event_meta = origin.get("evidence_matched_event_meta")
    if not isinstance(origin_event_meta, dict):
        origin_event_meta = {}
    origin_label = "UE" if (
        origin or raw.get("ue_batch_id") or raw.get("ue_evidence")
        or decision_id.startswith("universe_expansion")
    ) else ("DA" if decision_id.startswith("DA-") or raw.get("source") == "deep_analysis" else "UNKNOWN")

    short_check = raw.get("short_check") if isinstance(raw.get("short_check"), dict) else {}
    origin_short = origin.get("short_check") if isinstance(origin.get("short_check"), dict) else {}
    ue_short = raw.get("ue_short_check") if isinstance(raw.get("ue_short_check"), dict) else {}
    ue_reaction = raw.get("ue_event_reaction") if isinstance(
        raw.get("ue_event_reaction"), dict) else {}
    planned_entry = raw.get("planned_entry")
    if planned_entry in (None, ""):
        confirmation = raw.get("confirmation")
        if isinstance(confirmation, dict):
            planned_entry = confirmation.get("price_level")
    planned_entry = _as_float(planned_entry)
    instruction_entry = _as_float(inst.get("entry_price"))
    plan_gap_pct = (
        round((instruction_entry - planned_entry) / planned_entry * 100.0, 3)
        if (planned_entry and planned_entry > 0 and instruction_entry)
        else None
    )
    borrow_basis = str(
        short_check.get("borrow_data_basis")
        or origin_short.get("borrow_data_basis")
        or ue_short.get("borrow_data_basis") or ""
    ).upper()

    lowered = bool(raw.get("score_lowered_by_challenger"))
    gaps = _gap_count(raw.get("evidence_gaps"))
    high_long = direction == "LONG" and score_band == "HIGH"
    downgrade = bool(high_long and (
        lowered or gaps >= 2 or market_corroborated is False
        or evidence_tier == "THESIS_ONLY"
    ))
    prefer_pullback = bool(
        direction == "LONG" and score_band == "MEDIUM" and archetype == "PULLBACK_LIMIT")
    require_confirmation = bool(direction == "LONG" and (
        evidence_tier == "THESIS_ONLY"
        or confidence_basis in ("THESIS_ONLY", "TRANSMISSION_ASSUMED")
        or market_corroborated is False
        or citation_state in ("transmission_assumed", "not_found")
    ))

    return {
        "schema": "quality_shadow_v2",
        "direction": direction,
        "origin": origin_label,
        "source": str(raw.get("source") or "") or None,
        "info_channel": info_channel or None,
        "first_news_id": first_news_id or None,
        "evidence_ref": evidence_ref[:300] or None,
        "event_ids": event_ids,
        "event_family": str(
            raw.get("event_family") or origin.get("event_family")
            or origin_event_meta.get("event_family") or "") or None,
        "score": score,
        "score_band": score_band,
        "confidence": confidence,
        "evidence_tier": evidence_tier,
        "confidence_basis": confidence_basis or None,
        "market_corroborated": market_corroborated,
        "evidence_citation_state": citation_state or None,
        "evidence_gaps": gaps,
        "score_lowered_by_challenger": lowered,
        "challenger_score": _as_float(raw.get("challenger_score_shadow")),
        "challenger_self_confidence": str(raw.get("challenger_self_confidence") or "") or None,
        "challenger_direction": str(raw.get("challenger_direction") or "") or None,
        "critique_verdict": str(critique.get("verdict") or "") or None,
        "critique_weakest_link": str(critique.get("weakest_link") or "")[:200] or None,
        "debate_outcome": str(raw.get("debate_outcome") or "")[:200] or None,
        "entry_archetype": archetype,
        "planned_entry": planned_entry,
        "plan_gap_pct": plan_gap_pct,
        "borrow_data_basis": borrow_basis or None,
        "event_age_h": _as_float(ue_reaction.get("age_h")),
        "post_event_move_pct": _as_float(ue_reaction.get("post_event_move_pct")),
        "reaction_basis": str(ue_reaction.get("reaction_basis") or "") or None,
        "reaction_source": str(ue_reaction.get("source") or "") or None,
        "reaction_reason": str(ue_reaction.get("reason") or "") or None,
        "would_downgrade_high_score_long": downgrade,
        "would_prefer_medium_pullback": prefer_pullback,
        "would_require_market_confirmation": require_confirmation,
        "would_allow_unknown_short_small": bool(
            direction == "SHORT" and borrow_basis == "UNKNOWN"),
    }


def attach_quality_shadow(inst: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把标签合并进 inst['metadata'] (保留既有键); 失败/空标签返回 None."""
    try:
        tags = build_quality_shadow(inst)
        if not tags:
            return None
        metadata = inst.get("metadata")
        if metadata is None:
            metadata = _load_obj(inst.get("metadata_json"))
        elif not isinstance(metadata, dict):
            metadata = _load_obj(metadata)
        metadata["quality_shadow"] = tags
        inst["metadata"] = metadata
        return tags
    except Exception as exc:
        logger.warning(
            "[quality_shadow] 标签计算失败 (不影响指令入库): %s", exc, exc_info=True)
        return None
