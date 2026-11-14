"""Switch-gated EventContext injection into DA prompts (RFC-208).

Purpose
-------
Feed the shared intraday EventContext brief (written by
``scripts/da_event_context_shadow.py`` to
``data/shadow/event_intelligence/live/latest.json``) into the production DA
prompt **only when explicitly enabled** by ``DA_EVENT_CONTEXT_INJECT``
(default ``false``).

Safety contract
---------------
* Default off => this module returns an ``enabled=False`` record without reading
  the brief.  DA behaves byte-identically (prompt / hash / checkpoint policy).
* Any missing/invalid/stale brief => ``injected=False`` plus an explicit
  ``reason``.  Callers must treat failures as "no injection", never as a DA
  blocker.
* Consumers are the raw checkpoint and the DA quality evaluator only.  Gate /
  order / position sizing code must not read ``_event_context``.
* Because this is direct production prompt injection, result metadata is
  ``advisory_only=True`` / ``record_only=False`` (not a record-only shadow).
* The persisted ``injection_hash`` is the *global* injection identity
  (contract version + brief_id + as_of + valid_until + outcome).  Writer status
  flips between ``OK`` and ``ALREADY_CURRENT`` do not change content identity.
  It is deliberately symbol-independent so the pipeline checkpoint policy can
  include exactly the same value and invalidate old baseline caches when the
  injection changes.  Per-symbol content is covered separately by ``section_hash``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from app.event_stream.instructions import compact_symbol_event_section

ENV_FLAG = "DA_EVENT_CONTEXT_INJECT"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env.us"
DEFAULT_LATEST_PATH = REPO_ROOT / "data" / "shadow" / "event_intelligence" / "live" / "latest.json"
MAX_AGE_HOURS = 26.0
VALID_STATUSES = frozenset({"OK", "ALREADY_CURRENT"})
CONTRACT_VERSION = "da_event_prompt_injection_v1"
FUTURE_TOLERANCE_SECONDS = 300.0
ET = ZoneInfo("America/New_York")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Offline replay wording is not production-compatible.  Keep the compact
#: section builder (single source for facts/transmissions shrinkage) but replace
#: only that sentence; no field contract is changed.
_OFFLINE_ONLY_SENTENCE = "本次仅为离线日内研究。"
_PRODUCTION_SENTENCE = "本次为生产日内分析。"


def _env_file_flag(name: str, env_file: Path) -> Optional[str]:
    """Exact ``scripts.shadow_runtime.env_flag`` file fallback (fail closed)."""
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, raw = text.split("=", 1)
            if key.strip() == name:
                return raw.strip()
    except OSError:
        return None
    return None


def injection_enabled(
    environ: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
) -> bool:
    """Return True only for explicit truthy flag values (default false).

    DA cron runs without systemd env injection, so an unset process env var is
    resolved from ``.env.us`` — same precedence as ``shadow_runtime.env_flag``
    (process env > file > default false).  Unknown values fail closed.
    """
    if environ is not None:
        value = environ.get(ENV_FLAG)
    else:
        value = os.environ.get(ENV_FLAG)
        if value is None:
            value = _env_file_flag(ENV_FLAG, env_file or DEFAULT_ENV_FILE)
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_VALUES


def _parse_aware(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def valid_until_et(now: Optional[datetime] = None) -> str:
    """Current ET date at 16:00, ISO-8601 with explicit offset."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    et_now = current.astimezone(ET)
    expiry = et_now.replace(hour=16, minute=0, second=0, microsecond=0)
    return expiry.isoformat()


