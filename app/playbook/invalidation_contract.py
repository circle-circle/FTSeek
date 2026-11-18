"""Compile Playbook invalidation inputs into a review-only contract.

The A0 contract is intentionally inert: every compiled action is
``REVIEW_ONLY``.  Nothing in this module exits, reduces, blocks overnight, or
changes a hard stop.  It only normalises the structured triggers in
``raw_instruction_json`` and the legacy natural-language ``invalidation`` into
a deterministic object for audit and later adjudication.

Units:
    structured 收盘价 / 5日涨跌 / 盘前涨跌幅 are numeric values in their
    native metric units; 量比 is a ratio.  Text price rules are absolute
    prices.  Text percentage rules are percent-of-price.

Consumer:
    scripts/invalidation_contract_audit.py.  A future A2 consumer must receive
    a separate, explicitly approved contract action; this module rejects any
    action other than REVIEW_ONLY during validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from app.playbook.invalidation_shadow import _PCT_PAT, _PRICE_PAT


INVALIDATION_CONTRACT_SCHEMA = "invalidation_contract_v1"
ACTION_REVIEW_ONLY = "REVIEW_ONLY"
PRECEDENCE = ["hard_sl", "structured", "text"]
STRUCTURED_METRICS = ("收盘价", "量比", "5日涨跌", "盘前涨跌幅")
STRUCTURED_OPERATORS = ("<", ">")
_COMPOUND_PAT = re.compile(r"或者|或|并且|且|\band\b|\bor\b", re.I)
_DEADLINE_PAT = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")

# ── 文本价解析 (2026-10-28 接管修复, DEB-20261028 §15.8) ───────────────────────
# 病灶: 原实现取 _PRICE_PAT 的**第一处**匹配, 再加 0.1–10× 粗门判定退化。实测 670 笔
#   主窗口: 13 条被采纳的文本价离入场价 15%–833% (跨资产/日期/窗口误读, 如 UBER 采纳
#   `$680`@72.9、MRK `$720`@125), 同时 12 条判"退化"的文本里有 5 条其实含可用价
#   (T `$21.80`、TSM `$372.72`、IBKR `$92.80`、HUM `$428`、CRM `237.23`)。
# 修法 (预注册规则, 不得事后放宽):
#   ① 先剥掉非价格数字 (百分比/日期/MA 窗口/N日/周/倍/亿);
#   ② 候选价 = 动词后数字 (`跌破X`) > 匿名"关键位词"数字 (低点/支撑/缺口/下沿/最低/收盘/价位/MA);
#   ③ 在候选里取**离入场锚最近**且偏离 ≤30% 的; 无候选接入 ±30% ⇒ `degenerate`;
#      完全无数字 ⇒ `unparseable`。30% 上限写死为 `_MAX_TEXT_PRICE_DISTANCE`。
_MAX_TEXT_PRICE_DISTANCE = 0.30
_PRICE_VERB_PAT = re.compile(
    r"(?:跌破|破位|跌穿|下破|below|支撑[位]?)\s*[:：]?\s*\$?(\d[\d,]*(?:\.\d+)?)"
)
# SHORT 仓的失效是"向上破位"(突破/站上/上破), 与 LONG 的"跌破"互为镜像 —— 两种动词
# 不能混用: 把"突破X"当 LONG 失效价会退化成"永远不触发的死规则"。
_PRICE_BREAK_PAT = re.compile(
    r"(?:突破|上破|站上|涨破|above)\s*[:：]?\s*\$?(\d[\d,]*(?:\.\d+)?)"
)
_NUM_PAT = re.compile(r"(\$)?(\d[\d,]*(?:\.\d+)?)")
_UNIT_PAT = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:%|日|天|周|个月|月|年|批|次|倍|小时|分钟|亿|万)"
)
_MA_PAT = re.compile(r"MA\s*\d+", re.I)
_DATE_RANGE_PAT = re.compile(r"\d{1,4}\s*[-/月]\s*\d{1,2}(?:\s*[-/日]\s*\d{1,4})?")
_LEVEL_WORDS = ("低点", "支撑", "缺口", "下沿", "最低", "收盘", "价位", "MA")


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalise_deadline(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    match = _DEADLINE_PAT.search(text)
    if not match:
        return None
    try:
        return date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        ).isoformat()
    except ValueError:
        return None


def _metric_price_basis(metric: str) -> str:
    if metric in ("收盘价", "5日涨跌"):
        return "close"
    if metric == "量比":
        return "session_volume"
    if metric == "盘前涨跌幅":
        return "premarket"
    return "unknown"


def compile_structured_rules(
    raw_instruction_json: Any,
) -> Tuple[list[Dict[str, Any]], list[Dict[str, Any]], int]:
    """Return (valid_rules, rejected_entries, raw_entry_count)."""
    raw = parse_json_object(raw_instruction_json)
    entries = raw.get("invalidation_triggers")
    if not isinstance(entries, list):
        return [], [], 0
    rules: list[Dict[str, Any]] = []
    rejected: list[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            rejected.append({"index": index, "reason": "not_object"})
            continue
        metric = str(entry.get("metric") or "").strip()
        operator = str(entry.get("operator") or "").strip()
        value = _finite_float(entry.get("value"))
        if metric not in STRUCTURED_METRICS:
            rejected.append({"index": index, "reason": "bad_metric", "metric": metric})
            continue
        if operator not in STRUCTURED_OPERATORS:
            rejected.append(
                {"index": index, "reason": "bad_operator", "operator": operator}
            )
            continue
        if value is None:
            rejected.append({"index": index, "reason": "bad_value"})
            continue
        rules.append({
            "rule_id": f"structured:{index}",
            "source": "structured",
            "metric": metric,
            "operator": operator,
            "value": value,
            "unit": "ratio" if metric == "量比" else "percent" if "涨跌" in metric else "price",
            "deadline": _normalise_deadline(entry.get("deadline")),
            "price_basis": _metric_price_basis(metric),
            "action": ACTION_REVIEW_ONLY,
        })
    return rules, rejected, len(entries)


def _strip_non_price_numbers(text: str) -> str:
    """Blank out numbers that are never absolute prices (%, dates, MA windows, counts)."""
    cleaned = _PCT_PAT.sub(" ", text)
    cleaned = _UNIT_PAT.sub(" ", cleaned)
    cleaned = _DATE_RANGE_PAT.sub(" ", cleaned)
    cleaned = _MA_PAT.sub(" ", cleaned)
    cleaned = re.sub(r"20\d{2}\s*年?", " ", cleaned)
    return cleaned


def text_price_candidates(invalidation: Any, direction: Any = None) -> Tuple[list, list]:
    """Return (verb_hits, level_hits) price candidates parsed from free text.

    verb_hits  — number directly after the direction-appropriate break verb
                 (LONG: 跌破/下破/…  SHORT: 突破/站上/…), highest confidence
    level_hits — number carrying a level word (低点/支撑/缺口下沿/最低/收盘价/MA) or "$"
    Both pools keep every distinct value so the caller can pick the one closest to the
    entry anchor instead of trusting the first regex hit.
    """
    text = str(invalidation or "").strip()
    if not text:
        return [], []
    cleaned = _strip_non_price_numbers(text)
    direction_text = str(direction or "").upper()
    verb_pattern = (
        _PRICE_BREAK_PAT
        if direction_text in ("SHORT", "SELL_SHORT", "SELL")
        else _PRICE_VERB_PAT
    )
    verb_hits: list = []
    for match in verb_pattern.finditer(cleaned):
        value = _finite_float(str(match.group(1)).replace(",", ""))
        if value is None or value <= 0:
            continue
        if value not in verb_hits:
            verb_hits.append(value)
    level_hits: list = []
    for match in _NUM_PAT.finditer(cleaned):
        value = _finite_float(str(match.group(2)).replace(",", ""))
        if value is None or value <= 0:
            continue
        has_dollar = bool(match.group(1))
        window = cleaned[max(0, match.start(2) - 8): match.start(2)]
        if not has_dollar and not any(word in window for word in _LEVEL_WORDS):
            continue
        if value not in level_hits:
            level_hits.append(value)
    return verb_hits, level_hits


def _pick_text_price(
    invalidation: Any, entry_price: Any, direction: Any = None
) -> Tuple[Optional[float], Optional[str], Optional[str], str, list]:
    """Pick the text price closest to the entry anchor within the pre-registered band.

    Returns (value, basis, phrase_basis, status, candidates).
    """
    text = str(invalidation or "").strip()
    if not text:
        return None, None, None, "empty", []
    verb_hits, level_hits = text_price_candidates(text, direction=direction)
    candidates = sorted(set(verb_hits) | set(level_hits))
    anchor = _finite_float(entry_price)
    if anchor is None or anchor <= 0:
        return None, None, None, "no_anchor", candidates
    for basis, pool in (("verb", verb_hits), ("level", level_hits)):
        near = sorted(
            (abs(value - anchor) / anchor, value)
            for value in pool
            if abs(value - anchor) / anchor <= _MAX_TEXT_PRICE_DISTANCE
        )
        if near:
            return near[0][1], basis, basis, "parseable", candidates
    if candidates:
        return None, None, None, "degenerate", candidates
    return None, None, None, "unparseable", candidates


def compile_text_rule(
    invalidation: Any, entry_price: Any = None, direction: Any = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Compile the legacy natural-language invalidation into one review-only rule.

    Price selection = closest candidate to the entry anchor within ±30% (see
    `_MAX_TEXT_PRICE_DISTANCE`); a sentence whose only numbers are cross-asset or
    window values is marked ``degenerate`` rather than guessed.
    """
    text = str(invalidation or "").strip()
    if not text:
        return None, "empty"
    value, basis, _phrase_basis, status, candidates = _pick_text_price(
        text, entry_price, direction=direction
    )
    if status == "parseable" and value is not None:
        # Direction-aware comparison: LONG invalidation is below the anchor,
        # SHORT invalidation is a break above it (原文用"突破/站稳"表述)。
        operator = ">" if str(direction or "").upper() in ("SHORT", "SELL_SHORT", "SELL") else "<"
        return {
            "rule_id": "text:price:0",
            "source": "text",
            "metric": "价格",
            "operator": operator,
            "value": value,
            "unit": "price",
            "deadline": None,
            "price_basis": "close" if "收盘" in text else "intraday",
            "text_phrase": "|".join(str(c) for c in candidates[:4]),
            "text_price_basis": basis,
            "text_price_candidates": candidates[:6],
            "action": ACTION_REVIEW_ONLY,
        }, "parseable"
    if status == "degenerate":
        return None, "degenerate"
    pct_match = _PCT_PAT.search(text)
    if pct_match:
        pct_value = _finite_float(pct_match.group(1))
        if pct_value is not None:
            return {
                "rule_id": "text:pct:0",
                "source": "text",
                "metric": "不利变动",
                "operator": ">=",
                "value": pct_value,
                "unit": "percent",
                "deadline": None,
                "price_basis": "intraday",
                "text_phrase": pct_match.group(0).strip(),
                "action": ACTION_REVIEW_ONLY,
            }, "parseable"
    return None, "unparseable"


