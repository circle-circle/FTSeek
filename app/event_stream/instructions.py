from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.event_stream.intelligence import parse_timestamp

EVENT_ROLES = frozenset({"PRIMARY", "CONFIRM", "CONTRARY", "NONE"})
DIRECTIONS = frozenset({"BUY", "SELL_SHORT", "SKIP"})
OPPOSITIONS = frozenset({"NONE", "ABSTAIN", "OPPOSE"})
PRICE_IN = frozenset({"PRICED", "PARTIAL", "UNPRICED", "UNKNOWN"})

MAIN_EVENT_CONTRACT = """
在原有 JSON 字段之外，必须同时输出以下顶层字段，不得省略：
- event_role: PRIMARY/CONFIRM/CONTRARY/NONE，仅描述共享日内事件简报对本股的实际作用。
- event_ids: 数组；未使用共享简报时为 []，只能引用输入共享简报中的 event_id。
- price_in_status: PRICED/PARTIAL/UNPRICED/UNKNOWN。
- price_in_evidence: 字符串；非 UNKNOWN 时必须有本股价格或量能证据，UNKNOWN 时可为空。
- valid_until: 逐字复制输入给定的当日 ET ISO 时间，不得改写或省略。
这些字段与原 direction/execution_plan 等字段同等强制。
"""


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value > 0 else None


def _fatal(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict) and str(value.get("detail") or "").strip():
        return {"detail": str(value["detail"]).strip(), "evidence_ids": _strings(value.get("evidence_ids"))}
    if value:
        return {"detail": str(value), "evidence_ids": []}
    return None


def brief_event_ids(brief: Optional[Dict[str, Any]]) -> set[str]:
    if not brief:
        return set()
    output = set()
    for fact in brief.get("facts") or []:
        output.add(str(fact["event_id"]))
        output.update(str(item) for item in fact.get("member_event_ids") or [])
    return output


def _related_event_ids(brief: Optional[Dict[str, Any]], event_ids: Sequence[str]) -> set[str]:
    related = set(event_ids)
    for _ in range(2):
        for fact in (brief or {}).get("facts") or []:
            fact_ids = {str(fact["event_id"]), *(str(item) for item in fact.get("member_event_ids") or [])}
            if related & fact_ids:
                related.update(fact_ids)
        for transmission in (brief or {}).get("transmissions") or []:
            transmission_ids = {str(item) for item in transmission.get("event_ids") or []}
            if related & transmission_ids:
                related.update(transmission_ids)
    return related


def _global_fatal_for_events(
    brief: Optional[Dict[str, Any]], event_ids: Sequence[str],
) -> Optional[Dict[str, Any]]:
    fatal = _fatal((brief or {}).get("fatal_flaw"))
    if not fatal or not event_ids:
        return None
    if not _related_event_ids(brief, event_ids).intersection(fatal["evidence_ids"]):
        return None
    return fatal


