#!/usr/bin/env python3
"""RFC-208 · EventContext prompt-injection canary report (read-only).

Purpose
-------
Read the real DA checkpoint produced **after** EventContext injection and answer
one question only: *did the injected prompt behave safely on today's samples?*

For every ``data/da_checkpoint/<date>/US.*.json`` (raw checkpoints and ``.bak``
files are excluded as report targets, but ``US.*.raw.json`` is read as an input):

  * locate the injection metadata, which the pipeline may persist at
    ``analysis["_event_context"]`` (outer/top-level), at
    ``analysis["_llm_output"]["_event_context"]``, or at
    ``<symbol>.raw.json["event_context"]``;
  * read ``injected`` / ``reason`` / ``brief_id`` / ``injection_hash`` /
    ``event_ids`` plus the model's ``_prompt_hash``, ``user_prompt`` presence,
    ``direction`` / ``score`` / ``event_role`` / model-cited ``event_ids``;
  * reuse ``scripts.da_model_ab_replay.score`` + ``day_bars`` for
    ``valid_card`` / ``research_complete`` / price geometry / direction-plan
    consistency;
  * compare against the latest shared brief facts/member IDs (ungrounded event
    IDs are a hard canary failure);
  * optionally compare against a ``da_model_ab_replay`` JSONL (and its
    ``.manifest.json`` sidecar) for same-symbol direction observations and
    aggregate valid/research rates.

Record-only claim validation (evidence quality, never a gate)
------------------------------------------------------------
Per symbol the same two documents are re-read to annotate card claims.  These
findings are informational: they set ``evidence_quality_pass=false``, append
``<SYMBOL>:<code>`` entries to ``issues`` and force ``status=WARN``, but they
never touch ``canary_pass``, Gate visibility or any order path.

  * ``unsupported_indicator_claim`` — a claim names an indicator (KDJ, RSI,
    MACD, ...) for which no machine-computed series exists in the collected
    payloads.  Generic OSC/RSI/MA claims are *not* flagged when the collected
    data carries that series.  ``app/data/lv3_technical_calculator.py`` lists
    KDJ in its docstring but implements no KDJ calculation, so a card quoting
    "KDJ低位金叉" has nothing behind it.
  * ``valuation_contradiction`` — PE(TTM) diverges >25% from the dynamic or
    normalized PE, a PE caliber present in the data is left undisclosed, or an
    industry PE median is quoted from fewer than 5 usable peers.
  * ``event_factual_precision_warning`` — an "officially confirmed" style claim
    while every resolved brief source is still ``UNVERIFIED``.

Metrics: ``unsupported_claim_count`` / ``valuation_contradiction_count``
(plus ``event_factual_precision_count`` and ``evidence_quality_fail_symbol_count``).

Safety contract
---------------
  * Zero paid calls: no LLM/network code is imported or invoked.
  * Zero production writes: no DB, playbook, config, cron, notifier or order
    path is touched.  Output is limited to the canary shadow directory.
  * ``economic_value_claimed=false`` and ``production_write=false`` are always
    emitted.
  * ``latest.json`` is atomically replaced; ``<date>/<content_hash>.json`` is
    created with ``O_EXCL`` and never overwritten.  Re-running with unchanged
    inputs reuses the same immutable artifact (idempotent).
  * The canary requires, for every injected sample:
      - metadata present and consistent with the latest shared brief
        (brief_id / as_of / status and a re-derivable ``injection_hash``);
      - a ``_prompt_hash`` that is present and differs from the baseline
        ``llm_synth.PROMPT_HASH``;
      - no model-cited event ID absent from the latest brief facts/member IDs;
      - no event attribution when the model reports ``event_role=NONE``.
  * With ``--control`` the canary valid/research rates may not fall more than
    5 percentage points below the control manifest/JSONL.

Machine-readable status: ``OK`` / ``WARN`` / ``ERROR`` and ``canary_pass``.
Exit codes: OK=0, WARN=1, ERROR=2.

CLI
---
    ./venv/bin/python scripts/da_event_inject_report.py --date 2026-11-13
    ./venv/bin/python scripts/da_event_inject_report.py --date 2026-11-13 \\
        --control data/shadow/da_model_ab_nvda_20261112_v2.jsonl
    ./venv/bin/python scripts/da_event_inject_report.py --eval \\
        --latest-path data/shadow/da_event_prompt_canary/latest.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.da_model_ab_replay import score as score_card  # noqa: E402
from scripts.shadow_runtime import (  # noqa: E402
    atomic_write_json,
    read_json,
    sha256_json,
    write_json_new,
)
from app.event_stream.da_prompt_injection import (  # noqa: E402
    CONTRACT_VERSION,
    DEFAULT_LATEST_PATH,
    _hash_identity,
)

SCHEMA = "da_event_prompt_canary_v1"
WRITER = "scripts/da_event_inject_report.py"
DEFAULT_CHECKPOINT_ROOT = ROOT / "data" / "da_checkpoint"
DEFAULT_OUT_ROOT = ROOT / "data" / "shadow" / "da_event_prompt_canary"
CONTROL_DROP_PP = 5.0
DIRECTIONS = frozenset({"BUY", "SELL_SHORT", "SKIP"})
EVENT_ROLES = frozenset({"PRIMARY", "CONFIRM", "CONTRARY", "NONE"})
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
UNITS = {
    "rates": "ratio_0_to_1",
    "delta": "percentage_points",
    "score": "model_score",
}

# --- Record-only claim validation (evidence quality; never a gate) ----------
# These checks annotate the report only.  ``canary_pass`` keeps its RFC-208
# meaning (metadata / prompt hash / grounding / control rates); claim-quality
# findings live under ``evidence_quality_pass`` and never block anything.
VALUATION_DEVIATION_THRESHOLD = 0.25   # >25% relative divergence between PE estimators
MIN_PEER_SAMPLE = 5                    # an industry PE median needs >= 5 usable peers
ISSUE_UNSUPPORTED_INDICATOR = "unsupported_indicator_claim"
ISSUE_VALUATION_CONTRADICTION = "valuation_contradiction"
ISSUE_EVENT_FACTUAL_PRECISION = "event_factual_precision_warning"
EVIDENCE_ISSUE_CODES = (
    ISSUE_UNSUPPORTED_INDICATOR,
    ISSUE_VALUATION_CONTRADICTION,
    ISSUE_EVENT_FACTUAL_PRECISION,
)
# Indicators that have *no machine implementation anywhere* in the pipeline.
# ``app/data/lv3_technical_calculator.py`` lists KDJ in its module docstring but
# ships no ``_calc_kdj`` and no other module emits a KDJ series, so a card that
# cites "KDJ低位金叉" is quoting an indicator the system never computed.
EXPLICITLY_UNSUPPORTED_INDICATORS = frozenset({"KDJ"})


def _ascii_token(token: str) -> str:
    """Match an ASCII indicator name next to CJK text.

    ``\\b`` is unusable here: in Unicode regex mode CJK characters count as word
    characters, so ``\\bKDJ\\b`` never matches "且KDJ低位".  ASCII-only
    lookarounds keep "KDJ"/"RSI" matching beside Chinese while still refusing to
    match inside longer ASCII identifiers.
    """
    return rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"


# Indicator name -> pattern proving the *claim* names that indicator.
INDICATOR_CLAIM_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("KDJ", re.compile(_ascii_token("KDJ") + r"|随机指标")),
    ("RSI", re.compile(r"(?<![A-Za-z0-9])RSI\d{0,3}(?![A-Za-z0-9])|相对强弱")),
    ("MACD", re.compile(_ascii_token("MACD"))),
    ("BOLL", re.compile(_ascii_token("BOLL") + r"|布林")),
    ("ATR", re.compile(_ascii_token("ATR"))),
    ("VWAP", re.compile(_ascii_token("VWAP"))),
    ("OBV", re.compile(_ascii_token("OBV"))),
    ("CCI", re.compile(r"(?<![A-Za-z0-9])CCI\d{0,3}(?![A-Za-z0-9])")),
    ("ADX", re.compile(r"(?<![A-Za-z0-9])ADX\d{0,3}(?![A-Za-z0-9])")),
    ("WR", re.compile(r"(?<![A-Za-z0-9])WR\d{1,2}(?![A-Za-z0-9])|威廉指标")),
    ("OSC", re.compile(_ascii_token("OSC") + r"|振荡指标")),
    (
        "MA",
        re.compile(r"(?<![A-Za-z0-9])(?:MA|EMA|SMA)\d{1,3}(?![A-Za-z0-9])|均线|移动平均"),
    ),
)

# Indicator -> patterns that identify a *machine-computed* series in the
# collected payloads (matched as ``fullmatch`` against lowercased dict keys).
# OSC is an umbrella label: any computed oscillator (RSI / stochastic) counts,
# which is exactly the "do not treat generic OSC/RSI/MA as unsupported when the
# collected data carries them" carve-out.
INDICATOR_EVIDENCE_PATTERNS: Dict[str, Tuple["re.Pattern[str]", ...]] = {
    "KDJ": (re.compile(r"kdj(_[kdj])?"), re.compile(r"stoch(astic)?(_[kd])?")),
    "RSI": (re.compile(r"rsi\d{0,3}"), re.compile(r"relative_strength(_index)?")),
    "MACD": (re.compile(r"macd(_(dif|dea|hist|signal))?"), re.compile(r"(dif|dea)")),
    "BOLL": (
        re.compile(r"boll(inger)?(_(upper|middle|lower))?"),
        re.compile(r"bb_(upper|middle|lower)"),
    ),
    "ATR": (re.compile(r"atr\d{0,3}"),),
    "VWAP": (re.compile(r"vwap"),),
    "OBV": (re.compile(r"obv"),),
    "CCI": (re.compile(r"cci\d{0,3}"),),
    "ADX": (re.compile(r"adx\d{0,3}"), re.compile(r"dmi(_(plus|minus))?")),
    "WR": (re.compile(r"wr\d{0,3}"), re.compile(r"williams(_r)?")),
    "OSC": (
        re.compile(r"osc(illator)?"),
        re.compile(r"rsi\d{0,3}"),
        re.compile(r"stoch(astic)?(_[kd])?"),
    ),
    "MA": (re.compile(r"(ma|sma|ema)\d{0,3}"), re.compile(r"moving_average")),
}

# Claim-bearing card fields.  Only these are audited so that tool/QA fields
# (which legitimately *deny* having a series) are never read as claims.
CLAIM_TEXT_FIELDS = ("executive_summary", "causal_chain")
CLAIM_LIST_FIELDS = ("bull_case", "bear_case", "constraints", "risks")
CLAIM_ITEM_FIELDS = ("thesis", "claim", "evidence", "summary", "text", "note", "reason")

PE_ESTIMATOR_ORDER = ("pe_ttm", "pe_dynamic", "pe_normalized")
PEER_REFERENCE_PATTERN = re.compile(
    r"行业[^，。；\s]{0,8}(?:PE|市盈率|中位|中枢)"
    r"|industry[_\s]*(?:pe|median)"
    r"|peer",
    re.IGNORECASE,
)
MEDIAN_CLAIM_PATTERNS = (
    re.compile(r"(?:中位数|中位|中枢|median)[^0-9]{0,6}(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?)[^0-9]{0,4}(?:中位数|中位|中枢)", re.IGNORECASE),
)
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

# "Officially confirmed"-style assertions that a UNVERIFIED source cannot back.
CONFIRMATION_MARKERS = (
    "官方确认",
    "官方证实",
    "官方宣布",
    "正式确认",
    "公司确认",
    "officially confirmed",
    "officially announced",
    "confirmed by the company",
    "the company confirmed",
    "confirmed that",
)
EVENT_FACTUAL_PRECISION_LIMIT = 10


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _display_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (OSError, ValueError):
        return str(path)


def _parse_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"--date must be YYYY-MM-DD: {value!r}") from exc


def _parse_utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("US.")


def _strings(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return sorted({str(item).strip() for item in raw if str(item).strip()})


def _mean(values: Sequence[bool]) -> Optional[float]:
    return (sum(1 for item in values if item) / len(values)) if values else None


def _round_rate(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


def day_bars(symbol: str, day: str) -> Optional[List[list]]:
    """Reuse the A/B replay bar loader (tests monkeypatch this wrapper)."""
    from scripts import da_model_ab_replay as replay

    return replay.day_bars(symbol, day)


def baseline_prompt_hash() -> Optional[str]:
    """Production baseline prompt hash (``llm_synth.PROMPT_HASH``).

    Imported lazily so the report stays hermetic and never triggers a paid call
    or a config/network side effect.  Tests monkeypatch this function.
    """
    if not SKILL_SCRIPTS.is_dir():
        return None
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        import llm_synth  # type: ignore

        value = str(getattr(llm_synth, "PROMPT_HASH", "") or "").strip()
        return value or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Read-only inputs
# ---------------------------------------------------------------------------


def discover_checkpoints(checkpoint_dir: Path) -> List[Tuple[Path, str]]:
    """Return ``(analysis_path, symbol)`` for analysis checkpoints only.

    ``US.*.raw.json`` and ``*.bak`` are never report targets; the raw file is
    read separately as a metadata/user_prompt source.
    """
    if not checkpoint_dir.is_dir():
        return []
    found: List[Tuple[Path, str]] = []
    for path in sorted(checkpoint_dir.glob("US.*.json")):
        name = path.name
        if not name.endswith(".json") or name.endswith(".raw.json"):
            continue
        if ".raw." in name or name.endswith(".bak"):
            continue
        symbol = name[: -len(".json")]
        if not symbol.startswith("US.") or len(symbol) <= 3:
            continue
        found.append((path, symbol))
    return found


def latest_checkpoint_date(checkpoint_root: Path) -> Optional[str]:
    if not checkpoint_root.is_dir():
        return None
    dates: List[str] = []
    for child in checkpoint_root.iterdir():
        if not child.is_dir():
            continue
        try:
            parsed = date.fromisoformat(child.name).isoformat()
        except ValueError:
            print(f"[da_event_inject_report] skip non-date checkpoint dir: {child}",
                  file=sys.stderr)
            continue
        if discover_checkpoints(child):
            dates.append(parsed)
    return sorted(dates)[-1] if dates else None


def load_brief_latest(path: Path) -> Dict[str, Any]:
    """Read the shared brief document and collect grounded event IDs.

    Accepts either the writer's ``latest.json`` envelope (``{"brief": {...}}``)
    or a bare brief document (``{"facts": [...]}``).  Never raises for missing
    or malformed data; callers treat ``available=False`` as unverifiable.
    """
    info: Dict[str, Any] = {
        "path": _display_path(path),
        "available": False,
        "error": None,
        "brief_id": None,
        "as_of": None,
        "status": None,
        "fact_count": 0,
        "allowed_event_ids": [],
        "fact_status_counts": {},
        "event_status": {},
        "verified_event_count": 0,
        "unverified_event_count": 0,
    }
    doc = read_json(path)
    if not isinstance(doc, dict):
        info["error"] = "unreadable_or_missing"
        return info
    if isinstance(doc.get("brief"), dict):
        brief = doc["brief"]
        info["status"] = str(doc.get("status") or "").strip().upper() or None
    elif isinstance(doc.get("facts"), list):
        brief = doc
    else:
        info["error"] = "brief_missing"
        return info
    facts = brief.get("facts")
    if not isinstance(facts, list):
        info["error"] = "brief_facts_missing"
        return info
    allowed = set()
    status_counts: collections.Counter = collections.Counter()
    event_status: Dict[str, str] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        event_id = str(fact.get("event_id") or "").strip()
        if event_id:
            allowed.add(event_id)
        # Source verification tier per fact/member: today every adapter emits
        # UNVERIFIED (a T0 filing may later be CORROBORATED).  Record-only claim
        # validation compares a card's "officially confirmed" wording against it.
        fact_status = str(fact.get("status") or "").strip().upper()
        if fact_status:
            status_counts[fact_status] += 1
        if event_id and fact_status:
            event_status[event_id] = fact_status
        for member_id in fact.get("member_event_ids") or []:
            text = str(member_id or "").strip()
            if text:
                allowed.add(text)
                if fact_status:
                    event_status.setdefault(text, fact_status)
        for member in fact.get("member_facts") or []:
            if isinstance(member, dict):
                text = str(member.get("event_id") or "").strip()
                if text:
                    allowed.add(text)
                    member_status = (
                        str(member.get("status") or "").strip().upper() or fact_status
                    )
                    if member_status:
                        event_status[text] = member_status
    verified = sum(
        count for status, count in status_counts.items() if status != "UNVERIFIED"
    )
    info.update({
        "available": True,
        "brief_id": str(brief.get("brief_id") or "").strip() or None,
        "as_of": brief.get("as_of") or doc.get("as_of"),
        "fact_count": len(facts),
        "allowed_event_ids": sorted(allowed),
        "fact_status_counts": dict(sorted(status_counts.items())),
        "event_status": event_status,
        "verified_event_count": verified,
        "unverified_event_count": status_counts.get("UNVERIFIED", 0),
    })
    return info


def expected_injection_hash(ctx: Mapping[str, Any], brief_info: Mapping[str, Any]) -> str:
    """Re-derive the global injection identity from persisted fields.

    Contract copied from ``EventContextLoad.identity``: the hash covers contract
    version + enabled + injected + reason + brief_id + status + as_of +
    valid_until.  The ``enabled`` flag is not persisted in ``result_fields`` but
    is necessarily true whenever a context metadata record exists.
    """
    latest = bool(brief_info.get("available"))
    latest_as_of = _parse_utc(brief_info.get("as_of")) if latest else None
    identity = {
        "contract_version": CONTRACT_VERSION,
        "enabled": True,
        "injected": bool(ctx.get("injected")),
        "reason": ctx.get("reason"),
        "brief_id": (brief_info.get("brief_id") if latest else None) or ctx.get("brief_id"),
        "status": (
            brief_info.get("status")
            if latest and brief_info.get("status") is not None
            else ctx.get("status")
        ),
        "as_of": (latest_as_of.isoformat() if latest_as_of is not None else None)
        or ctx.get("as_of"),
        "valid_until": ctx.get("valid_until"),
    }
    return _hash_identity(identity)


def _extract_event_context(
    analysis: Mapping[str, Any], raw: Mapping[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str], List[str], bool]:
    """Locate injection metadata in any of the pipeline's persistence points."""
    candidates: List[Tuple[str, Any]] = [
        ("analysis_top_level", analysis.get("_event_context")),
        ("llm_output", (analysis.get("_llm_output") or {}).get("_event_context")
         if isinstance(analysis.get("_llm_output"), dict) else None),
        ("raw_checkpoint", raw.get("event_context") if isinstance(raw, dict) else None),
    ]
    ctx: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    sources: List[str] = []
    serialized = set()
    for name, value in candidates:
        if not isinstance(value, dict):
            continue
        sources.append(name)
        serialized.add(sha256_json(value))
        if ctx is None:
            ctx = dict(value)
            source = name
    conflict = len(serialized) > 1
    return ctx, source, sources, conflict