def extract_text_price(
    invalidation: Any, entry_price: Any = None
) -> Optional[float]:
    rule, _status = compile_text_rule(invalidation, entry_price=entry_price)
    if rule and rule.get("metric") == "价格":
        return rule.get("value")
    return None


def _overall_price_basis(rules: Iterable[Mapping[str, Any]]) -> Optional[str]:
    bases = sorted({str(rule.get("price_basis")) for rule in rules if rule.get("price_basis")})
    if not bases:
        return None
    return bases[0] if len(bases) == 1 else "mixed"


def _earliest_deadline(rules: Iterable[Mapping[str, Any]]) -> Optional[str]:
    deadlines = sorted(
        str(rule["deadline"]) for rule in rules if rule.get("deadline")
    )
    return deadlines[0] if deadlines else None


def compile_pullback_failure_rule(
    *, direction: Any, pivot: Any, known_at: Any, timeframe: str = "5m",
) -> Dict[str, Any]:
    """Compile the R5 pullback-reclaim rule without changing legacy contract hashes.

    This standalone compiler is deliberately REVIEW_ONLY.  It gives the shadow a
    deterministic rule representation while leaving ``compile_invalidation_contract``
    and its existing source/hash semantics untouched.
    """
    value = _finite_float(pivot)
    direction_text = str(direction or "").upper()
    if value is None or value <= 0:
        raise ValueError("pivot must be a positive finite price")
    if direction_text not in ("LONG", "BUY", "SHORT", "SELL_SHORT", "SELL"):
        raise ValueError("unsupported direction")
    if not known_at:
        raise ValueError("known_at is required")
    is_short = direction_text in ("SHORT", "SELL_SHORT", "SELL")
    return {
        "rule_id": "structure:pullback_reclaim_failed:0",
        "source": "structure",
        "metric": "reclaim_failed",
        "operator": ">" if is_short else "<",
        "value": value,
        "unit": "price",
        "timeframe": timeframe,
        "price_basis": "bar_close",
        "known_at": str(known_at),
        "action": ACTION_REVIEW_ONLY,
    }