def build_intraday_instruction_card(
    *,
    symbol: str,
    session_date: str,
    valid_until: str,
    result: Dict[str, Any],
    challenger_raw: Dict[str, Any],
    brief: Optional[Dict[str, Any]],
    arm: str,
    allowed_event_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    errors = []
    direction = str(result.get("direction") or "SKIP").upper()
    if direction not in DIRECTIONS:
        errors.append("invalid_direction")
        direction = "SKIP"
    for field_name in ("event_role", "event_ids", "price_in_status", "price_in_evidence"):
        if field_name not in result:
            errors.append(f"missing_{field_name}")
    event_role = str(result.get("event_role") or "NONE").upper()
    event_ids = _strings(result.get("event_ids"))
    if event_role not in EVENT_ROLES:
        errors.append("invalid_event_role")
    valid_event_ids = brief_event_ids(brief) if allowed_event_ids is None else allowed_event_ids
    for event_id in event_ids:
        if event_id not in valid_event_ids:
            errors.append(f"ungrounded_event:{event_id}")
    if event_role != "NONE" and not event_ids:
        errors.append("event_role_requires_event_ids")
    if event_role == "NONE" and event_ids:
        errors.append("event_ids_with_none_role")
    price_in_status = str(result.get("price_in_status") or "UNKNOWN").upper()
    price_in_evidence = str(result.get("price_in_evidence") or "").strip()
    if price_in_status not in PRICE_IN:
        errors.append("invalid_price_in")
    if price_in_status != "UNKNOWN" and not price_in_evidence:
        errors.append("price_in_requires_evidence")
    if "opposition" not in challenger_raw:
        errors.append("challenger_missing_opposition")
    if "fatal_flaw" not in challenger_raw:
        errors.append("challenger_missing_fatal_flaw")
    opposition = str(challenger_raw.get("opposition") or "ABSTAIN").upper()
    if opposition not in OPPOSITIONS:
        errors.append("invalid_opposition")
    fatal_flaw = _fatal(challenger_raw.get("fatal_flaw"))
    if fatal_flaw:
        if not fatal_flaw["evidence_ids"]:
            errors.append("fatal_flaw_requires_evidence")
        for event_id in fatal_flaw["evidence_ids"]:
            if event_id not in valid_event_ids:
                errors.append(f"ungrounded_fatal_flaw_event:{event_id}")
    opposition_detail = str(
        challenger_raw.get("opposition_detail")
        or challenger_raw.get("detail")
        or challenger_raw.get("falsification_target")
        or challenger_raw.get("skip_reason")
        or ""
    ).strip()
    opposition_event_ids = _strings(challenger_raw.get("evidence_ids"))
    for event_id in opposition_event_ids:
        if event_id not in valid_event_ids:
            errors.append(f"ungrounded_opposition_event:{event_id}")
    opposition_data_evidence = [
        item for item in (
            list(challenger_raw.get("challenged_claims") or [])
            + list(challenger_raw.get("missed_risks") or [])
        ) if item
    ]
    if opposition == "OPPOSE" and not opposition_detail:
        errors.append("oppose_requires_detail")
    if opposition == "OPPOSE" and not (
        opposition_event_ids or opposition_data_evidence or fatal_flaw
    ):
        errors.append("oppose_requires_evidence")
    if opposition == "OPPOSE" and not (
        challenger_raw.get("independent_direction")
        or challenger_raw.get("my_direction")
        or fatal_flaw
    ):
        errors.append("oppose_requires_direction_or_fatal_flaw")
    plan = result.get("execution_plan") or {}
    confirmation = plan.get("confirmation") or {}
    entry = _number(plan.get("entry_price")) or _number(confirmation.get("price_level"))
    stop = _number(plan.get("stop_loss_price"))
    target = _number(plan.get("take_profit_price"))
    invalidation = str(plan.get("invalidation") or "").strip()
    skip_reason = str(result.get("skip_reason") or "").strip()
    if direction == "SKIP":
        if not skip_reason:
            errors.append("skip_reason_required")
    else:
        if entry is None or stop is None or target is None:
            errors.append("numeric_entry_stop_target_required")
        elif direction == "BUY" and not stop < entry < target:
            errors.append("buy_price_geometry_invalid")
        elif direction == "SELL_SHORT" and not target < entry < stop:
            errors.append("short_price_geometry_invalid")
        if not invalidation:
            errors.append("invalidation_required")
    model_valid_until = str(result.get("valid_until") or "").strip()
    if model_valid_until != valid_until:
        errors.append("valid_until_mismatch")
    global_fatal_flaw = _global_fatal_for_events(brief, event_ids)
    card = {
        "schema": "intraday_instruction_v1",
        "session_date": session_date,
        "symbol": symbol,
        "as_of": (brief or {}).get("as_of") or f"{session_date}T08:35:00-05:00",
        "brief_version": (brief or {}).get("brief_id"),
        "arm": arm,
        "event_role": event_role,
        "event_ids": event_ids,
        "direction": direction,
        "score": result.get("score"),
        "entry_trigger": {
            "type": str(plan.get("entry_archetype") or "UNSPECIFIED"),
            "price": entry,
            "confirmation": confirmation,
        },
        "stop_loss": stop,
        "target": target,
        "invalidation": invalidation,
        "valid_until": valid_until,
        "price_in": {"status": price_in_status, "evidence": price_in_evidence},
        "independent_direction": str(
            challenger_raw.get("independent_direction") or challenger_raw.get("my_direction") or "SKIP"
        ).upper(),
        "opposition": opposition,
        "opposition_evidence": {
            "detail": opposition_detail,
            "event_ids": opposition_event_ids,
            "data_evidence": opposition_data_evidence,
        },
        "fatal_flaw": fatal_flaw,
        "global_fatal_flaw": global_fatal_flaw,
        "skip_reason": skip_reason,
        "arbitration_required": bool(fatal_flaw or global_fatal_flaw),
        "global_arbitration_required": bool(global_fatal_flaw),
        "status": "DEGRADED" if errors else "VALID",
        "validation_errors": sorted(set(errors)),
        "production_write": False,
    }
    return card


def compare_instruction_cards(
    control: Dict[str, Any],
    treatment: Dict[str, Any],
) -> Dict[str, Any]:
    numeric_fields = ("entry_trigger", "stop_loss", "target")
    plan_changed = any(control.get(field) != treatment.get(field) for field in numeric_fields)
    return {
        "symbol": treatment["symbol"],
        "control_direction": control["direction"],
        "treatment_direction": treatment["direction"],
        "direction_changed": control["direction"] != treatment["direction"],
        "control_score": control.get("score"),
        "treatment_score": treatment.get("score"),
        "event_role": treatment["event_role"],
        "event_ids": treatment["event_ids"],
        "plan_changed": plan_changed,
        "opposition_changed": control["opposition"] != treatment["opposition"],
        "fatal_flaw_changed": control["fatal_flaw"] != treatment["fatal_flaw"],
        "control_status": control["status"],
        "treatment_status": treatment["status"],
        "treatment_validation_errors": treatment["validation_errors"],
        "economic_value_claimed": False,
    }


def _decision_fingerprint(card: Dict[str, Any]) -> str:
    fields = {
        key: card.get(key) for key in (
            "direction", "entry_trigger", "stop_loss", "target", "invalidation",
            "valid_until", "event_role", "event_ids", "price_in",
        )
    }
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decision_id(card: Dict[str, Any]) -> str:
    return str(card.get("decision_id") or (
        f"OFFLINE-{card['session_date']}-{card['symbol']}-v{card.get('instruction_version', 1)}"
    ))


def reconcile_intraday_instruction(
    previous: Optional[Dict[str, Any]],
    candidate: Dict[str, Any],
    *,
    as_of: str,
    execution_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    state = execution_state or {}
    now = parse_timestamp(as_of, require_timezone=True)
    candidate_expiry = parse_timestamp(candidate["valid_until"], require_timezone=True)
    protected = any(bool(state.get(key)) for key in (
        "triggered", "executed", "broker_order_active", "entry_confirm_active",
    ))

    def result(action: str, reason: str, *, review_required: bool = False) -> Dict[str, Any]:
        if previous is None:
            version = 1
            supersedes = None
            decision_id = _decision_id(candidate)
        else:
            version = int(previous.get("instruction_version", 1))
            supersedes = _decision_id(previous) if action == "REVISE" else None
            decision_id = _decision_id(previous)
        if action == "REVISE":
            version += 1
            decision_id = f"OFFLINE-{candidate['session_date']}-{candidate['symbol']}-v{version}"
        return {
            "schema": "intraday_reconciliation_v1",
            "action": action,
            "reason": reason,
            "symbol": candidate["symbol"],
            "as_of": as_of,
            "instruction_version": version,
            "decision_id": decision_id,
            "supersedes_decision_id": supersedes,
            "review_required": review_required or bool(candidate.get("arbitration_required")),
            "production_write": False,
        }

    if previous is not None:
        previous_as_of = parse_timestamp(previous["as_of"], require_timezone=True)
        candidate_as_of = parse_timestamp(candidate["as_of"], require_timezone=True)
        if candidate_as_of <= previous_as_of:
            return result("NO_CHANGE", "STALE_REVISION")
        previous_expiry = parse_timestamp(previous["valid_until"], require_timezone=True)
        if now >= previous_expiry:
            if protected:
                return result("NO_CHANGE", "EXECUTION_STATE_PROTECTED", review_required=True)
            return result("INVALIDATE", "EXPIRED")
    if protected:
        return result("NO_CHANGE", "EXECUTION_STATE_PROTECTED", review_required=True)
    if now >= candidate_expiry:
        return result("INVALIDATE" if previous is not None else "NO_CHANGE", "EXPIRED")
    if candidate.get("status") != "VALID":
        return result("NO_CHANGE", "INVALID_CANDIDATE", review_required=True)
    if candidate["direction"] == "SKIP":
        return result("INVALIDATE" if previous is not None else "NO_CHANGE", "SKIP")
    if previous is None:
        return result("ISSUE", "NEW_ACTIONABLE_CARD")
    if _decision_fingerprint(previous) == _decision_fingerprint(candidate):
        return result("NO_CHANGE", "IDENTICAL_DECISION")
    return result("REVISE", "DECISION_CHANGED")


def arbitrate_intraday_card(card: Dict[str, Any]) -> Dict[str, Any]:
    direction = card["direction"]
    opposition = card.get("opposition")
    independent = card.get("independent_direction")
    fatal = card.get("fatal_flaw")
    global_fatal = card.get("global_fatal_flaw")
    reason = "NO_STRUCTURED_OBJECTION"
    if fatal:
        direction = "SKIP"
        reason = "UNRESOLVED_FATAL_FLAW"
    elif global_fatal:
        direction = "SKIP"
        reason = "UNRESOLVED_GLOBAL_FATAL_FLAW"
    elif opposition == "OPPOSE" and independent == "SKIP" and card.get("opposition_evidence"):
        direction = "SKIP"
        reason = "EVIDENCED_OPPOSE_SKIP"
    elif (
        opposition == "OPPOSE" and independent in {"BUY", "SELL_SHORT"}
        and independent != card["direction"] and card.get("opposition_evidence")
    ):
        direction = "SKIP"
        reason = "UNRESOLVED_TRUE_REVERSE"
    return {
        "schema": "intraday_arbitration_v1",
        "symbol": card["symbol"],
        "brief_version": card.get("brief_version"),
        "baseline_direction": card["direction"],
        "structured_direction": direction,
        "changed": direction != card["direction"],
        "reason": reason,
        "opposition": opposition,
        "independent_direction": independent,
        "fatal_flaw": fatal,
        "global_fatal_flaw": global_fatal,
        "repair_attempts": 0,
        "economic_value_claimed": False,
        "production_write": False,
    }


DOMAIN_TERMS = {
    "ai": {"ai", "artificial intelligence", "chip", "chips", "semiconductor", "semiconductors", "data center", "data centre", "cloud", "software", "hardware"},
    "healthcare": {"health", "hospital", "medical", "pharma", "biotech", "drug", "medicaid", "medicare"},
    "energy": {"oil", "crude", "energy", "gas", "opec", "iran"},
    "auto": {"auto", "automobile", "automobiles", "vehicle", "vehicles", "ev", "battery", "tesla"},
    "finance": {"bank", "financial", "insurance", "dollar", "gold", "treasury", "rate"},
}


def _domains(text: str) -> set[str]:
    lowered = text.casefold()
    return {
        domain for domain, terms in DOMAIN_TERMS.items()
        if any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered) for term in terms)
    }


