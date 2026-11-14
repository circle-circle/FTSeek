#!/usr/bin/env python3
"""RFC-207/208 · DA 执行前 EventContext 新鲜度/生命周期 record-only 影子。

回答的唯一问题
==============
「现在 `data/shadow/event_intelligence/live/latest.json` 里的共享简报，与上次真正
注入 DA 生产 Prompt 的 treatment（`data/shadow/da_event_prompt_canary/latest.json`）
相比，是否仍然是同一份？」

  · 同一 `brief_id` + 同一 injection 内容哈希 ⇒ `NO_CHANGE`；
  · 有效但不同的简报 ⇒ `REVIEW`，并列出 old/new brief_id、变化的 transmission /
    theme ID 以及受影响的 canary 标的；
  · 当前 latest 缺失/无效/超龄/时间戳非法或未来，或 treatment 缺失/无效 ⇒
    `REVIEW_INVALID_BRIEF` + 机器可读原因，**绝不静默当作新鲜**。

边界（刻意不做的事）
====================
  · 不修订/不失效任何简报，不改 Gate/仓位/订单/playbook/config/cron；
  · 不计算 PnL 或反事实收益；
  · 不写生产 DB；只写两处证据：
      `data/shadow/da_event_preexecute/latest.json`（原子更新）
      `data/shadow/da_event_preexecute/<ET 日期>/<content_hash>.json`（O_EXCL 不可变）
  · 相同输入重跑 ⇒ 同一 content_hash / 同一 artifact，只递增 `check_count`。

injection 内容哈希口径
======================
canary 每行 `injection_hash` 是生产注入身份（`da_event_prompt_injection_v1`：
contract_version + enabled + injected + reason + brief_id + status + as_of +
valid_until）。本影子对当前 latest 复用同一生产 loader 重新计算，因此比较的是
**同一契约**下的身份哈希，而不是另造一套。

brief v4 没有持久化的 transmission_id / theme_id，故本影子用内容寻址派生：
  · theme_id       = sha256(casefold(规范化 theme 文本))[:12]
  · transmission_id= sha256(规范化 transmission 对象)[:12]
任一字段变化 ⇒ 旧 ID 消失、新 ID 出现，均计入 `changed_transmission_ids`。

开关与付费闸
============
  · `DA_EVENT_PREEXECUTE_SHADOW`（缺省 false，每次实读，走 `shadow_runtime.env_flag`）。
    `DA_EVENTPREEXECUTE_SHADOW` 仅作向后兼容 alias。
    false ⇒ 打印 `DISABLED` 并返回 0；除读开关外不读任何文件、不建目录、不写文件。
  · 本脚本自身**永不**调用 LLM；只有显式 `--execute-refresh` 才会先调用
    `scripts/da_event_context_shadow.py --allow-no-checkpoint --execute`，且必须
    **同时**满足 `DA_EVENT_PREEXECUTE_SHADOW=1` 与 `DA_EVENT_CONTEXT_SHADOW=1`。
    未传 `--execute-refresh` ⇒ 零付费调用。

CLI
===
    ./venv/bin/python scripts/da_event_preexecute_shadow.py
    ./venv/bin/python scripts/da_event_preexecute_shadow.py --execute-refresh
    ./venv/bin/python scripts/da_event_preexecute_shadow.py --now 2026-11-13T13:35:00Z

退出码：DISABLED / NO_CHANGE = 0；REVIEW = 1；REVIEW_INVALID_BRIEF = 2。

评估器：`scripts/da_event_preexecute_shadow_eval.py`。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.shadow_runtime import (  # noqa: E402
    atomic_write_json,
    env_flag,
    read_json,
    sha256_json,
    write_json_new,
)
from scripts.da_event_context_shadow import (  # noqa: E402
    REPORT_SCHEMA as CURRENT_REPORT_SCHEMA,
    validate_brief_record,
)

#: Canonical switch.  The legacy/typo variant is accepted as an alias so an
#: operator who writes either spelling still fails closed only when both are off.
ENV_FLAG = "DA_EVENT_PREEXECUTE_SHADOW"
ENV_FLAG_ALIASES = ("DA_EVENTPREEXECUTE_SHADOW",)
CONTEXT_FLAG = "DA_EVENT_CONTEXT_SHADOW"

REPORT_SCHEMA = "da_event_preexecute_shadow_latest_v1"
CANARY_SCHEMA = "da_event_prompt_canary_v1"
WRITER = "scripts/da_event_preexecute_shadow.py"
DEFAULT_LATEST = ROOT / "data" / "shadow" / "event_intelligence" / "live" / "latest.json"
DEFAULT_CANARY_LATEST = ROOT / "data" / "shadow" / "da_event_prompt_canary" / "latest.json"
DEFAULT_HISTORY_ROOT = ROOT / "data" / "shadow" / "event_intelligence" / "live"
DEFAULT_OUT_ROOT = ROOT / "data" / "shadow" / "da_event_preexecute"
DEFAULT_MAX_AGE_HOURS = 26.0
FUTURE_TOLERANCE_SECONDS = 300.0
MAX_HISTORY_SCAN = 500
ET = ZoneInfo("America/New_York")

STATUS_NO_CHANGE = "NO_CHANGE"
STATUS_REVIEW = "REVIEW"
STATUS_INVALID = "REVIEW_INVALID_BRIEF"
STATUS_DISABLED = "DISABLED"
WRITER_STATUSES = frozenset(
    {STATUS_NO_CHANGE, STATUS_REVIEW, STATUS_INVALID, STATUS_DISABLED}
)
VALID_CURRENT_STATUSES = frozenset({"OK", "ALREADY_CURRENT"})

#: Fields that describe the run, not the evidence.  Excluding them keeps
#: same-input reruns on the same content hash (and therefore the same immutable
#: artifact) even though wall-clock age advances.
VOLATILE_FIELDS = frozenset(
    {
        "generated_at",
        "last_checked_at",
        "check_count",
        "artifact_path",
        "content_hash",
        "writer",
        "age_hours",
    }
)

CAVEAT = (
    "record-only 执行前观测: 不修订/不失效简报; 不改 Gate/仓位/订单/playbook/config/cron; "
    "不写生产 DB; 不含 PnL 或反事实收益; 变化主题/标的是待人工复核的研究线索。"
)


# ---------------------------------------------------------------------------
# 时间 / 路径 / 开关
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; naive/invalid values return ``None``."""
    if value in (None, ""):
        return None
    text = str(value).strip()
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


