#!/usr/bin/env python3
"""RFC-207 §2 · EventContext 影子只读评估器

唯一读入口：`data/shadow/event_intelligence/live/latest.json`
（写方 `scripts/da_event_context_shadow.py`）。

只回答「这份共享简报是否还活着、是否自洽、覆盖多少证据」：
  - 状态 / 新鲜度 / 数据计数 / validation errors；
  - **不计算 PnL，也不做任何反事实收益推算**；
  - 只读：不写 latest、不写 artifact、不调 LLM、不碰生产 DB。

机器可读状态：
  OK                 latest 存在、brief 有效、未超龄。
  ALREADY_CURRENT    同上，且最近一次写方运行因 input_hash 未变而跳过付费调用。
  DISABLED           env flag `DA_EVENT_CONTEXT_SHADOW` 为 false（不读 latest）。
  NO_DATA            latest 不存在。
  ERROR              latest 不可读 / schema 不符 / brief 无效 / 时间戳无效或未来 /
                     超龄 / --date 与 latest 的 ET 日不符。

退出码：OK / ALREADY_CURRENT / DISABLED = 0；NO_DATA = 1；ERROR = 2。

用法：
    ./venv/bin/python scripts/da_event_context_shadow_eval.py
    ./venv/bin/python scripts/da_event_context_shadow_eval.py --date 2026-11-12
    ./venv/bin/python scripts/da_event_context_shadow_eval.py --latest /tmp/live/latest.json
    ./venv/bin/python scripts/da_event_context_shadow_eval.py --max-age-hours 60
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.da_event_context_shadow import (  # noqa: E402
    DEFAULT_OUT_ROOT,
    FLAG,
    REPORT_SCHEMA,
    CAVEAT,
    validate_brief_record,
)
from scripts.shadow_runtime import env_flag, read_json  # noqa: E402

DEFAULT_LATEST = DEFAULT_OUT_ROOT / "latest.json"
DEFAULT_MAX_AGE_HOURS = 36.0
FUTURE_TOLERANCE_SECONDS = 300.0
STATUSES = ("OK", "ALREADY_CURRENT", "DISABLED", "NO_DATA", "ERROR")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
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


def _base(status: str, latest_path: Path) -> Dict[str, Any]:
    return {
        "status": status,
        "latest_path": str(latest_path),
        "record_only": True,
        "caveat": CAVEAT,
    }


def evaluate(
    latest_path: Path,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    expected_date: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """读 latest 并返回状态报告；纯只读。"""
    if not env_flag(FLAG):
        return _base("DISABLED", latest_path)

    now = now or _now_utc()
    path = Path(latest_path)
    if not path.is_file():
        return {**_base("NO_DATA", path), "reason": "latest_missing"}

    doc = read_json(path)
    if not isinstance(doc, dict):
        return {**_base("ERROR", path), "reason": "latest_unreadable"}

    errors: list[str] = []
    if doc.get("schema") != REPORT_SCHEMA:
        errors.append("latest_schema_mismatch")
    if expected_date and doc.get("date_et") != expected_date:
        errors.append(f"date_mismatch:{doc.get('date_et')}")

    generated_at = _parse_iso(doc.get("generated_at"))
    age_hours: Optional[float] = None
    if generated_at is None:
        errors.append("generated_at_invalid")
    else:
        age_hours = round((now - generated_at).total_seconds() / 3600.0, 3)
        if age_hours < -(FUTURE_TOLERANCE_SECONDS / 3600.0):
            errors.append("generated_at_in_future")
        if age_hours > max_age_hours:
            errors.append(f"latest_stale:{age_hours}h>{max_age_hours}h")

    brief = doc.get("brief")
    errors.extend(validate_brief_record(brief, date_et=doc.get("date_et")))

    stored_status = doc.get("status")
    if stored_status not in ("OK", "ALREADY_CURRENT"):
        errors.append(f"latest_status_invalid:{stored_status}")

    if errors:
        return {
            **_base("ERROR", path),
            "reason": "validation_failed",
            "errors": sorted(set(errors)),
            "validation_errors": sorted(set(errors)),
            "age_hours": age_hours,
            "date_et": doc.get("date_et"),
            "as_of": doc.get("as_of"),
            "generated_at": doc.get("generated_at"),
            "last_checked_at": doc.get("last_checked_at"),
            "input_hash": doc.get("input_hash"),
            "brief_id": doc.get("brief_id"),
            "event_count": doc.get("event_count"),
            "fact_count": doc.get("fact_count"),
            "transmission_count": doc.get("transmission_count"),
            "llm_calls": doc.get("llm_calls"),
            "artifact_path": doc.get("artifact_path"),
            "artifact_exists": bool(doc.get("artifact_path")) and (
                ROOT / str(doc.get("artifact_path"))
            ).is_file(),
        }

    checked_at = _parse_iso(doc.get("last_checked_at"))
    checked_age_hours = (
        round((now - checked_at).total_seconds() / 3600.0, 3)
        if checked_at is not None else None
    )
    status = "ALREADY_CURRENT" if stored_status == "ALREADY_CURRENT" else "OK"
    artifact_value = str(doc.get("artifact_path") or "")
    artifact_file = ROOT / artifact_value if artifact_value else None
    return {
        **_base(status, path),
        "date_et": doc.get("date_et"),
        "as_of": doc.get("as_of"),
        "generated_at": doc.get("generated_at"),
        "last_checked_at": doc.get("last_checked_at"),
        "age_hours": age_hours,
        "checked_age_hours": checked_age_hours,
        "max_age_hours": max_age_hours,
        "input_hash": doc.get("input_hash"),
        "brief_id": doc.get("brief_id"),
        "event_count": doc.get("event_count"),
        "fact_count": doc.get("fact_count"),
        "transmission_count": doc.get("transmission_count"),
        "validation_errors": [],
        "llm_calls": doc.get("llm_calls"),
        "check_count": doc.get("check_count"),
        "main_model": doc.get("main_model"),
        "challenger_model": doc.get("challenger_model"),
        "target_universe_count": len(doc.get("target_universe") or []),
        "supplemental_merged_fact_count": doc.get("supplemental_merged_fact_count"),
        "degraded": doc.get("degraded"),
        "artifact_path": doc.get("artifact_path"),
        "artifact_exists": bool(artifact_file and artifact_file.is_file()),
        "allowed_statuses": list(STATUSES),
    }


def _parse_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"--date must be YYYY-MM-DD: {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EventContext record-only 影子 latest 只读评估器 (无 PnL)",
    )
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    parser.add_argument("--date", default="", help="要求 latest 的 ET 日等于该日期")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help="latest 超过该小时数即 ERROR (默认 36h)")
    parser.add_argument("--json", action="store_true", help="兼容开关；输出始终是 JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        expected_date = _parse_date(args.date) if args.date else None
    except ValueError as exc:
        print(json.dumps(
            {
                **_base("ERROR", args.latest),
                "reason": "bad_cli",
                "error": str(exc),
            },
            ensure_ascii=False, indent=2, sort_keys=True,
        ))
        return 2
    report = evaluate(
        args.latest,
        max_age_hours=args.max_age_hours,
        expected_date=expected_date,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return {"OK": 0, "ALREADY_CURRENT": 0, "DISABLED": 0, "NO_DATA": 1}.get(
        str(report.get("status")), 2
    )


if __name__ == "__main__":
    raise SystemExit(main())