def _select_symbol_transmissions(
    brief: Dict[str, Any],
    *,
    symbol: str,
    relevance_text: str,
    limit: int = 2,
) -> List[Dict[str, Any]]:
    bare_symbol = symbol.upper().removeprefix("US.")
    relevance_domains = _domains(relevance_text)
    scored = []
    for index, item in enumerate(brief.get("transmissions") or []):
        item_symbols = {
            str(value).upper().removeprefix("US.")
            for key in ("beneficiary_symbols", "loser_symbols")
            for value in item.get(key) or []
        }
        item_text = json.dumps({
            "theme": item.get("theme"),
            "causal_chain": item.get("causal_chain"),
        }, ensure_ascii=False)
        direct = bare_symbol in item_symbols
        related = bool(relevance_domains & _domains(item_text))
        score = (4 if direct else 0) + (2 if related else 0)
        scored.append((score, float(item.get("confidence") or 0), -index, item))
    positive = [row for row in scored if row[0] > 0]
    candidates = positive if bare_symbol or relevance_domains else scored
    candidates.sort(key=lambda row: (-row[0], -row[1], -row[2]))
    return [row[3] for row in candidates[:limit]]


def compact_symbol_event_section(
    brief: Optional[Dict[str, Any]],
    *,
    valid_until: str,
    symbol: str = "",
    relevance_text: str = "",
) -> str:
    contract = f"""## 大盘环境·共享日内事件简报与输出契约
本次仅为离线日内研究。valid_until 必须逐字输出为 {valid_until}。
主分析师额外输出 event_role(PRIMARY/CONFIRM/CONTRARY/NONE)、event_ids、price_in_status(PRICED/PARTIAL/UNPRICED/UNKNOWN)、price_in_evidence、valid_until。
event_role 只描述本共享简报的实际作用；未使用则 NONE 且 event_ids=[]。引用其他材料不得冒充共享简报 event_id。
非 UNKNOWN 的 price_in_status 必须在 price_in_evidence 给出本股价格/量能证据。保留原 direction/execution_plan 契约。
"""
    if not brief:
        return contract + "共享简报为空；event_role 必须 NONE，event_ids 必须为空。\n"
    selected_transmissions = _select_symbol_transmissions(
        brief, symbol=symbol, relevance_text=relevance_text,
    )
    transmissions = []
    for item in selected_transmissions:
        compact = {
            "event_ids": item.get("event_ids"), "theme": item.get("theme"), "sign": item.get("sign"),
            "price_in": item.get("price_in"),
            "invalidation": str(item.get("invalidation") or "")[:120],
            "causal_chain": str(item.get("causal_chain") or "")[:220],
            "counter_evidence": str(item.get("counter_evidence") or "")[:120],
        }
        for key in ("beneficiary_symbols", "loser_symbols"):
            if item.get(key):
                compact[key] = item[key]
        transmissions.append(compact)
    referenced = {
        event_id for item in transmissions for event_id in item.get("event_ids") or []
    }
    global_fatal = _global_fatal_for_events(brief, sorted(referenced))
    referenced.update((global_fatal or {}).get("evidence_ids") or [])
    facts = []
    for fact in brief.get("facts") or []:
        fact_ids = {fact["event_id"], *(fact.get("member_event_ids") or [])}
        if not referenced.intersection(fact_ids):
            continue
        compact_fact = {
            "event_id": fact["event_id"], "event_time": fact.get("event_time"),
            "first_seen_at": fact.get("first_seen_at"), "headline": fact.get("headline"),
        }
        member_facts = [{
                "event_id": member["event_id"], "first_seen_at": member.get("first_seen_at"),
                "headline": member.get("headline"), "source_id": member.get("source_id"),
            } for member in fact.get("member_facts") or [] if member["event_id"] != fact["event_id"]]
        if member_facts:
            compact_fact["member_facts"] = member_facts
        facts.append(compact_fact)
    payload = {
        "brief_id": brief["brief_id"], "as_of": brief["as_of"], "facts": facts,
        "transmissions": transmissions,
        "omitted_transmission_count": max(0, len(brief.get("transmissions") or []) - len(transmissions)),
    }
    if global_fatal:
        payload["global_fatal_flaw"] = global_fatal
    return contract + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n"


CHALLENGER_EVENT_CONTRACT = """
额外输出 opposition(NONE/ABSTAIN/OPPOSE) 与 fatal_flaw(null 或 {detail,evidence_ids})。
ABSTAIN 表示没有独立反对；OPPOSE 必须给 independent_direction/my_direction 或有证据的 fatal_flaw。
fatal_flaw 仅指足以否定方向或入场条件的实质错误，evidence_ids 只能引用共享简报事实；普通风险仍走原有字段。
"""