def _hash_identity(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:12]


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class EventContextLoad:
    """Result of reading + validating the shared brief."""

    enabled: bool
    injected: bool
    reason: str
    latest_path: str
    brief_id: Optional[str] = None
    status: Optional[str] = None
    as_of: Optional[str] = None
    valid_until: Optional[str] = None
    brief: Optional[Dict[str, Any]] = field(default=None, repr=False)
    injection_hash: str = ""

    def identity(self) -> Dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "enabled": self.enabled,
            "injected": self.injected,
            "reason": self.reason,
            "brief_id": self.brief_id,
            "as_of": self.as_of,
            "valid_until": self.valid_until,
        }

    def result_fields(self) -> Dict[str, Any]:
        """Fields persisted into ``result['_event_context']``.

        ``consumers`` / ``gate_visible`` register the allowed read scope for
        downstream auditors: checkpoint + quality evaluator only, never Gate.
        """
        return {
            "schema": "da_event_prompt_injection_v1",
            "injected": self.injected,
            "reason": self.reason,
            "brief_id": self.brief_id,
            "status": self.status,
            "as_of": self.as_of,
            "valid_until": self.valid_until,
            "injection_hash": self.injection_hash,
            "latest_path": self.latest_path,
            "consumers": ["raw_checkpoint", "quality_evaluator"],
            "gate_visible": False,
            # This is direct production prompt injection (advisory input), not a
            # record-only shadow artifact.
            "advisory_only": True,
            "record_only": False,
        }


@dataclass
class EventContextInjection:
    """Result of producing a symbol-specific prompt section."""

    load: EventContextLoad
    injected: bool
    reason: str
    section: str = ""
    event_ids: List[str] = field(default_factory=list)
    section_hash: str = ""

    @property
    def brief_id(self) -> Optional[str]:
        return self.load.brief_id

    @property
    def status(self) -> Optional[str]:
        return self.load.status

    @property
    def as_of(self) -> Optional[str]:
        return self.load.as_of

    @property
    def valid_until(self) -> Optional[str]:
        return self.load.valid_until

    @property
    def injection_hash(self) -> str:
        return self.load.injection_hash

    def result_fields(self) -> Dict[str, Any]:
        fields = self.load.result_fields()
        fields.update({
            "injected": self.injected,
            "reason": self.reason,
            "event_ids": list(self.event_ids),
            "section_hash": self.section_hash,
        })
        return fields


def _load_error(
    reason: str,
    latest_path: Path,
    *,
    enabled: bool,
    valid_until: Optional[str],
) -> EventContextLoad:
    load = EventContextLoad(
        enabled=enabled,
        injected=False,
        reason=reason,
        latest_path=str(latest_path),
        valid_until=valid_until,
    )
    load.injection_hash = _hash_identity(load.identity())
    return load


def load_event_context(
    *,
    now: Optional[datetime] = None,
    latest_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
    max_age_hours: float = MAX_AGE_HOURS,
) -> EventContextLoad:
    """Read and validate ``latest.json``.  Never raises for data-quality issues."""
    enabled = injection_enabled(environ, env_file)
    path = Path(latest_path) if latest_path is not None else DEFAULT_LATEST_PATH
    expiry = valid_until_et(now)
    if not enabled:
        return EventContextLoad(
            enabled=False,
            injected=False,
            reason="flag_disabled",
            latest_path=str(path),
            valid_until=expiry,
            injection_hash="",
        )

    if not path.exists():
        return _load_error("latest_missing", path, enabled=True, valid_until=expiry)
    try:
        with path.open(encoding="utf-8") as handle:
            doc = json.load(handle)
    except Exception as exc:  # unreadable / malformed JSON
        return _load_error(
            f"latest_unreadable:{type(exc).__name__}", path, enabled=True, valid_until=expiry,
        )
    if not isinstance(doc, dict):
        return _load_error("latest_not_object", path, enabled=True, valid_until=expiry)

    if doc.get("record_only") is not True:
        return _load_error("record_only_not_true", path, enabled=True, valid_until=expiry)
    status = str(doc.get("status") or "").strip().upper()
    if status not in VALID_STATUSES:
        return _load_error(
            f"status_not_ok:{status or 'missing'}", path, enabled=True, valid_until=expiry,
        )
    if not isinstance(doc.get("validation_errors"), list) or doc.get("validation_errors"):
        return _load_error("latest_validation_errors", path, enabled=True, valid_until=expiry)

    brief = doc.get("brief")
    if not isinstance(brief, dict):
        return _load_error("brief_missing", path, enabled=True, valid_until=expiry)
    if (not isinstance(brief.get("validation_errors"), list)
            or brief.get("validation_errors")):
        return _load_error("brief_validation_errors", path, enabled=True, valid_until=expiry)
    brief_id = str(brief.get("brief_id") or "").strip()
    if not brief_id:
        return _load_error("brief_id_missing", path, enabled=True, valid_until=expiry)

    as_of_raw = brief.get("as_of") or doc.get("as_of")
    as_of_dt = _parse_aware(as_of_raw)
    if as_of_dt is None:
        return _load_error("brief_as_of_invalid", path, enabled=True, valid_until=expiry)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current.astimezone(timezone.utc) - as_of_dt
    if as_of_dt.astimezone(ET).date() != current.astimezone(ET).date():
        return _load_error("brief_date_mismatch", path, enabled=True, valid_until=expiry)
    if age < timedelta(seconds=-FUTURE_TOLERANCE_SECONDS):
        return _load_error("brief_as_of_future", path, enabled=True, valid_until=expiry)
    if age > timedelta(hours=max_age_hours):
        return _load_error("brief_stale", path, enabled=True, valid_until=expiry)

    load = EventContextLoad(
        enabled=True,
        injected=True,
        reason="injected",
        latest_path=str(path),
        brief_id=brief_id,
        status=status,
        as_of=as_of_dt.isoformat(),
        valid_until=expiry,
        brief=brief,
    )
    load.injection_hash = _hash_identity(load.identity())
    return load


