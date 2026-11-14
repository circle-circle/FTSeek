#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EXT 在途事件风险连续影子 · 只读评估器

输入: `<output-dir>/latest.json` (写方 `scripts/ext_event_risk_live_shadow.py`)。
只回答四个问题: 影子在不在跑 / 数据多新 / 有几笔在评 / 有几笔 REVIEW。
**不计算反事实 PnL**, 不读交易库, 不写任何文件。

机器可读状态 (优先级从上到下):
  DISABLED  开关 `EXT_EVENT_RISK_SHADOW` 为 false (影子未启用); 退出码 0。
  ERROR     latest 读不了 / 解析失败 / schema 不符 / 写方记录了持仓评估错误;
            退出码 2。
  NO_DATA   latest.json 不存在 (已启用但还没跑过, 或 output-dir 指错);
            退出码 1。
  REVIEW    至少 1 笔实盘判决为 REVIEW (含被降级的 official_invalidate_candidate);
            退出码 1 (提示需要人看的证据, 不是故障)。
  OK        有 latest 且零 REVIEW、零错误; 退出码 0。

age 取 latest 的 `generated_at` (写方每轮原子覆盖该文件), 退化到 `as_of`、
再退化到文件 mtime; `age_source` 明示用了哪一个。`--max-age-minutes` 只给出
`stale` 布尔 (不改状态), 供哨兵判断影子是否停摆。

用法:
  ./venv/bin/python scripts/ext_event_risk_live_shadow_eval.py
  ./venv/bin/python scripts/ext_event_risk_live_shadow_eval.py --json
  ./venv/bin/python scripts/ext_event_risk_live_shadow_eval.py --output-dir /tmp/ext-live
  ./venv/bin/python scripts/ext_event_risk_live_shadow_eval.py --max-age-minutes 30
  ./venv/bin/python scripts/ext_event_risk_live_shadow_eval.py --ignore-flag
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import shadow_runtime  # noqa: E402

SCHEMA = "ext_event_risk_live_shadow_eval.v1"
WRITER_SCHEMA = "ext_event_risk_live_shadow.v1"
ENV_FLAG = "EXT_EVENT_RISK_SHADOW"

DEFAULT_OUTPUT_DIR = ROOT / "data" / "shadow" / "ext_event_risk" / "live"
DEFAULT_ENV_FILE = shadow_runtime.DEFAULT_ENV_FILE

STATE_OK = "OK"
STATE_REVIEW = "REVIEW"
STATE_DISABLED = "DISABLED"
STATE_NO_DATA = "NO_DATA"
STATE_ERROR = "ERROR"

EXIT_CODES = {
    STATE_OK: 0,
    STATE_DISABLED: 0,
    STATE_REVIEW: 1,
    STATE_NO_DATA: 1,
    STATE_ERROR: 2,
}

COUNT_KEYS = (
    "positions_total",
    "evaluated",
    "no_change",
    "review",
    "official_invalidate_candidates",
    "new_review_artifacts",
    "errors",
)

CAVEATS = (
    "record-only shadow: verdicts are evidence for human review, not exit orders.",
    "No counterfactual or avoided PnL is computed; absence of a review is not proof of safety.",
    "A stale latest.json means the shadow stopped running; it says nothing about positions.",
)


# ---------------------------------------------------------------------------
# 读取 / 时间
# ---------------------------------------------------------------------------