def compile_invalidation_contract(
    *,
    invalidation: Any,
    raw_instruction_json: Any,
    direction: Any = None,
    entry_price: Any = None,
) -> Dict[str, Any]:
    """Compile one instruction into a deterministic, review-only contract."""
    structured_rules, rejected, raw_count = compile_structured_rules(
        raw_instruction_json
    )
    text = str(invalidation or "").strip()
    has_text = bool(text)
    text_rule, text_status = compile_text_rule(
        text, entry_price=entry_price, direction=direction
    )
    text_candidates: list = []
    if has_text:
        _verb_hits, _level_hits = text_price_candidates(text, direction=direction)
        text_candidates = sorted(set(_verb_hits) | set(_level_hits))[:6]
    text_rules = [text_rule] if text_rule else []
    rules = structured_rules + text_rules
    legacy_text_parseable = (
        bool(_PRICE_PAT.search(text) or _PCT_PAT.search(text)) if has_text else None
    )
    if raw_count and has_text:
        source = "both"
    elif raw_count:
        source = "structured"
    elif has_text:
        source = "text"
    else:
        source = "none"

    warnings = []
    if has_text and text_status == "unparseable":
        warnings.append("text_unparseable")
    if has_text and text_status == "degenerate":
        warnings.append("text_degenerate_price")
    if rejected:
        warnings.append("structured_entries_rejected")
    if _COMPOUND_PAT.search(text):
        warnings.append("text_compound_condition")

    core: Dict[str, Any] = {
        "schema_version": INVALIDATION_CONTRACT_SCHEMA,
        "source": source,
        "rules": rules,
        "action": ACTION_REVIEW_ONLY,
        "price_basis": _overall_price_basis(rules),
        "requires_confirmation": bool(has_text or rejected),
        "deadline": _earliest_deadline(structured_rules),
        "reanchor_on_reprice": "stale",
        "precedence": list(PRECEDENCE),
        "parseable": bool(rules),
        "text_parseable": bool(text_rule) if has_text else None,
        "legacy_text_parseable": legacy_text_parseable,
        "text_parse_status": text_status,
        "text_compound_condition": bool(_COMPOUND_PAT.search(text)),
        "text_price_candidates": text_candidates,
        "structured_raw_count": raw_count,
        "structured_rejected": rejected,
        "warnings": warnings,
        "direction": str(direction or "").upper() or None,
        "entry_price": _finite_float(entry_price),
    }
    digest = hashlib.sha256(
        json.dumps(core, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    core["contract_hash"] = digest
    return core


def validate_invalidation_contract(contract: Mapping[str, Any]) -> list[str]:
    """Return contract invariant violations; A0 only permits REVIEW_ONLY."""
    errors = []
    if contract.get("schema_version") != INVALIDATION_CONTRACT_SCHEMA:
        errors.append("bad_schema_version")
    if contract.get("action") != ACTION_REVIEW_ONLY:
        errors.append("a0_action_must_be_REVIEW_ONLY")
    if contract.get("source") not in ("both", "structured", "text", "none"):
        errors.append("bad_source")
    if list(contract.get("precedence") or []) != PRECEDENCE:
        errors.append("bad_precedence")
    if contract.get("reanchor_on_reprice") != "stale":
        errors.append("bad_reanchor_policy")
    rules = contract.get("rules")
    if not isinstance(rules, list):
        errors.append("rules_not_list")
        return errors
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            errors.append("rule_not_object")
            continue
        rule_id = rule.get("rule_id")
        if not rule_id or rule_id in seen:
            errors.append("duplicate_or_missing_rule_id")
        seen.add(rule_id)
        if rule.get("action") != ACTION_REVIEW_ONLY:
            errors.append("rule_action_must_be_REVIEW_ONLY")
        if rule.get("unit") not in ("price", "percent", "ratio"):
            errors.append("bad_rule_unit")
    if contract.get("parseable") != bool(rules):
        errors.append("parseable_mismatch")
    return errors