def _event_ids_from_payload(payload: Dict[str, Any]) -> List[str]:
    ids = set()
    for fact in payload.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        event_id = fact.get("event_id")
        if event_id:
            ids.add(str(event_id))
        for member in fact.get("member_facts") or []:
            if isinstance(member, dict) and member.get("event_id"):
                ids.add(str(member["event_id"]))
    return sorted(ids)


def _section_payload(section: str) -> Optional[Dict[str, Any]]:
    start = section.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(section[start:])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _relevance_text(collected: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "symbol": collected.get("symbol"),
            "company_profile": collected.get("company_profile"),
            "plates": collected.get("plates"),
        },
        ensure_ascii=False,
        default=str,
    )


def build_event_context_injection(
    collected: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    latest_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
) -> EventContextInjection:
    """Validate the brief and build the symbol-specific prompt section.

    Returns ``injected=False`` with an explicit reason for flag-off, missing,
    stale, or invalid brief data.  Never raises for those cases and never
    blocks DA.
    """
    load = load_event_context(
        now=now, latest_path=latest_path, environ=environ, env_file=env_file,
    )
    if not load.enabled:
        return EventContextInjection(load=load, injected=False, reason=load.reason)
    if not load.injected or not load.brief:
        return EventContextInjection(load=load, injected=False, reason=load.reason)

    valid_until = load.valid_until or valid_until_et(now)
    symbol = str(collected.get("symbol") or "")
    try:
        section = compact_symbol_event_section(
            load.brief,
            valid_until=valid_until,
            symbol=symbol,
            relevance_text=_relevance_text(collected),
        )
    except Exception as exc:
        return EventContextInjection(
            load=load, injected=False, reason=f"section_build_failed:{type(exc).__name__}",
        )
    section = section.replace(_OFFLINE_ONLY_SENTENCE, _PRODUCTION_SENTENCE)
    payload = _section_payload(section)
    if payload is None:
        return EventContextInjection(
            load=load, injected=False, reason="section_unparseable",
        )
    return EventContextInjection(
        load=load,
        injected=True,
        reason="injected",
        section=section,
        event_ids=_event_ids_from_payload(payload),
        section_hash=_hash_text(section),
    )


def event_injection_policy_hash(
    *,
    now: Optional[datetime] = None,
    latest_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
) -> Optional[str]:
    """Checkpoint-policy hash, or ``None`` when the flag is off.

    ``None`` keeps the pipeline policy payload byte-identical to the historical
    (pre-injection) value, so old baseline caches stay valid while the feature
    is disabled.  When enabled, the returned value is exactly the
    ``injection_hash`` persisted in ``result['_event_context']`` — including the
    "enabled but no valid brief" state, so a later valid brief cannot silently
    reuse a pre-injection checkpoint.
    """
    load = load_event_context(
        now=now, latest_path=latest_path, environ=environ, env_file=env_file,
    )
    return load.injection_hash or None