def latest_path(output_dir: Path, path: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else Path(output_dir) / "latest.json"


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_seconds(now: datetime, stamp: Optional[datetime]) -> Optional[float]:
    if stamp is None:
        return None
    return max(0.0, (now - stamp).total_seconds())


def _age_human(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# 计数
# ---------------------------------------------------------------------------


def _normalize_counts(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict) or not raw:
        return None
    counts: Dict[str, int] = {}
    for key in COUNT_KEYS:
        value = raw.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None
        counts[key] = value
    return counts


def _counts_from_positions(positions: Any) -> Optional[Dict[str, int]]:
    if not isinstance(positions, list):
        return None
    rows = [row for row in positions if isinstance(row, dict)]
    return {
        "positions_total": len(rows),
        "evaluated": sum(1 for row in rows if row.get("status") == "OK"),
        "no_change": sum(1 for row in rows if row.get("live_verdict") == "NO_CHANGE"),
        "review": sum(1 for row in rows if row.get("live_verdict") == "REVIEW"),
        "official_invalidate_candidates": sum(
            1 for row in rows if row.get("official_invalidate_candidate")
        ),
        "new_review_artifacts": sum(
            1 for row in rows if row.get("artifact_written")
        ),
        "errors": sum(1 for row in rows if row.get("status") == "ERROR"),
    }


def _position_rows(positions: Any) -> List[Dict[str, Any]]:
    if not isinstance(positions, list):
        return []
    rows: List[Dict[str, Any]] = []
    for row in positions:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "trade_id": row.get("trade_id"),
                "decision_id": row.get("decision_id"),
                "symbol": row.get("symbol"),
                "status": row.get("status"),
                "live_verdict": row.get("live_verdict"),
                "underlying_verdict": row.get("underlying_verdict"),
                "official_invalidate_candidate": bool(
                    row.get("official_invalidate_candidate")
                ),
                "material_event_count": row.get("material_event_count"),
                "artifact": row.get("artifact"),
                "error": row.get("error"),
            }
        )
    rows.sort(key=lambda row: (str(row.get("trade_id")), str(row.get("decision_id"))))
    return rows


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def evaluate_latest(
    payload: Any,
    *,
    path: Path,
    now: Optional[datetime] = None,
    max_age_seconds: Optional[float] = None,
    enabled: bool = True,
    file_mtime: Optional[datetime] = None,
    error: Optional[str] = None,
    missing: bool = False,
) -> Dict[str, Any]:
    """纯函数: latest 内容 → 机器可读状态。不读写任何文件。"""
    now = now or datetime.now(timezone.utc)
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "path": str(path),
        "enabled": bool(enabled),
        "state": None,
        "exit_code": None,
        "counts": None,
        "positions": [],
        "errors": [],
        "generated_at": None,
        "as_of": None,
        "as_of_source": None,
        "age_seconds": None,
        "age_source": "unknown",
        "age_human": None,
        "file_mtime_utc": file_mtime.isoformat() if file_mtime else None,
        "file_age_seconds": _age_seconds(now, file_mtime),
        "max_age_seconds": max_age_seconds,
        "stale": None,
        "notes": [],
        "caveats": list(CAVEATS),
    }

    if isinstance(payload, dict):
        result["generated_at"] = payload.get("generated_at")
        result["as_of"] = payload.get("as_of")
        result["as_of_source"] = payload.get("as_of_source")
        counts = _normalize_counts(payload.get("counts"))
        if counts is None:
            counts = _counts_from_positions(payload.get("positions"))
        result["counts"] = counts
        result["positions"] = _position_rows(payload.get("positions"))
        raw_errors = payload.get("errors")
        if isinstance(raw_errors, list):
            result["errors"] = [row for row in raw_errors if isinstance(row, dict)]

    # age: generated_at → as_of → 文件 mtime
    for value, source in (
        (result.get("generated_at"), "generated_at"),
        (result.get("as_of"), "as_of"),
    ):
        stamp = _parse_iso(value)
        if stamp is not None:
            result["age_seconds"] = _age_seconds(now, stamp)
            result["age_source"] = source
            break
    if result["age_seconds"] is None:
        result["age_seconds"] = result["file_age_seconds"]
        result["age_source"] = "file_mtime" if result["file_age_seconds"] is not None else "unknown"
    result["age_human"] = _age_human(result["age_seconds"])
    if max_age_seconds is not None and result["age_seconds"] is not None:
        result["stale"] = result["age_seconds"] > max_age_seconds
        if result["stale"]:
            result["notes"].append(
                f"latest is older than {max_age_seconds:.0f}s — shadow may have stopped"
            )

    if not enabled:
        state = STATE_DISABLED
        result["notes"].append(f"{ENV_FLAG} is false; the shadow writer is not running")
    elif missing:
        state = STATE_NO_DATA
        result["notes"].append(f"latest not found: {path}")
    elif error is not None:
        state = STATE_ERROR
        result["notes"].append(error)
    elif not isinstance(payload, dict):
        state = STATE_ERROR
        result["notes"].append("latest.json is not a JSON object")
    elif payload.get("schema") != WRITER_SCHEMA:
        state = STATE_ERROR
        result["notes"].append(
            f"schema mismatch: expected {WRITER_SCHEMA!r}, got {payload.get('schema')!r}"
        )
    elif result["counts"] is None:
        state = STATE_ERROR
        result["notes"].append("latest.json has no usable counts")
    elif result["counts"]["errors"] > 0:
        state = STATE_ERROR
        result["notes"].append(
            f"{result['counts']['errors']} position evaluation error(s) recorded by the writer"
        )
    elif result["counts"]["review"] > 0:
        state = STATE_REVIEW
    else:
        state = STATE_OK

    result["state"] = state
    result["exit_code"] = EXIT_CODES[state]
    return result