def _display_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (OSError, ValueError):
        return str(path)


def _enabled() -> bool:
    if env_flag(ENV_FLAG):
        return True
    return any(env_flag(alias) for alias in ENV_FLAG_ALIASES)


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text if text.startswith("US.") else f"US.{text}"


# ---------------------------------------------------------------------------
# 输入：当前 latest / treatment canary
# ---------------------------------------------------------------------------


def _load_current(
    path: Path,
    *,
    now: datetime,
    max_age_hours: float,
) -> Dict[str, Any]:
    """Read + validate the current shared brief latest.  Never raises."""
    result: Dict[str, Any] = {
        "path": str(path),
        "doc": None,
        "brief": None,
        "errors": [],
        "age_hours": None,
    }
    if not Path(path).is_file():
        result["errors"] = ["current_latest_missing"]
        return result
    doc = read_json(Path(path))
    if not isinstance(doc, dict):
        result["errors"] = ["current_latest_unreadable"]
        return result
    result["doc"] = doc
    errors: List[str] = []

    if doc.get("schema") != CURRENT_REPORT_SCHEMA:
        errors.append(f"current_schema_mismatch:{doc.get('schema')}")
    if doc.get("record_only") is not True:
        errors.append("current_record_only_not_true")

    status = str(doc.get("status") or "").strip().upper()
    if status not in VALID_CURRENT_STATUSES:
        errors.append(f"current_status_invalid:{status or 'missing'}")

    stored_errors = doc.get("validation_errors")
    if not isinstance(stored_errors, list):
        errors.append("current_validation_errors_missing")
    elif stored_errors:
        errors.append("current_validation_errors_present")

    generated_at = parse_iso_utc(doc.get("generated_at"))
    if generated_at is None:
        errors.append("current_generated_at_invalid")
    else:
        age_hours = round((now - generated_at).total_seconds() / 3600.0, 3)
        result["age_hours"] = age_hours
        if age_hours < -(FUTURE_TOLERANCE_SECONDS / 3600.0):
            errors.append("current_generated_at_in_future")
        if age_hours > max_age_hours:
            errors.append(f"current_latest_stale:{age_hours}h>{max_age_hours}h")

    brief = doc.get("brief")
    if not isinstance(brief, dict):
        errors.append("current_brief_missing")
    else:
        result["brief"] = brief
        errors.extend(
            f"brief:{item}"
            for item in validate_brief_record(brief, date_et=doc.get("date_et"))
        )
        brief_as_of = parse_iso_utc(brief.get("as_of"))
        if brief_as_of is not None:
            if brief_as_of > now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
                errors.append("current_brief_as_of_in_future")

    result["errors"] = sorted(set(errors))
    return result