# ---------------------------------------------------------------------------
# Record-only claim validation (evidence quality; never a gate)
#
# Everything below only *annotates* a card.  It reads the same two documents
# ``_symbol_detail`` already loads (analysis checkpoint + raw checkpoint) plus
# the shared brief, never writes and never feeds Gate/orders.
# ---------------------------------------------------------------------------


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _claim_texts(llm: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """Return ``(field_path, text)`` for every claim-bearing card field.

    Only analyst-visible claim fields are read; free-form tool/QA fields are
    skipped so that a legit "no KDJ series available" note is never mistaken
    for a claim.
    """
    if not isinstance(llm, Mapping):
        return []
    out: List[Tuple[str, str]] = []
    for field in CLAIM_TEXT_FIELDS:
        value = llm.get(field)
        if isinstance(value, str) and value.strip():
            out.append((field, value.strip()))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, str) and item.strip():
                    out.append((f"{field}[{index}]", item.strip()))
    for field in CLAIM_LIST_FIELDS:
        value = llm.get(field)
        if isinstance(value, str):
            if value.strip():
                out.append((field, value.strip()))
            continue
        if not isinstance(value, (list, tuple)):
            continue
        for index, item in enumerate(value):
            path = f"{field}[{index}]"
            if isinstance(item, str):
                if item.strip():
                    out.append((path, item.strip()))
            elif isinstance(item, Mapping):
                for sub in CLAIM_ITEM_FIELDS:
                    text = item.get(sub)
                    if isinstance(text, str) and text.strip():
                        out.append((f"{path}.{sub}", text.strip()))
    return out


