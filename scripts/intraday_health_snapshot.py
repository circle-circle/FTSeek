#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Intraday ERROR/CRITICAL snapshot producer for the meta_audit loop.

The meta loop must not infer health from prose logs. This producer converts the
repo's literal ERROR/CRITICAL log contract into one explicit JSON artifact.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(os.environ.get("FTSEEK_REPO", Path(__file__).resolve().parent.parent))
DEFAULT_LOGS = BASE / "logs"
DEFAULT_OUTPUT = BASE / "data" / "intraday_health_latest.json"
LEVEL_MARKERS = ("] ERROR:", "] CRITICAL:")
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
ROTATED_RE = re.compile(r"\.log-(\d{8})(?:\.gz)?$")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _normalize(message: str) -> str:
    compact = " ".join(message.split())
    compact = re.sub(r"\b\d[\d.,:/%-]*\b", "<N>", compact)
    compact = re.sub(r"\b[0-9a-f]{8,}\b", "<HASH>", compact, flags=re.IGNORECASE)
    return compact[:240]


def _signature(message: str) -> str:
    return hashlib.sha256(_normalize(message).encode("utf-8")).hexdigest()[:12]


def _in_window(path: Path, cutoff: datetime, now: datetime) -> bool:
    match = ROTATED_RE.search(path.name)
    if not match:
        return True
    try:
        rotated = datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError:
        return False
    return cutoff.date() <= rotated.date() <= now.date() + timedelta(days=1)


def _lines(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line
    else:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line


def scan(logs_dir: Path, window_hours: int):
    now = datetime.now()
    cutoff = now - timedelta(hours=window_hours)
    paths = sorted(
        p for p in logs_dir.glob("*.log*")
        if p.is_file() and _in_window(p, cutoff, now)
    )
    if not paths:
        raise RuntimeError(f"日志面为空: {logs_dir}")

    grouped = {}
    total = 0
    skipped_no_timestamp = 0
    for path in paths:
        for line in _lines(path):
            marker = next((m for m in LEVEL_MARKERS if m in line), None)
            if not marker:
                continue
            timestamp_match = TIMESTAMP_RE.match(line)
            if not timestamp_match:
                skipped_no_timestamp += 1
                continue
            try:
                timestamp = datetime.strptime(timestamp_match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                skipped_no_timestamp += 1
                continue
            if timestamp < cutoff or timestamp > now + timedelta(minutes=5):
                continue
            level = "CRITICAL" if marker.endswith("CRITICAL:") else "ERROR"
            message = line.split(marker, 1)[1].strip() if marker in line else line.strip()
            signature = _signature(message)
            bucket = grouped.setdefault(signature, {
                "count": 0, "level": level, "message": message,
                "examples": [], "last_seen": timestamp.isoformat(timespec="seconds"),
            })
            bucket["count"] += 1
            if len(bucket["examples"]) < 3:
                bucket["examples"].append({
                    "file": path.name,
                    "line": line.strip()[:360],
                })
            bucket["last_seen"] = timestamp.isoformat(timespec="seconds")
            total += 1

    findings = []
    for signature, bucket in sorted(grouped.items(), key=lambda item: item[1]["last_seen"], reverse=True):
        findings.append({
            "key": f"intraday_health:{bucket['level']}:{signature}",
            "title": f"盘中日志 {bucket['level']} ×{bucket['count']}",
            "evidence": f"{bucket['message'][:220]} | last_seen={bucket['last_seen']}",
            "severity": "P0",
            "pnl_path": "capital_preservation",
            "count": bucket["count"],
            "examples": bucket["examples"],
        })

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": window_hours,
        "no_findings": not findings,
        "findings": findings,
        "counts": {
            "error_or_critical": total,
            "distinct_signatures": len(findings),
            "logs_scanned": len(paths),
            "lines_without_timestamp": skipped_no_timestamp,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs-dir", default=str(DEFAULT_LOGS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--window-hours", type=int, default=24)
    args = parser.parse_args(argv)
    try:
        doc = scan(Path(args.logs_dir), args.window_hours)
        _atomic_write(Path(args.output), json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    except Exception as exc:
        print(f"[intraday_health_snapshot] producer error: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