def _load_canary(path: Path) -> Dict[str, Any]:
    """Read + validate the injected-treatment canary report.  Never raises."""
    result: Dict[str, Any] = {
        "path": str(path),
        "doc": None,
        "errors": [],
        "brief_id": None,
        "as_of": None,
        "injection_hash": None,
        "valid_until": None,
        "content_hash": None,
        "date": None,
        "symbols": [],
        "injected_rows": [],
    }
    if not Path(path).is_file():
        result["errors"] = ["canary_latest_missing"]
        return result
    doc = read_json(Path(path))
    if not isinstance(doc, dict):
        result["errors"] = ["canary_latest_unreadable"]
        return result
    result["doc"] = doc
    errors: List[str] = []

    if doc.get("schema") != CANARY_SCHEMA:
        errors.append(f"canary_schema_mismatch:{doc.get('schema')}")

    brief_latest = doc.get("brief_latest")
    if not isinstance(brief_latest, dict):
        errors.append("canary_brief_latest_missing")
    else:
        if brief_latest.get("available") is not True:
            errors.append("canary_brief_unavailable")
        brief_id = str(brief_latest.get("brief_id") or "").strip()
        if not brief_id:
            errors.append("canary_brief_id_missing")
        else:
            result["brief_id"] = brief_id
        result["as_of"] = brief_latest.get("as_of")

    rows = doc.get("symbols")
    if not isinstance(rows, list):
        errors.append("canary_symbols_missing")
        rows = []
    injected_rows = [
        row for row in rows
        if isinstance(row, dict) and row.get("injected") is True
    ]
    result["injected_rows"] = injected_rows
    result["symbols"] = sorted({
        symbol
        for symbol in (_normalize_symbol(row.get("symbol")) for row in rows if isinstance(row, dict))
        if symbol
    })

    if not injected_rows:
        errors.append("canary_no_injected_symbols")

    injection_hashes = [
        str(row.get("injection_hash") or "").strip() for row in injected_rows
    ]
    # An injected row without a hash is not a usable treatment identity: fail
    # closed instead of letting the remaining rows define the comparison.
    if any(not value for value in injection_hashes):
        errors.append("canary_injection_hash_missing")
    unique_hashes = sorted({value for value in injection_hashes if value})
    if not unique_hashes:
        if "canary_injection_hash_missing" not in errors:
            errors.append("canary_injection_hash_missing")
    elif len(unique_hashes) > 1:
        errors.append("canary_injection_hash_inconsistent")
    else:
        result["injection_hash"] = unique_hashes[0]

    valid_untils = sorted({
        str(row.get("valid_until") or "").strip()
        for row in injected_rows
        if str(row.get("valid_until") or "").strip()
    })
    if len(valid_untils) == 1:
        result["valid_until"] = valid_untils[0]

    result["content_hash"] = doc.get("content_hash")
    result["date"] = doc.get("date")
    result["errors"] = sorted(set(errors))
    return result


def _current_injection_hash(
    latest_path: Path,
    *,
    now: datetime,
    max_age_hours: float,
) -> Tuple[Optional[str], str]:
    """Re-derive the production injection identity for the current latest."""
    from app.event_stream.da_prompt_injection import load_event_context

    load = load_event_context(
        latest_path=Path(latest_path),
        now=now,
        environ={"DA_EVENT_CONTEXT_INJECT": "1"},
        max_age_hours=max_age_hours,
    )
    if not load.injected:
        return None, str(load.reason or "not_injected")
    value = str(load.injection_hash or "").strip()
    if not value:
        return None, "injection_hash_missing"
    return value, "injected"


# ---------------------------------------------------------------------------
# 变化对比
# ---------------------------------------------------------------------------