def _walk_keys(
    value: Any, path: str, keys: Dict[str, str], depth: int = 0
) -> None:
    """Collect lowercased dict keys (-> first source path) from machine data."""
    if depth > 6 or len(keys) > 4000:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).strip().lower()
            if name and name not in keys:
                keys[name] = f"{path}.{key}"
            _walk_keys(item, f"{path}.{key}", keys, depth + 1)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value[:50]):
            _walk_keys(item, f"{path}[{index}]", keys, depth + 1)


def _machine_indicator_keys(
    analysis: Mapping[str, Any], raw: Mapping[str, Any]
) -> Tuple[Dict[str, str], List[str]]:
    """Collect keys of machine-computed payloads and the sections inspected."""
    keys: Dict[str, str] = {}
    sections: List[str] = []
    collected = raw.get("collected") if isinstance(raw, Mapping) else None
    candidates = (
        ("analysis.technical", analysis.get("technical")),
        ("analysis.snapshot", analysis.get("snapshot")),
        ("raw.collected", collected),
    )
    for label, value in candidates:
        if isinstance(value, (Mapping, list, tuple)) and value:
            sections.append(label)
            _walk_keys(value, label, keys)
    return keys, sections


def _indicator_machine_series(
    keys: Mapping[str, str], indicator: str
) -> Optional[Dict[str, str]]:
    """Return the first machine-computed key that backs ``indicator``."""
    for name, path in keys.items():
        for pattern in INDICATOR_EVIDENCE_PATTERNS.get(indicator, ()):
            if pattern.fullmatch(name):
                return {"key": name, "source_field": path}
    return None


def _unsupported_indicator_records(
    claims: Sequence[Tuple[str, str]],
    machine_keys: Mapping[str, str],
    machine_sections: Sequence[str],
) -> List[Dict[str, Any]]:
    """Flag indicator claims with no machine-computed series behind them."""
    if not machine_sections:
        # Nothing was collected for this symbol -> the claim is unverifiable,
        # not false.  Stay silent rather than invent a finding.
        return []
    hits: Dict[str, List[Tuple[str, str]]] = {}
    for field, text in claims:
        for indicator, pattern in INDICATOR_CLAIM_PATTERNS:
            if pattern.search(text):
                hits.setdefault(indicator, []).append((field, text))
    records: List[Dict[str, Any]] = []
    for indicator, found in hits.items():
        if _indicator_machine_series(machine_keys, indicator) is not None:
            continue  # e.g. RSI/MA/OSC genuinely present in collected data
        records.append({
            "issue": ISSUE_UNSUPPORTED_INDICATOR,
            "indicator": indicator,
            "reason": (
                "no_implementation"
                if indicator in EXPLICITLY_UNSUPPORTED_INDICATORS
                else "no_machine_series"
            ),
            "claim_fields": [field for field, _ in found],
            "claim_text": found[0][1],
            "claim_count": len(found),
            "machine_series_present": False,
            "source_fields_checked": list(machine_sections),
            "expected_evidence_key_patterns": [
                pattern.pattern
                for pattern in INDICATOR_EVIDENCE_PATTERNS.get(indicator, ())
            ],
        })
    return records


