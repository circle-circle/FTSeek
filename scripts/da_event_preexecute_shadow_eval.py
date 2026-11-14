#!/usr/bin/env python3
"""DA 执行前 EventContext 影子 · 只读评估器。

唯一读入口：`data/shadow/da_event_preexecute/latest.json`
（写方 `scripts/da_event_preexecute_shadow.py`）。

只回答「执行前新鲜度结论是否可读、是否仍新鲜」：

  OK       写方结论 NO_CHANGE，且报告时间戳有效、未超龄。
  REVIEW   写方结论 REVIEW / REVIEW_INVALID_BRIEF（需要人工复核）。
  NO_DATA  latest 不存在。
  ERROR    latest 不可读 / schema 不符 / status 未知 / 时间戳无效或未来 / 超龄。

本评估器**不**重新比较简报，也不读 canary；它只审计写方产物本身。
只读：不写 latest、不写 artifact、不调 LLM、不碰生产 DB、不算 PnL。

退出码：OK = 0；REVIEW / NO_DATA = 1；ERROR = 2。

用法：
    ./venv/bin/python scripts/da_event_preexecute_shadow_eval.py
    ./venv/bin/python scripts/da_event_preexecute_shadow_eval.py --max-age-hours 48
    ./venv/bin/python scripts/da_event_preexecute_shadow_eval.py --latest /tmp/latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.da_event_preexecute_shadow import (  # noqa: E402
    CAVEAT,
    DEFAULT_OUT_ROOT,
    FUTURE_TOLERANCE_SECONDS,
    REPORT_SCHEMA,
    WRITER_STATUSES,
    _display_path,
    now_utc,
    parse_iso_utc,
)
from scripts.shadow_runtime import read_json  # noqa: E402

DEFAULT_LATEST = DEFAULT_OUT_ROOT / "latest.json"
DEFAULT_MAX_AGE_HOURS = 36.0
STATUSES = ("OK", "REVIEW", "NO_DATA", "ERROR")
REVIEW_WRITER_STATUSES = frozenset({"REVIEW", "REVIEW_INVALID_BRIEF"})


def _base(status: str, latest_path: Path) -> Dict[str, Any]:
    return {
        "status": status,
        "latest_path": _display_path(latest_path),
        "record_only": True,
        "caveat": CAVEAT,
        "allowed_statuses": list(STATUSES),
    }


def _snapshot_fields(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "writer_status": doc.get("status"),
        "report_generated_at": doc.get("generated_at"),
        "observed_age_hours": doc.get("age_hours"),
        "current_brief_id": doc.get("current_brief_id"),
        "previous_brief_id": doc.get("previous_brief_id"),
        "current_injection_hash": doc.get("current_injection_hash"),
        "previous_injection_hash": doc.get("previous_injection_hash"),
        "changed_transmission_ids": doc.get("changed_transmission_ids") or [],
        "changed_theme_ids": doc.get("changed_theme_ids") or [],
        "affected_canary_symbols": doc.get("affected_canary_symbols") or [],
        "reasons": doc.get("reasons") or [],
        "validation_errors": doc.get("validation_errors") or [],
        "content_hash": doc.get("content_hash"),
        "artifact_path": doc.get("artifact_path"),
    }


def evaluate(
    latest_path: Path,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Read the writer's latest report and return a read-only status."""
    now = now or now_utc()
    latest_path = Path(latest_path)
    if not latest_path.is_file():
        return {**_base("NO_DATA", latest_path), "reason": "latest_missing"}

    doc = read_json(latest_path)
    if not isinstance(doc, dict):
        return {**_base("ERROR", latest_path), "reason": "latest_unreadable"}

    errors = []
    if doc.get("schema") != REPORT_SCHEMA:
        errors.append(f"latest_schema_mismatch:{doc.get('schema')}")

    generated_at = parse_iso_utc(doc.get("generated_at"))
    age_hours: Optional[float] = None
    if generated_at is None:
        errors.append("generated_at_invalid")
    else:
        age_hours = round((now - generated_at).total_seconds() / 3600.0, 3)
        if age_hours < -(FUTURE_TOLERANCE_SECONDS / 3600.0):
            errors.append("generated_at_in_future")
        if age_hours > max_age_hours:
            errors.append(f"latest_stale:{age_hours}h>{max_age_hours}h")

    writer_status = str(doc.get("status") or "").strip().upper()
    if writer_status not in WRITER_STATUSES:
        errors.append(f"latest_status_invalid:{writer_status or 'missing'}")

    fields = _snapshot_fields(doc)
    if errors:
        return {
            **_base("ERROR", latest_path),
            **fields,
            "reason": "validation_failed",
            "errors": sorted(set(errors)),
            "age_hours": age_hours,
            "max_age_hours": max_age_hours,
        }

    if writer_status == "NO_CHANGE":
        status, reason = "OK", "unchanged"
    elif writer_status in REVIEW_WRITER_STATUSES:
        status, reason = "REVIEW", writer_status.lower()
    elif writer_status == "DISABLED":
        status, reason = "ERROR", "report_disabled"
    else:  # pragma: no cover - guarded by WRITER_STATUSES above
        status, reason = "ERROR", "latest_status_invalid"

    return {
        **_base(status, latest_path),
        **fields,
        "reason": reason,
        "age_hours": age_hours,
        "max_age_hours": max_age_hours,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DA 执行前 EventContext 影子 latest 只读评估器 (无 PnL)",
    )
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help="报告超过该小时数即 ERROR (默认 36h)")
    parser.add_argument("--now", default="",
                        help="固定当前时刻 (UTC ISO，必须带时区；用于回放/测试)")
    parser.add_argument("--json", action="store_true", help="兼容开关；输出始终是 JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    now = now_utc()
    if args.now:
        parsed = parse_iso_utc(args.now)
        if parsed is None:
            print(json.dumps(
                {
                    **_base("ERROR", args.latest),
                    "reason": "bad_cli",
                    "error": "--now requires an ISO timestamp with timezone",
                },
                ensure_ascii=False, indent=2, sort_keys=True,
            ))
            return 2
        now = parsed
    report = evaluate(args.latest, max_age_hours=args.max_age_hours, now=now)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return {"OK": 0, "REVIEW": 1, "NO_DATA": 1}.get(str(report.get("status")), 2)


if __name__ == "__main__":
    raise SystemExit(main())