def _theme_id(theme: Any) -> str:
    normalized = " ".join(str(theme or "").split()).casefold()
    return sha256_json({"theme": normalized})[:12]


def _transmission_records(brief: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive content-addressed transmission IDs (brief v4 has none)."""
    records: Dict[str, Dict[str, Any]] = {}
    for item in brief.get("transmissions") or []:
        if not isinstance(item, dict):
            continue
        transmission_id = sha256_json(item)[:12]
        theme = str(item.get("theme") or "").strip()
        records[transmission_id] = {
            "transmission_id": transmission_id,
            "theme_id": _theme_id(theme),
            "theme": theme,
            "sign": item.get("sign"),
            "horizon": item.get("horizon"),
            "event_ids": sorted(str(value) for value in item.get("event_ids") or []),
            "loser_symbols": [str(value) for value in item.get("loser_symbols") or []],
            "beneficiary_symbols": [
                str(value) for value in item.get("beneficiary_symbols") or []
            ],
        }
    return [records[key] for key in sorted(records)]


def _theme_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    themes = {
        record["theme_id"]: {
            "theme_id": record["theme_id"],
            "theme": record["theme"],
        }
        for record in records
    }
    return [themes[key] for key in sorted(themes)]


def _find_previous_brief(
    *,
    history_root: Path,
    brief_id: Optional[str],
    current_doc: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Locate the brief that the canary actually treated (immutable history)."""
    if not brief_id:
        return None, "previous_brief_id_missing"
    if isinstance(current_doc, dict):
        current_brief = current_doc.get("brief")
        if (
            isinstance(current_brief, dict)
            and str(current_brief.get("brief_id") or "").strip() == brief_id
        ):
            return current_brief, "current_latest"
    root = Path(history_root)
    if not root.is_dir():
        return None, "previous_brief_unavailable"
    for path in sorted(root.glob("*/*.json"), reverse=True)[:MAX_HISTORY_SCAN]:
        doc = read_json(path)
        if not isinstance(doc, dict):
            continue
        brief = doc.get("brief")
        if isinstance(brief, dict) and str(brief.get("brief_id") or "").strip() == brief_id:
            return brief, f"history:{_display_path(path)}"
    return None, "previous_brief_unavailable"


def _compute_diff(
    *,
    previous_brief: Dict[str, Any],
    current_brief: Dict[str, Any],
    canary: Dict[str, Any],
) -> Dict[str, Any]:
    previous = _transmission_records(previous_brief)
    current = _transmission_records(current_brief)
    previous_by_id = {record["transmission_id"]: record for record in previous}
    current_by_id = {record["transmission_id"]: record for record in current}
    previous_ids = set(previous_by_id)
    current_ids = set(current_by_id)

    added = [current_by_id[key] for key in sorted(current_ids - previous_ids)]
    removed = [previous_by_id[key] for key in sorted(previous_ids - current_ids)]
    changed_transmission_ids = sorted(previous_ids ^ current_ids)

    previous_themes = {record["theme_id"]: record for record in _theme_records(previous)}
    current_themes = {record["theme_id"]: record for record in _theme_records(current)}
    added_themes = [
        current_themes[key] for key in sorted(set(current_themes) - set(previous_themes))
    ]
    removed_themes = [
        previous_themes[key] for key in sorted(set(previous_themes) - set(current_themes))
    ]
    # A theme can survive by name while its transmission content changes.  Report
    # both set-difference themes and themes touched by any added/removed
    # transmission, so "changed theme" is not silently narrowed to "new theme".
    changed_theme_ids = sorted(
        (set(previous_themes) ^ set(current_themes))
        | {
            record["theme_id"]
            for record in added + removed
            if record["theme_id"]
        }
    )

    changed_event_ids = {
        event_id
        for record in added + removed
        for event_id in record["event_ids"]
    }
    named_symbols = {
        _normalize_symbol(symbol)
        for record in added + removed
        for symbol in record["loser_symbols"] + record["beneficiary_symbols"]
    }
    grounded_symbols = set()
    for row in canary.get("injected_rows") or []:
        row_ids = {str(value) for value in row.get("event_ids") or []}
        if row_ids & changed_event_ids:
            symbol = _normalize_symbol(row.get("symbol"))
            if symbol:
                grounded_symbols.add(symbol)

    canary_symbols = set(canary.get("symbols") or [])
    affected = sorted((named_symbols | grounded_symbols) & canary_symbols)

    return {
        "diff_available": True,
        "previous_transmission_ids": sorted(previous_ids),
        "current_transmission_ids": sorted(current_ids),
        "changed_transmission_ids": changed_transmission_ids,
        "previous_theme_ids": sorted(previous_themes),
        "current_theme_ids": sorted(current_themes),
        "changed_theme_ids": changed_theme_ids,
        "added_transmissions": added,
        "removed_transmissions": removed,
        "added_themes": added_themes,
        "removed_themes": removed_themes,
        "unchanged_transmission_count": len(previous_ids & current_ids),
        "affected_canary_symbols": affected,
    }


def _empty_diff(reason: str) -> Dict[str, Any]:
    return {
        "diff_available": False,
        "diff_reason": reason,
        "previous_transmission_ids": [],
        "current_transmission_ids": [],
        "changed_transmission_ids": [],
        "previous_theme_ids": [],
        "current_theme_ids": [],
        "changed_theme_ids": [],
        "added_transmissions": [],
        "removed_transmissions": [],
        "added_themes": [],
        "removed_themes": [],
        "unchanged_transmission_count": 0,
        "affected_canary_symbols": [],
    }


# ---------------------------------------------------------------------------
# 可选：先刷新简报（受双开关闸）
# ---------------------------------------------------------------------------


def _execute_refresh(args: argparse.Namespace) -> Dict[str, Any]:
    """Run the paid EventContext refresh only when explicitly double-gated."""
    result: Dict[str, Any] = {
        "requested": bool(args.execute_refresh),
        "allowed": False,
        "attempted": False,
        "returncode": None,
        "reason": "not_requested",
        "command": None,
        "timeout_seconds": int(args.refresh_timeout),
    }
    if not args.execute_refresh:
        return result
    if not (_enabled() and env_flag(CONTEXT_FLAG)):
        result["reason"] = "requires_flags"
        return result

    result["allowed"] = True
    command = [
        sys.executable,
        str(ROOT / "scripts" / "da_event_context_shadow.py"),
        "--allow-no-checkpoint",
        "--execute",
    ]
    result["command"] = command
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=args.refresh_timeout,
        )
    except subprocess.TimeoutExpired:
        result.update({"attempted": True, "reason": "timeout"})
        return result
    except OSError as exc:
        result.update(
            {"attempted": True, "reason": "spawn_error", "error": type(exc).__name__}
        )
        return result

    result["attempted"] = True
    result["returncode"] = int(completed.returncode)
    result["reason"] = "completed" if completed.returncode == 0 else "nonzero_exit"
    return result