def _pe_estimators(
    analysis: Mapping[str, Any], raw: Mapping[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Collect every disclosed PE caliber with its exact source field."""
    estimators: Dict[str, Dict[str, Any]] = {}

    def take(name: str, container: Any, key: str, label: str) -> None:
        if name in estimators or not isinstance(container, Mapping):
            return
        value = _as_number(container.get(key))
        if value is None or value <= 0:
            return
        estimators[name] = {"value": round(value, 6), "source_field": label}

    snapshot = analysis.get("snapshot") if isinstance(analysis, Mapping) else None
    collected = raw.get("collected") if isinstance(raw, Mapping) else None
    collected = collected if isinstance(collected, Mapping) else {}
    raw_snapshot = collected.get("snapshot")
    fundamentals = collected.get("fundamentals")

    take("pe_ttm", snapshot, "pe_ttm", "analysis.snapshot.pe_ttm")
    take("pe_dynamic", snapshot, "pe_ratio", "analysis.snapshot.pe_ratio")
    take(
        "pe_normalized",
        fundamentals,
        "pe_normalized",
        "raw.collected.fundamentals.pe_normalized",
    )
    take("pe_ttm", raw_snapshot, "pe_ttm_ratio", "raw.collected.snapshot.pe_ttm_ratio")
    take("pe_ttm", raw_snapshot, "pe_ttm", "raw.collected.snapshot.pe_ttm")
    take("pe_dynamic", raw_snapshot, "pe_ratio", "raw.collected.snapshot.pe_ratio")
    return estimators


def _industry_peer_sample(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarise the peer set that can actually back an industry PE median."""
    info: Dict[str, Any] = {
        "source_field": None,
        "industry": None,
        "declared_peer_count": 0,
        "listed_peer_count": 0,
        "pe_peer_count": 0,
        "peer_symbols_with_pe": [],
        "peer_pe_values": [],
        "excluded_non_positive_pe": [],
        "computed_pe_median": None,
    }
    collected = raw.get("collected") if isinstance(raw, Mapping) else None
    doc = collected.get("industry_peers") if isinstance(collected, Mapping) else None
    if not isinstance(doc, Mapping) or not doc:
        return info
    info["source_field"] = "raw.collected.industry_peers"
    info["industry"] = str(doc.get("industry") or "").strip() or None
    peers = doc.get("peers") if isinstance(doc.get("peers"), list) else []
    info["listed_peer_count"] = len(peers)
    declared = _as_number(doc.get("peer_count"))
    info["declared_peer_count"] = int(declared) if declared is not None else len(peers)
    values: List[float] = []
    for peer in peers:
        if not isinstance(peer, Mapping):
            continue
        symbol = _normalize_symbol(peer.get("symbol"))
        pe = _as_number(peer.get("pe"))
        if pe is None:
            continue
        if pe <= 0:
            info["excluded_non_positive_pe"].append(symbol or "?")
            continue
        values.append(round(pe, 6))
        info["peer_symbols_with_pe"].append(symbol)
    info["peer_pe_values"] = sorted(values)
    info["pe_peer_count"] = len(values)
    if values:
        info["computed_pe_median"] = round(statistics.median(values), 4)
    return info


def _valuation_records(
    claims: Sequence[Tuple[str, str]],
    estimators: Mapping[str, Mapping[str, Any]],
    peers: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Flag PE cherry-picking: undisclosed calibers, >25% spread, tiny peers."""
    if not estimators and not peers.get("source_field"):
        return []
    citing: List[Dict[str, Any]] = []
    for field, text in claims:
        numbers = [float(token) for token in NUMBER_PATTERN.findall(text)]
        cited = [
            name for name in PE_ESTIMATOR_ORDER
            if name in estimators
            and any(
                round(number, 2) == round(float(estimators[name]["value"]), 2)
                for number in numbers
            )
        ]
        if cited:
            citing.append({"field": field, "text": text, "cited_estimators": cited})
    peer_reference_fields = [
        (field, text) for field, text in claims if PEER_REFERENCE_PATTERN.search(text)
    ]
    if not citing and not peer_reference_fields:
        return []

    pairs: List[Dict[str, Any]] = []
    for other in ("pe_dynamic", "pe_normalized"):
        if "pe_ttm" not in estimators or other not in estimators:
            continue
        ttm = float(estimators["pe_ttm"]["value"])
        reference = float(estimators[other]["value"])
        if not reference:
            continue
        deviation = abs(ttm - reference) / abs(reference)
        pairs.append({
            "pair": f"pe_ttm_vs_{other}",
            "pe_ttm": round(ttm, 6),
            other: round(reference, 6),
            "relative_deviation": round(deviation, 6),
            "exceeds_threshold": deviation > VALUATION_DEVIATION_THRESHOLD,
        })
    max_deviation = max(
        (pair["relative_deviation"] for pair in pairs), default=None
    )
    material_deviation = (
        max_deviation is not None and max_deviation > VALUATION_DEVIATION_THRESHOLD
    )

    disclosed = sorted({name for item in citing for name in item["cited_estimators"]})
    undisclosed = [
        name for name in PE_ESTIMATOR_ORDER if name in estimators and name not in disclosed
    ]
    undisclosed_trigger = bool(citing) and bool(undisclosed)

    pe_peer_count = int(peers.get("pe_peer_count") or 0)
    small_peer_sample = (
        bool(peer_reference_fields)
        and bool(peers.get("source_field"))
        and pe_peer_count < MIN_PEER_SAMPLE
    )

    claimed_median: Optional[float] = None
    for _field, text in peer_reference_fields:
        for pattern in MEDIAN_CLAIM_PATTERNS:
            match = pattern.search(text)
            if match:
                claimed_median = round(float(match.group(1)), 4)
                break
        if claimed_median is not None:
            break

    computed_median = peers.get("computed_pe_median")
    median_matches: Optional[bool] = None
    if claimed_median is not None and computed_median is not None:
        computed_value = float(computed_median)
        median_matches = abs(claimed_median - computed_value) <= max(
            0.01, 0.005 * abs(computed_value)
        )

    triggered_by: List[str] = []
    if material_deviation:
        triggered_by.append("material_deviation")
    if undisclosed_trigger:
        triggered_by.append("undisclosed_estimators")
    if small_peer_sample:
        triggered_by.append("small_peer_sample")
    if not triggered_by:
        return []

    claim_fields: List[str] = []
    for item in citing:
        if item["field"] not in claim_fields:
            claim_fields.append(item["field"])
    for field, _text in peer_reference_fields:
        if field not in claim_fields:
            claim_fields.append(field)
    primary_text = citing[0]["text"] if citing else peer_reference_fields[0][1]

    return [{
        "issue": ISSUE_VALUATION_CONTRADICTION,
        "triggered_by": triggered_by,
        "claim_fields": claim_fields,
        "claim_text": primary_text,
        "pe_estimators": {
            name: dict(estimators[name]) for name in PE_ESTIMATOR_ORDER
            if name in estimators
        },
        "pe_pairs": pairs,
        "max_relative_deviation": max_deviation,
        "relative_deviation_threshold": VALUATION_DEVIATION_THRESHOLD,
        "disclosed_estimators": disclosed,
        "undisclosed_estimators": undisclosed,
        "industry_pe_median": {
            "claimed": claimed_median,
            "computed_from_peers": computed_median,
            "matches": median_matches,
        },
        "peer_sample": {
            "source_field": peers.get("source_field"),
            "industry": peers.get("industry"),
            "declared_peer_count": peers.get("declared_peer_count"),
            "listed_peer_count": peers.get("listed_peer_count"),
            "pe_peer_count": pe_peer_count,
            "min_required": MIN_PEER_SAMPLE,
            "peer_symbols_with_pe": list(peers.get("peer_symbols_with_pe") or []),
            "excluded_non_positive_pe": list(peers.get("excluded_non_positive_pe") or []),
        },
        "small_peer_sample": small_peer_sample,
    }]


def _factual_precision_records(
    claims: Sequence[Tuple[str, str]],
    llm_event_ids: Sequence[str],
    brief_info: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Warn when "officially confirmed" wording rests on UNVERIFIED sources.

    Only fires when *every* resolved source is UNVERIFIED — if a corroborated
    fact backs any cited event the wording may be legitimate and we stay quiet.
    """
    if not brief_info.get("available"):
        return []
    event_status = brief_info.get("event_status") or {}
    if not event_status:
        return []
    cited = [event_id for event_id in llm_event_ids if event_id in event_status]
    if cited:
        scope = "cited_events"
        resolved = {event_id: event_status[event_id] for event_id in cited}
    else:
        scope = "brief_facts"
        resolved = dict(event_status)
    if not resolved:
        return []
    unverified = [key for key, status in resolved.items() if status == "UNVERIFIED"]
    if len(unverified) != len(resolved):
        return []
    records: List[Dict[str, Any]] = []
    for field, text in claims:
        lowered = text.lower()
        marker = next((item for item in CONFIRMATION_MARKERS if item in lowered), None)
        if marker is None:
            continue
        records.append({
            "issue": ISSUE_EVENT_FACTUAL_PRECISION,
            "claim_field": field,
            "claim_text": text,
            "confirmation_marker": marker,
            "scope": scope,
            "resolved_event_count": len(resolved),
            "unverified_event_count": len(unverified),
            "source_status": "UNVERIFIED",
            "source_field": (
                "brief.facts[].status / brief.facts[].member_facts[].status"
            ),
            "brief_fact_status_counts": dict(
                brief_info.get("fact_status_counts") or {}
            ),
            "detail": (
                "claim asserts official confirmation while every resolved "
                "source is UNVERIFIED"
            ),
        })
        if len(records) >= EVENT_FACTUAL_PRECISION_LIMIT:
            break
    return records


def _evidence_quality(
    llm: Mapping[str, Any],
    analysis: Mapping[str, Any],
    raw: Mapping[str, Any],
    brief_info: Mapping[str, Any],
) -> Dict[str, Any]:
    """Assemble the record-only claim-validation block for one symbol."""
    claims = _claim_texts(llm)
    machine_keys, machine_sections = _machine_indicator_keys(analysis, raw)
    estimators = _pe_estimators(analysis, raw)
    peers = _industry_peer_sample(raw)

    machine_series: Dict[str, Dict[str, str]] = {}
    for indicator, _pattern in INDICATOR_CLAIM_PATTERNS:
        found = _indicator_machine_series(machine_keys, indicator)
        if found:
            machine_series[indicator] = found

    unsupported = _unsupported_indicator_records(
        claims, machine_keys, machine_sections
    )
    valuations = _valuation_records(claims, estimators, peers)
    factual = _factual_precision_records(
        claims, _strings(llm.get("event_ids")), brief_info
    )

    issues: List[str] = []
    for record in unsupported:
        issues.append(f"{ISSUE_UNSUPPORTED_INDICATOR}:{record['indicator']}")
    for record in valuations:
        for trigger in record.get("triggered_by") or []:
            issues.append(f"{ISSUE_VALUATION_CONTRADICTION}:{trigger}")
    for record in factual:
        issues.append(
            f"{ISSUE_EVENT_FACTUAL_PRECISION}:{record['confirmation_marker']}"
        )
    deduped: List[str] = []
    for code in issues:
        if code not in deduped:
            deduped.append(code)

    return {
        "evidence_quality_pass": not deduped,
        "evidence_quality_issues": deduped,
        "evidence_quality_data": {
            "claim_field_count": len(claims),
            "claim_fields": [field for field, _ in claims],
            "machine_sections": machine_sections,
            "machine_series": machine_series,
            "pe_estimators": estimators,
            "peer_sample": peers,
            "thresholds": {
                "valuation_relative_deviation": VALUATION_DEVIATION_THRESHOLD,
                "min_peer_sample": MIN_PEER_SAMPLE,
            },
        },
        "unsupported_indicator_claims": unsupported,
        "valuation_contradictions": valuations,
        "event_factual_precision_warnings": factual,
    }


def _symbol_detail(
    path: Path,
    symbol: str,
    *,
    checkpoint_dir: Path,
    date_et: str,
    brief_info: Mapping[str, Any],
    baseline_hash: Optional[str],
) -> Dict[str, Any]:
    """Build one per-symbol row plus its blocking/informational issues."""
    issues: List[str] = []
    blocking = False
    analysis = read_json(path)
    raw_path = checkpoint_dir / f"{symbol}.raw.json"
    raw = read_json(raw_path, default={})
    if not isinstance(raw, dict):
        raw = {}

    detail: Dict[str, Any] = {
        "symbol": symbol,
        "checkpoint_path": _display_path(path),
        "raw_checkpoint_path": _display_path(raw_path) if raw_path.is_file() else None,
        "checkpoint_readable": isinstance(analysis, dict),
        "event_context_source": None,
        "event_context_sources": [],
        "event_context_conflict": False,
        "event_context_present": False,
        "injected": False,
        "reason": "event_context_missing",
        "brief_id": None,
        "injection_hash": None,
        "event_ids": [],
        "valid_until": None,
        "llm_event_ids": [],
        "event_role": None,
        "prompt_hash": None,
        "llm_user_prompt_present": False,
        "raw_user_prompt_present": bool(str(raw.get("user_prompt") or "").strip()),
        "user_prompt_present": False,
        "direction": None,
        "score": None,
        "valid_card": False,
        "research_complete": False,
        "price_geometry_valid": None,
        "direction_plan_consistent": None,
        "reachable": None,
        "fwd": None,
        "ungrounded_event_ids": [],
        "ungrounded_context_event_ids": [],
        "evidence_quality_pass": True,
        "evidence_quality_issues": [],
        "evidence_quality_data": {},
        "unsupported_indicator_claims": [],
        "valuation_contradictions": [],
        "event_factual_precision_warnings": [],
        "checks": {},
        "issues": issues,
        "blocking": False,
    }
    if not isinstance(analysis, dict):
        issues.append("checkpoint_unreadable")
        detail["blocking"] = True
        return detail

    llm = analysis.get("_llm_output")
    llm_present = isinstance(llm, dict)
    if not llm_present:
        llm = {}
        issues.append("llm_output_missing")
        blocking = True

    ctx, source, sources, conflict = _extract_event_context(analysis, raw)
    detail["event_context_source"] = source
    detail["event_context_sources"] = sources
    detail["event_context_conflict"] = conflict
    detail["event_context_present"] = ctx is not None
    if conflict:
        issues.append("event_context_conflict")
        blocking = True

    injected = bool(ctx.get("injected")) if ctx else False
    reason = str(ctx.get("reason") or "").strip() if ctx else "event_context_missing"
    detail.update({
        "injected": injected,
        "reason": reason or "reason_missing",
        "brief_id": str(ctx.get("brief_id") or "").strip() or None if ctx else None,
        "injection_hash": str(ctx.get("injection_hash") or "").strip() or None if ctx else None,
        "event_ids": _strings(ctx.get("event_ids")) if ctx else [],
        "valid_until": str(ctx.get("valid_until") or "").strip() or None if ctx else None,
    })

    # Model-side attribution fields.
    llm_event_ids = _strings(llm.get("event_ids"))
    event_role = str(llm.get("event_role") or "NONE").strip().upper()
    prompt_hash = str(llm.get("_prompt_hash") or "").strip() or None
    llm_user_prompt = bool(str(llm.get("_user_prompt") or "").strip())
    direction = str(llm.get("direction") or "").strip().upper() or None
    if direction is None:
        fallback = str(analysis.get("action") or "").strip().upper()
        if fallback in DIRECTIONS:
            direction = fallback
    score_value = llm.get("score")
    if score_value is None:
        score_value = analysis.get("score")
    detail.update({
        "llm_event_ids": llm_event_ids,
        "event_role": event_role,
        "prompt_hash": prompt_hash,
        "llm_user_prompt_present": llm_user_prompt,
        "raw_user_prompt_present": bool(str(raw.get("user_prompt") or "").strip()),
        "user_prompt_present": llm_user_prompt or bool(str(raw.get("user_prompt") or "").strip()),
        "direction": direction,
        "score": score_value,
    })

    # Reuse the A/B replay scoring for card validity/research/geometry.
    bars = None
    try:
        bars = day_bars(symbol, date_et)
    except Exception:
        bars = None
    sc = score_card(llm if llm_present else None, bars)
    detail.update({
        "valid_card": bool(sc.get("valid_card")),
        "research_complete": bool(sc.get("research_complete")),
        "price_geometry_valid": sc.get("price_geometry_valid"),
        "direction_plan_consistent": sc.get("direction_plan_consistent"),
        "reachable": sc.get("reachable"),
        "fwd": sc.get("fwd"),
    })

    # Record-only claim validation (evidence quality).  Never flips
    # ``blocking``; findings ride on ``evidence_quality_*`` instead.
    claim_quality = _evidence_quality(llm, analysis, raw, brief_info)
    detail.update({
        "evidence_quality_pass": claim_quality["evidence_quality_pass"],
        "evidence_quality_issues": claim_quality["evidence_quality_issues"],
        "evidence_quality_data": claim_quality["evidence_quality_data"],
        "unsupported_indicator_claims": claim_quality["unsupported_indicator_claims"],
        "valuation_contradictions": claim_quality["valuation_contradictions"],
        "event_factual_precision_warnings": (
            claim_quality["event_factual_precision_warnings"]
        ),
    })
    issues.extend(claim_quality["evidence_quality_issues"])

    checks: Dict[str, Any] = {
        "event_context_present": ctx is not None,
        "brief_latest_available": bool(brief_info.get("available")),
        "brief_id_match": None,
        "as_of_match": None,
        "status_match": None,
        "injection_hash_match": None,
        "reason_is_injected": None,
        "metadata_ok": None,
        "prompt_hash_present": prompt_hash is not None,
        "prompt_hash_not_baseline": None,
        "grounding_checked": False,
        "grounding_ok": None,
        "role_event_ids_consistent": None,
    }

    if injected:
        if ctx is None:  # defensive: injected cannot be true without a context
            issues.append("event_context_missing")
            blocking = True
        reason_ok = reason == "injected"
        checks["reason_is_injected"] = reason_ok
        if not reason_ok:
            issues.append(f"reason_not_injected:{reason or 'missing'}")
            blocking = True

        metadata_ok = True
        if not brief_info.get("available"):
            issues.append("brief_latest_unavailable")
            metadata_ok = False
        else:
            brief_id = str(brief_info.get("brief_id") or "").strip() or None
            ctx_brief_id = str(ctx.get("brief_id") or "").strip() or None
            bid_match = bool(brief_id) and brief_id == ctx_brief_id
            checks["brief_id_match"] = bid_match
            if not bid_match:
                issues.append("brief_id_mismatch")
                metadata_ok = False

            latest_as_of = _parse_utc(brief_info.get("as_of"))
            ctx_as_of = _parse_utc(ctx.get("as_of"))
            as_of_match = latest_as_of is not None and ctx_as_of is not None and latest_as_of == ctx_as_of
            checks["as_of_match"] = as_of_match
            if not as_of_match:
                issues.append("as_of_mismatch")
                metadata_ok = False

            latest_status = brief_info.get("status")
            ctx_status = str(ctx.get("status") or "").strip().upper() or None
            if latest_status is None:
                checks["status_match"] = None
            else:
                status_match = ctx_status == str(latest_status).strip().upper()
                checks["status_match"] = status_match
                if not status_match:
                    issues.append("brief_status_mismatch")
                    metadata_ok = False

            expected_hash = expected_injection_hash(ctx, brief_info)
            stored_hash = str(ctx.get("injection_hash") or "").strip()
            hash_match = bool(stored_hash) and stored_hash == expected_hash
            checks["injection_hash_match"] = hash_match
            if not stored_hash:
                issues.append("injection_hash_missing")
                metadata_ok = False
            elif not hash_match:
                issues.append("injection_hash_mismatch")
                metadata_ok = False

        checks["metadata_ok"] = metadata_ok
        if not metadata_ok:
            blocking = True

        # Prompt hash must prove the injection path actually changed the prompt.
        if prompt_hash is None:
            issues.append("prompt_hash_missing")
            checks["prompt_hash_not_baseline"] = False
            blocking = True
        elif baseline_hash is None:
            issues.append("baseline_prompt_hash_unavailable")
            checks["prompt_hash_not_baseline"] = None
            blocking = True
        elif prompt_hash == baseline_hash:
            issues.append("prompt_hash_baseline_when_injected")
            checks["prompt_hash_not_baseline"] = False
            blocking = True
        else:
            checks["prompt_hash_not_baseline"] = True

    # Event role / event ID attribution contract (checked even without injection,
    # but only injected samples can make the canary fail on it).
    role_ok = True
    if event_role not in EVENT_ROLES:
        issues.append(f"invalid_event_role:{event_role or 'missing'}")
        role_ok = False
    if event_role == "NONE" and llm_event_ids:
        issues.append("role_none_with_event_ids")
        role_ok = False
    if event_role != "NONE" and not llm_event_ids:
        issues.append("event_role_requires_event_ids")
        role_ok = False
    checks["role_event_ids_consistent"] = role_ok

    ungrounded: List[str] = []
    if brief_info.get("available"):
        allowed = set(brief_info.get("allowed_event_ids") or [])
        ungrounded = sorted(set(llm_event_ids) - allowed)
        checks["grounding_checked"] = True
        checks["grounding_ok"] = role_ok and not ungrounded
        if ungrounded:
            for event_id in ungrounded:
                issues.append(f"ungrounded_event:{event_id}")
        if not role_ok:
            blocking = True
        if ungrounded:
            blocking = True
    else:
        # Without the brief we cannot prove grounding; injected samples fail
        # closed rather than passing unverified.
        if injected:
            issues.append("grounding_unverified")
            blocking = True
        checks["grounding_ok"] = None if not injected else False

    detail["ungrounded_event_ids"] = ungrounded
    context_ids = set(detail["event_ids"])
    if brief_info.get("available"):
        allowed = set(brief_info.get("allowed_event_ids") or [])
        detail["ungrounded_context_event_ids"] = sorted(context_ids - allowed)

    detail["checks"] = checks
    detail["blocking"] = blocking
    return detail


# ---------------------------------------------------------------------------
# Optional control (da_model_ab_replay JSONL / manifest)
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    records: List[Dict[str, Any]] = []
    errors = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 1
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            errors += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            errors += 1
    return records, errors


def load_control(path: Path, input_mode: Optional[str] = None) -> Dict[str, Any]:
    """Parse one explicit model/input arm; never mix v3 replay input modes."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"control file not found: {p}")
    manifest: Optional[Dict[str, Any]] = None
    records: List[Dict[str, Any]] = []
    formats: List[str] = []

    if p.suffix == ".jsonl":
        records, errors = _read_jsonl(p)
        formats.append("jsonl")
        if errors and not records:
            raise ValueError(f"control JSONL has no readable records: {p}")
        side = p.with_suffix(".manifest.json")
        if side.is_file():
            obj = read_json(side)
            if isinstance(obj, dict):
                manifest = obj
                formats.append("manifest")
    else:
        obj = read_json(p)
        if isinstance(obj, dict) and isinstance(obj.get("summary"), dict):
            manifest = obj
            formats.append("manifest")
        elif isinstance(obj, list):
            records = [item for item in obj if isinstance(item, dict)]
            formats.append("json")
        elif isinstance(obj, dict):
            records = [obj]
            formats.append("json")
        else:
            raise ValueError(f"control file is not JSON/JSONL: {p}")
        if not records:
            if p.name.endswith(".manifest.json"):
                side = p.with_name(p.name[: -len(".manifest.json")] + ".jsonl")
            else:
                side = p.with_suffix(".jsonl")
            if side.is_file():
                records, errors = _read_jsonl(side)
                if errors and not records:
                    raise ValueError(f"control JSONL has no readable records: {side}")
                formats.append("jsonl")

    production_model = None
    if manifest:
        settings = manifest.get("production_settings")
        if isinstance(settings, dict):
            production_model = str(settings.get("model") or "").strip() or None

    available_modes = {str(record.get("input_mode")) for record in records if record.get("input_mode")}
    if manifest and isinstance(manifest.get("input_modes"), list):
        available_modes.update(str(mode) for mode in manifest["input_modes"] if mode)
    if input_mode and records and not available_modes:
        raise ValueError("control records do not declare input_mode; cannot select an input arm")
    if input_mode and available_modes and input_mode not in available_modes:
        raise ValueError(f"control input mode {input_mode} not in {sorted(available_modes)}")
    if not input_mode:
        if len(available_modes) == 1:
            input_mode = next(iter(available_modes))
        elif len(available_modes) > 1:
            raise ValueError(
                "control contains multiple input modes; pass --control-input-mode explicitly"
            )

    rated_rows = records
    if production_model and records:
        rated_rows = [
            record for record in records
            if str(record.get("model") or "") == production_model
        ]
    if input_mode:
        rated_rows = [record for record in rated_rows
                      if record.get("input_mode") == input_mode]

    valid_rate: Optional[float] = None
    research_rate: Optional[float] = None
    record_count = len(rated_rows)
    if rated_rows:
        valid_rate = _mean([bool(record.get("valid_card")) for record in rated_rows])
        rc_present = [
            record for record in rated_rows
            if record.get("research_complete") is not None
        ]
        research_rate = (
            _mean([bool(record.get("research_complete")) for record in rc_present])
            if rc_present else None
        )
    elif manifest:
        summary = manifest.get("summary") or {}
        row: Optional[Dict[str, Any]] = None
        summary_key = f"{production_model}|{input_mode}" if production_model and input_mode else production_model
        if summary_key and isinstance(summary.get(summary_key), dict):
            row = summary[summary_key]
        elif not input_mode and isinstance(summary, dict) and summary:
            first_key = sorted(summary)[0]
            if isinstance(summary.get(first_key), dict):
                row = summary[first_key]
        if row:
            valid_rate = row.get("valid_card_rate")
            research_rate = row.get("research_complete_rate")
            record_count = int(row.get("n") or 0)
    if record_count <= 0:
        raise ValueError(
            "control selection has zero usable records for "
            f"model={production_model or 'any'} input_mode={input_mode or 'legacy_untagged'}"
        )

    day_symbol_dirs: Dict[Tuple[str, str], set] = collections.defaultdict(set)
    symbol_dirs: Dict[str, set] = collections.defaultdict(set)
    for record in rated_rows:
        symbol = _normalize_symbol(record.get("symbol"))
        direction = str(record.get("direction") or "").strip().upper()
        if not symbol or direction not in DIRECTIONS:
            continue
        day = str(record.get("day") or "").strip()
        symbol_dirs[symbol].add(direction)
        if day:
            day_symbol_dirs[(day, symbol)].add(direction)

    return {
        "path": _display_path(p),
        "format": "+".join(dict.fromkeys(formats)),
        "production_model": production_model, "input_mode": input_mode,
        "record_count": record_count,
        "valid_card_rate": _round_rate(valid_rate),
        "research_complete_rate": _round_rate(research_rate),
        "day_symbol_dirs": day_symbol_dirs,
        "symbol_dirs": symbol_dirs,
    }


def _compare_control(
    details: Sequence[Mapping[str, Any]],
    control: Optional[Mapping[str, Any]],
    *,
    date_et: str,
) -> Dict[str, Any]:
    if not control:
        return {"enabled": False, "pass": True, "threshold_pp": CONTROL_DROP_PP}

    total = len(details)
    canary_valid = _mean([bool(detail.get("valid_card")) for detail in details]) if total else None
    canary_research = (
        _mean([bool(detail.get("research_complete")) for detail in details]) if total else None
    )
    checks: Dict[str, Any] = {}
    for name, canary_value, control_value in (
        ("valid_card", canary_valid, control.get("valid_card_rate")),
        ("research_complete", canary_research, control.get("research_complete_rate")),
    ):
        if control_value is None:
            checks[name] = {"applicable": False, "pass": True}
            continue
        delta_pp = round((float(canary_value or 0.0) - float(control_value)) * 100.0, 4)
        checks[name] = {
            "applicable": True,
            "canary_rate": _round_rate(canary_value),
            "control_rate": _round_rate(control_value),
            "delta_pp": delta_pp,
            "pass": delta_pp >= -CONTROL_DROP_PP - 1e-9,
        }

    day_symbol_dirs = control.get("day_symbol_dirs") or {}
    symbol_dirs = control.get("symbol_dirs") or {}
    compared = 0
    matched = 0
    mismatches: List[Dict[str, Any]] = []
    for detail in details:
        symbol = _normalize_symbol(detail.get("symbol"))
        direction = str(detail.get("direction") or "").strip().upper()
        if not direction or direction not in DIRECTIONS:
            continue
        scope = None
        control_dirs = day_symbol_dirs.get((date_et, symbol))
        if control_dirs:
            scope = "day_symbol"
        elif symbol_dirs.get(symbol):
            scope = "symbol"
            control_dirs = symbol_dirs.get(symbol)
        if not control_dirs:
            continue
        compared += 1
        if direction in control_dirs:
            matched += 1
        else:
            mismatches.append({
                "symbol": detail.get("symbol"),
                "canary_direction": direction,
                "control_directions": sorted(control_dirs),
                "scope": scope,
            })
    return {
        "enabled": True,
        "path": control.get("path"),
        "format": control.get("format"),
        "production_model": control.get("production_model"),
        "input_mode": control.get("input_mode"),
        "record_count": control.get("record_count"),
        "threshold_pp": CONTROL_DROP_PP,
        "rates": checks,
        "direction_compared": compared,
        "direction_matched": matched,
        "direction_match_rate": _round_rate(matched / compared if compared else None),
        "direction_mismatches": mismatches,
        "pass": all(item.get("pass", True) for item in checks.values()),
    }


# ---------------------------------------------------------------------------
# Report assembly / idempotent outputs
# ---------------------------------------------------------------------------


def _build_report(
    *,
    checkpoint_dir: Path,
    date_et: str,
    brief_path: Path,
    control_path: Optional[Path],
    baseline_hash: Optional[str],
    control_input_mode: Optional[str] = None,
) -> Dict[str, Any]:
    samples = discover_checkpoints(checkpoint_dir)
    if not samples:
        raise ValueError(f"no DA analysis checkpoints under {checkpoint_dir}")

    brief_info = load_brief_latest(brief_path)
    details = [
        _symbol_detail(
            path,
            symbol,
            checkpoint_dir=checkpoint_dir,
            date_et=date_et,
            brief_info=brief_info,
            baseline_hash=baseline_hash,
        )
        for path, symbol in samples
    ]

    total = len(details)
    injected_count = sum(1 for detail in details if detail.get("injected"))
    missing_context_count = sum(1 for detail in details if not detail.get("event_context_present"))
    skipped_reasons = collections.Counter(
        str(detail.get("reason") or "reason_missing")
        for detail in details
        if not detail.get("injected")
    )
    directions = collections.Counter(
        str(detail.get("direction") or "UNKNOWN") for detail in details
    )
    directional = [
        detail for detail in details
        if str(detail.get("direction") or "").upper() in {"BUY", "SELL_SHORT"}
    ]
    valid_cards = [detail for detail in details if detail.get("valid_card")]

    control = None
    if control_path is not None:
        control = load_control(control_path, input_mode=control_input_mode)
    control_comparison = _compare_control(details, control, date_et=date_et)

    canary_issues: List[str] = []
    if injected_count == 0:
        canary_issues.append("no_injected_samples")
    if injected_count > 0 and missing_context_count > 0:
        canary_issues.append("mixed_event_context_states")
    for detail in details:
        if detail.get("blocking"):
            # Evidence-quality codes share the per-symbol ``issues`` list but
            # are never canary issues (even on an already-blocking symbol).
            claim_codes = set(detail.get("evidence_quality_issues") or [])
            for issue in detail.get("issues") or []:
                if issue in claim_codes:
                    continue
                canary_issues.append(f"{detail.get('symbol')}:{issue}")
    if control_comparison.get("enabled") and not control_comparison.get("pass"):
        canary_issues.append("control_rate_drop_above_5pp")

    # Record-only claim findings stay out of ``canary_pass``: RFC-208 pass
    # semantics (metadata / prompt hash / grounding / control rates) are
    # unchanged.  They only drive ``evidence_quality_pass`` and the WARN status.
    evidence_quality_issues: List[str] = []
    for detail in details:
        for code in detail.get("evidence_quality_issues") or []:
            evidence_quality_issues.append(f"{detail.get('symbol')}:{code}")

    canary_pass = not canary_issues
    evidence_quality_pass = not evidence_quality_issues
    status = "OK" if (canary_pass and evidence_quality_pass) else "WARN"

    metrics = {
        "injected_count": injected_count,
        "total": total,
        "skipped": total - injected_count,
        "directions": dict(sorted(directions.items())),
        "skipped_reasons": dict(sorted(skipped_reasons.items())),
        "valid_card_rate": _round_rate(_mean([bool(d.get("valid_card")) for d in details])),
        "research_complete_rate": _round_rate(
            _mean([bool(d.get("research_complete")) for d in details])
        ),
        "direction_rate": _round_rate(len(directional) / total if total else None),
        "price_geometry_rate": _round_rate(
            _mean([bool(d.get("price_geometry_valid")) for d in directional])
            if directional else None
        ),
        "direction_plan_consistent_rate": _round_rate(
            _mean([bool(d.get("direction_plan_consistent")) for d in valid_cards])
            if valid_cards else None
        ),
        "ungrounded_event_count": sum(
            len(detail.get("ungrounded_event_ids") or []) for detail in details
        ),
        "role_none_with_event_ids_count": sum(
            1 for detail in details if "role_none_with_event_ids" in (detail.get("issues") or [])
        ),
        "unsupported_claim_count": sum(
            len(detail.get("unsupported_indicator_claims") or []) for detail in details
        ),
        "valuation_contradiction_count": sum(
            len(detail.get("valuation_contradictions") or []) for detail in details
        ),
        "event_factual_precision_count": sum(
            len(detail.get("event_factual_precision_warnings") or [])
            for detail in details
        ),
        "evidence_quality_fail_symbol_count": sum(
            1 for detail in details if not detail.get("evidence_quality_pass", True)
        ),
    }

    content: Dict[str, Any] = {
        "schema": SCHEMA,
        "date": date_et,
        "checkpoint_dir": _display_path(checkpoint_dir),
        "brief_latest": {
            "path": brief_info.get("path"),
            "available": brief_info.get("available"),
            "error": brief_info.get("error"),
            "brief_id": brief_info.get("brief_id"),
            "as_of": brief_info.get("as_of"),
            "status": brief_info.get("status"),
            "fact_count": brief_info.get("fact_count"),
            "allowed_event_id_count": len(brief_info.get("allowed_event_ids") or []),
            "fact_status_counts": dict(brief_info.get("fact_status_counts") or {}),
            "verified_event_count": brief_info.get("verified_event_count"),
            "unverified_event_count": brief_info.get("unverified_event_count"),
        },
        "baseline_prompt_hash": baseline_hash,
        "symbols": details,
        "metrics": metrics,
        "control": control_comparison,
        "canary_pass": canary_pass,
        "canary_issues": canary_issues,
        "evidence_quality_pass": evidence_quality_pass,
        "evidence_quality_issues": evidence_quality_issues,
        "claim_validation": {
            "record_only": True,
            "gate_visible": False,
            "affects_canary_pass": False,
            "issue_types": list(EVIDENCE_ISSUE_CODES),
            "thresholds": {
                "valuation_relative_deviation": VALUATION_DEVIATION_THRESHOLD,
                "min_peer_sample": MIN_PEER_SAMPLE,
            },
        },
        "status": status,
        "issues": canary_issues + evidence_quality_issues,
        "units": UNITS,
        "economic_value_claimed": False,
        "production_write": False,
        "paid_calls": 0,
        "db_writes": False,
    }
    content["content_hash"] = sha256_json(content)
    return content


def write_report(
    content: Dict[str, Any],
    *,
    out_root: Path,
    latest_path: Path,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write immutable history + atomically update latest (idempotent)."""
    now = now or _now_utc()
    now_text = _iso(now)
    date_et = str(content["date"])
    content_hash = str(content["content_hash"])
    artifact_path = out_root / date_et / f"{content_hash}.json"

    if artifact_path.exists():
        existing = read_json(artifact_path)
        if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
            raise ValueError(
                f"immutable artifact exists with different payload (no overwrite): {artifact_path}"
            )
        generated_at = str(existing.get("generated_at") or now_text)
    else:
        artifact = dict(content)
        artifact.update({
            "generated_at": now_text,
            "check_count": 1,
            "writer": WRITER,
        })
        try:
            write_json_new(artifact_path, artifact)
            generated_at = now_text
        except FileExistsError:
            existing = read_json(artifact_path)
            if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
                raise ValueError(
                    f"immutable artifact appeared with different payload: {artifact_path}"
                )
            generated_at = str(existing.get("generated_at") or now_text)

    previous = read_json(latest_path)
    if (
        isinstance(previous, dict)
        and previous.get("content_hash") == content_hash
        and previous.get("date") == date_et
    ):
        check_count = int(previous.get("check_count") or 1) + 1
    else:
        check_count = 1

    latest = dict(content)
    latest.update({
        "generated_at": generated_at,
        "last_checked_at": now_text,
        "check_count": check_count,
        "artifact_path": _display_path(artifact_path),
        "writer": WRITER,
    })
    atomic_write_json(latest_path, latest)
    return latest


# ---------------------------------------------------------------------------
# --eval (read-only)
# ---------------------------------------------------------------------------


def evaluate(latest_path: Path, *, expected_date: Optional[str] = None) -> Dict[str, Any]:
    """Read an existing latest report and return OK/WARN/ERROR without writing."""
    base: Dict[str, Any] = {
        "schema": "da_event_prompt_canary_eval_v1",
        "latest_path": _display_path(latest_path),
        "economic_value_claimed": False,
        "production_write": False,
    }
    if not Path(latest_path).is_file():
        return {**base, "status": "ERROR", "reason": "latest_missing", "canary_pass": False}
    doc = read_json(latest_path)
    if not isinstance(doc, dict):
        return {**base, "status": "ERROR", "reason": "latest_unreadable", "canary_pass": False}

    issues: List[str] = []
    if doc.get("schema") != SCHEMA:
        issues.append("latest_schema_mismatch")
    if expected_date and doc.get("date") != expected_date:
        issues.append(f"date_mismatch:{doc.get('date')}")
    if doc.get("economic_value_claimed") is not False:
        issues.append("economic_value_claimed_not_false")
    if doc.get("production_write") is not False:
        issues.append("production_write_not_false")
    stored_status = str(doc.get("status") or "").strip().upper()
    if stored_status not in {"OK", "WARN", "ERROR"}:
        issues.append(f"latest_status_invalid:{stored_status or 'missing'}")
    if stored_status == "ERROR":
        issues.append("latest_status_error")

    artifact_exists = None
    artifact_value = str(doc.get("artifact_path") or "").strip()
    if not artifact_value:
        issues.append("artifact_path_missing")
    else:
        artifact = Path(artifact_value)
        if not artifact.is_absolute():
            artifact = ROOT / artifact
        artifact_exists = artifact.is_file()
        if not artifact_exists:
            issues.append("artifact_missing")

    canary_pass = doc.get("canary_pass") is True
    # Record-only claim findings never change ``canary_pass``; a stored report
    # without the key predates this check and is treated as passing.
    evidence_quality_pass = doc.get("evidence_quality_pass") is not False
    if issues:
        status = "ERROR"
    elif canary_pass and evidence_quality_pass:
        status = "OK"
    else:
        status = "WARN"
    return {
        **base,
        "status": status,
        "latest_status": stored_status or None,
        "canary_pass": canary_pass,
        "evidence_quality_pass": evidence_quality_pass,
        "evidence_quality_issues": list(doc.get("evidence_quality_issues") or []),
        "date": doc.get("date"),
        "content_hash": doc.get("content_hash"),
        "generated_at": doc.get("generated_at"),
        "last_checked_at": doc.get("last_checked_at"),
        "check_count": doc.get("check_count"),
        "artifact_path": doc.get("artifact_path"),
        "artifact_exists": artifact_exists,
        "metrics": doc.get("metrics"),
        "issues": issues or list(doc.get("issues") or []),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EventContext prompt-injection canary report (read-only, zero paid calls)",
    )
    parser.add_argument("--date", default="",
                        help="ET checkpoint date YYYY-MM-DD (default: latest available)")
    parser.add_argument("--control", default="",
                        help="optional da_model_ab_replay JSONL or manifest JSON")
    parser.add_argument("--control-input-mode", choices=("frozen", "full", "legacy"),
                        default=None, help="v3 多输入臂必须显式选择，禁止混算")
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT,
                        help="canary output root (latest.json + <date>/<content_hash>.json)")
    parser.add_argument("--latest-path", type=Path, default=None,
                        help="latest.json path (default: <out-root>/latest.json)")
    parser.add_argument("--eval", action="store_true",
                        help="read --latest-path and emit OK/WARN/ERROR without writing")
    parser.add_argument("--checkpoint-root", type=Path, default=DEFAULT_CHECKPOINT_ROOT,
                        help="DA checkpoint root (read-only)")
    parser.add_argument("--brief-latest", type=Path, default=DEFAULT_LATEST_PATH,
                        help="shared EventContext brief latest.json (read-only)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    latest_path = Path(args.latest_path) if args.latest_path else Path(args.out_root) / "latest.json"
    try:
        date_et = _parse_date(args.date) if args.date else None
    except ValueError as exc:
        report = {
            "schema": SCHEMA,
            "status": "ERROR",
            "error_type": "ValueError",
            "error": str(exc),
            "canary_pass": False,
            "economic_value_claimed": False,
            "production_write": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    if args.eval:
        report = evaluate(latest_path, expected_date=date_et)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return {"OK": 0, "WARN": 1, "ERROR": 2}.get(str(report.get("status")), 2)

    try:
        resolved_date = date_et or latest_checkpoint_date(Path(args.checkpoint_root))
        if not resolved_date:
            raise ValueError(
                f"no checkpoint date found under {args.checkpoint_root}; pass --date"
            )
        checkpoint_dir = Path(args.checkpoint_root) / resolved_date
        content = _build_report(
            checkpoint_dir=checkpoint_dir,
            date_et=resolved_date,
            brief_path=Path(args.brief_latest),
            control_path=Path(args.control) if args.control else None,
            baseline_hash=baseline_prompt_hash(),
            control_input_mode=args.control_input_mode,
        )
        report = write_report(content, out_root=Path(args.out_root), latest_path=latest_path)
    except Exception as exc:  # noqa: BLE001 - report must fail visibly, never write
        report = {
            "schema": SCHEMA,
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "canary_pass": False,
            "economic_value_claimed": False,
            "production_write": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