def load_and_evaluate(
    *,
    path: Path,
    now: Optional[datetime] = None,
    max_age_seconds: Optional[float] = None,
    enabled: bool = True,
) -> Dict[str, Any]:
    """读 latest.json 并评估; 文件缺失 → NO_DATA, 读/解析失败 → ERROR。"""
    now = now or datetime.now(timezone.utc)
    path = Path(path)
    file_mtime = None
    try:
        stat = path.stat()
        file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    except OSError:
        file_mtime = None

    if not path.exists():
        return evaluate_latest(
            None,
            path=path,
            now=now,
            max_age_seconds=max_age_seconds,
            enabled=enabled,
            file_mtime=None,
            missing=enabled,
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        return evaluate_latest(
            None,
            path=path,
            now=now,
            max_age_seconds=max_age_seconds,
            enabled=enabled,
            file_mtime=file_mtime,
            error=f"cannot read latest: {type(exc).__name__}: {exc}",
        )

    return evaluate_latest(
        payload,
        path=path,
        now=now,
        max_age_seconds=max_age_seconds,
        enabled=enabled,
        file_mtime=file_mtime,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_human(result: Dict[str, Any]) -> str:
    counts = result.get("counts") or {}
    return (
        f"status={result['state']} "
        f"positions={counts.get('positions_total', 0)} "
        f"no_change={counts.get('no_change', 0)} "
        f"review={counts.get('review', 0)} "
        f"errors={counts.get('errors', 0)} "
        f"age={result.get('age_human') or 'unknown'} "
        f"stale={result.get('stale')} "
        f"path={result['path']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only evaluator for the continuous EXT event-risk live shadow."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="explicit latest.json path (overrides --output-dir)",
    )
    parser.add_argument(
        "--max-age-minutes",
        type=float,
        default=None,
        help="mark stale when latest is older than this many minutes (state unchanged)",
    )
    parser.add_argument(
        "--ignore-flag",
        action="store_true",
        help="evaluate latest.json even when EXT_EVENT_RISK_SHADOW is false",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    enabled = args.ignore_flag or shadow_runtime.env_flag(
        ENV_FLAG, env_file=args.env_file
    )
    max_age_seconds = (
        args.max_age_minutes * 60.0 if args.max_age_minutes is not None else None
    )
    result = load_and_evaluate(
        path=latest_path(args.output_dir, args.path),
        max_age_seconds=max_age_seconds,
        enabled=enabled,
    )
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_format_human(result))
        for note in result.get("notes") or []:
            print(f"note: {note}")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())