# ---------------------------------------------------------------------------
# 内容 / 落盘
# ---------------------------------------------------------------------------


def _partition_date(
    *,
    current_doc: Optional[Dict[str, Any]],
    canary: Dict[str, Any],
    now: datetime,
) -> str:
    candidates = []
    if isinstance(current_doc, dict):
        candidates.append(current_doc.get("date_et"))
    candidates.append(canary.get("date"))
    for value in candidates:
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except (AttributeError, TypeError, ValueError):
            print(f"[preexecute] skip invalid date candidate={value!r}", file=sys.stderr)
            continue
    return now.astimezone(ET).date().isoformat()


def build_content(
    *,
    latest_path: Path,
    canary_path: Path,
    history_root: Path,
    now: datetime,
    max_age_hours: float,
    refresh: Dict[str, Any],
) -> Dict[str, Any]:
    current = _load_current(latest_path, now=now, max_age_hours=max_age_hours)
    canary = _load_canary(canary_path)

    current_brief = current.get("brief")
    current_brief_id = (
        str(current_brief.get("brief_id") or "").strip()
        if isinstance(current_brief, dict) else None
    ) or None
    previous_brief_id = canary.get("brief_id")

    current_hash: Optional[str] = None
    identity_error: Optional[str] = None
    if not current.get("errors"):
        current_hash, identity_reason = _current_injection_hash(
            latest_path, now=now, max_age_hours=max_age_hours,
        )
        if current_hash is None:
            identity_error = f"current_injection_identity_invalid:{identity_reason}"

    current_errors = list(current.get("errors") or [])
    canary_errors = list(canary.get("errors") or [])
    all_errors = sorted(set(current_errors + canary_errors + ([identity_error] if identity_error else [])))

    previous_hash = canary.get("injection_hash")
    same = bool(
        previous_brief_id
        and current_brief_id
        and previous_brief_id == current_brief_id
        and previous_hash
        and current_hash
        and previous_hash == current_hash
    )

    reasons: List[str] = []
    if all_errors:
        status = STATUS_INVALID
        if current_errors or identity_error:
            reasons.append("current_latest_invalid")
        if canary_errors:
            reasons.append("canary_treatment_invalid")
    elif same:
        status = STATUS_NO_CHANGE
        reasons.append("brief_and_injection_unchanged")
    else:
        status = STATUS_REVIEW
        if previous_brief_id != current_brief_id:
            reasons.append("brief_id_changed")
        else:
            reasons.append("injection_identity_changed")

    previous_brief = None
    diff_source = "previous_brief_id_missing"
    if previous_brief_id and isinstance(current_brief, dict):
        previous_brief, diff_source = _find_previous_brief(
            history_root=history_root,
            brief_id=previous_brief_id,
            current_doc=current.get("doc"),
        )
    if isinstance(previous_brief, dict) and isinstance(current_brief, dict):
        diff = _compute_diff(
            previous_brief=previous_brief,
            current_brief=current_brief,
            canary=canary,
        )
        diff["diff_reason"] = "available"
        diff["previous_brief_source"] = diff_source
    else:
        diff = _empty_diff(diff_source)
        diff["previous_brief_source"] = diff_source
        if status == STATUS_REVIEW and "previous_brief_unavailable" not in reasons:
            reasons.append("previous_brief_unavailable")

    previous_as_of = canary.get("as_of")
    current_as_of = current_brief.get("as_of") if isinstance(current_brief, dict) else None
    previous_as_of_dt = parse_iso_utc(previous_as_of)
    current_as_of_dt = parse_iso_utc(current_as_of)
    if (
        status == STATUS_REVIEW
        and previous_as_of_dt is not None
        and current_as_of_dt is not None
        and current_as_of_dt < previous_as_of_dt
    ):
        reasons.append("current_brief_older_than_treatment")

    paid_calls = 0
    current_status = str((current.get("doc") or {}).get("status") or "").upper()
    if (refresh.get("attempted") and isinstance(current_brief, dict)
            and current_status != "ALREADY_CURRENT"):
        llm_calls = current_brief.get("llm_calls")
        if isinstance(llm_calls, int) and not isinstance(llm_calls, bool) and llm_calls >= 0:
            paid_calls = llm_calls

    content: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": status,
        "date_et": _partition_date(
            current_doc=current.get("doc"), canary=canary, now=now,
        ),
        "current_latest_path": _display_path(latest_path),
        "canary_latest_path": _display_path(canary_path),
        "history_root": _display_path(history_root),
        "current_brief_id": current_brief_id,
        "previous_brief_id": previous_brief_id,
        "current_as_of": current_as_of,
        "previous_as_of": previous_as_of,
        "current_injection_hash": current_hash,
        "previous_injection_hash": previous_hash,
        "previous_valid_until": canary.get("valid_until"),
        "canary_content_hash": canary.get("content_hash"),
        "canary_symbol_count": len(canary.get("symbols") or []),
        "canary_symbols": list(canary.get("symbols") or []),
        "age_hours": current.get("age_hours"),
        "max_age_hours": max_age_hours,
        "reasons": sorted(set(reasons)),
        "validation_errors": all_errors,
        "refresh": refresh,
        "record_only": True,
        "advisory_only": True,
        "gate_visible": False,
        "economic_value_claimed": False,
        "pnl_included": False,
        "production_writes": False,
        "db_writes": False,
        "paid_calls": paid_calls,
        "caveat": CAVEAT,
        **diff,
    }
    return content


def write_report(
    content: Dict[str, Any],
    *,
    out_root: Path,
    now: datetime,
) -> Dict[str, Any]:
    """Write immutable history + atomically replace latest (idempotent)."""
    full = dict(content)
    hash_body = {key: value for key, value in full.items() if key not in VOLATILE_FIELDS}
    content_hash = sha256_json(hash_body)
    date_et = str(content.get("date_et") or now.astimezone(ET).date().isoformat())
    artifact_path = Path(out_root) / date_et / f"{content_hash}.json"

    generated_at: str
    if artifact_path.exists():
        existing = read_json(artifact_path)
        if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
            raise ValueError(
                f"immutable artifact exists with different payload (no overwrite): {artifact_path}"
            )
        generated_at = str(existing.get("generated_at") or iso_utc(now))
    else:
        artifact = dict(full)
        artifact.update(
            {
                "content_hash": content_hash,
                "generated_at": iso_utc(now),
                "check_count": 1,
                "writer": WRITER,
            }
        )
        try:
            write_json_new(artifact_path, artifact)
            generated_at = iso_utc(now)
        except FileExistsError:
            existing = read_json(artifact_path)
            if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
                raise ValueError(
                    f"immutable artifact appeared with different payload: {artifact_path}"
                )
            generated_at = str(existing.get("generated_at") or iso_utc(now))

    latest_path = Path(out_root) / "latest.json"
    previous = read_json(latest_path)
    if (
        isinstance(previous, dict)
        and previous.get("content_hash") == content_hash
        and previous.get("date_et") == date_et
    ):
        check_count = int(previous.get("check_count") or 1) + 1
    else:
        check_count = 1

    latest = dict(full)
    latest.update(
        {
            "content_hash": content_hash,
            "generated_at": generated_at,
            "last_checked_at": iso_utc(now),
            "check_count": check_count,
            "artifact_path": _display_path(artifact_path),
            "writer": WRITER,
        }
    )
    atomic_write_json(latest_path, latest)
    return latest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DA 执行前 EventContext 新鲜度/生命周期 record-only 影子",
    )
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST,
                        help="当前共享简报 latest.json")
    parser.add_argument("--canary-latest", type=Path, default=DEFAULT_CANARY_LATEST,
                        help="已注入 treatment 的 canary latest.json")
    parser.add_argument("--history-root", type=Path, default=DEFAULT_HISTORY_ROOT,
                        help="不可变简报历史根目录 (按 brief_id 回查旧简报)")
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT,
                        help="本影子产物目录 (latest.json + <date>/<hash>.json)")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help="当前 latest 超过该小时数即 REVIEW_INVALID_BRIEF (默认 26h)")
    parser.add_argument("--now", default="",
                        help="固定当前时刻 (UTC ISO，必须带时区；用于回放/测试)")
    parser.add_argument("--execute-refresh", action="store_true",
                        help="先调用 da_event_context_shadow.py --allow-no-checkpoint --execute "
                             "(需同时满足两个 env flag)")
    parser.add_argument("--refresh-timeout", type=int, default=1200,
                        help="刷新子进程超时秒数 (默认 1200)")
    parser.add_argument("--json", action="store_true", help="兼容开关；输出始终是 JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Disabled path: no data reads, no directory creation, no writes.
    if not _enabled():
        print(json.dumps(
            {
                "schema": REPORT_SCHEMA,
                "status": STATUS_DISABLED,
                "reason": "flag_disabled",
                "env_flag": ENV_FLAG,
                "latest_path": _display_path(args.latest),
                "canary_latest_path": _display_path(args.canary_latest),
                "record_only": True,
                "production_writes": False,
                "caveat": CAVEAT,
            },
            ensure_ascii=False, indent=2, sort_keys=True,
        ))
        return 0

    if args.now:
        now = parse_iso_utc(args.now)
        if now is None:
            print(json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "status": STATUS_INVALID,
                    "reason": "bad_cli",
                    "error": "--now requires an ISO timestamp with timezone",
                    "record_only": True,
                    "production_writes": False,
                },
                ensure_ascii=False, indent=2, sort_keys=True,
            ))
            return 2
    else:
        now = now_utc()

    refresh = _execute_refresh(args)
    content = build_content(
        latest_path=args.latest,
        canary_path=args.canary_latest,
        history_root=args.history_root,
        now=now,
        max_age_hours=args.max_age_hours,
        refresh=refresh,
    )
    latest = write_report(content, out_root=args.out_root, now=now)
    print(json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True))
    return {
        STATUS_DISABLED: 0,
        STATUS_NO_CHANGE: 0,
        STATUS_REVIEW: 1,
        STATUS_INVALID: 2,
    }.get(str(latest.get("status")), 2)


if __name__ == "__main__":
    raise SystemExit(main())