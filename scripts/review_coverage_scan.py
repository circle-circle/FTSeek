#!/usr/bin/env python3
"""Read-only coverage and push-exit census for the review system.

The scan deliberately includes repository code, Hermes scripts, and Hermes
skills.  Test references are recorded but do not mark a producer alive.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_log = logging.getLogger(__name__)

REPO = Path(os.environ.get("FTSEEK_REPO", Path(__file__).resolve().parents[1]))
HERMES = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))
REVIEW_LINE = re.compile(
    r"review|scorecard|report|settle|invariant|lesson|attribution", re.IGNORECASE
)
MAX_TOKENIZE_BYTES = 2_000_000
# Cron-redirected logs are never referenced by code (the crontab line is the
# only writer), so a code-reference liveness test is the wrong axis for them:
# it flagged all 25 review-line cron logs as dead on 2026-09-26 while every
# producer was in fact running.  Liveness for these artifacts = mtime within
# the schedule's own max natural interval + 1 tick (ftseek-loop-engineering:
# daily `* * 2-6` => 4d, weekly => 8d).
DEFAULT_CRON_LOG_THRESHOLD_DAYS = 8

# alive 判据口径版本 — 进 summary["liveness_rule"] 并被载体纳入签名。
# v2 (2026-09-26, RFC-109①): log 类 artifact 按 mtime + 调度自身最大自然间隔判活,
#   不再要求「有代码引用该 artifact 路径」; 非 log 类仍按代码引用 (tests 不算引用)。
#   换轴实测效果: dead 21 → 0 (19 个假阳消失, 真缺 2 个中 1 个 eth_weekly_gate.log
#   于当日 09:00 首跑自愈)。
# 改判据必须同步改这个常量, 否则载体无法区分「僵尸被清理」与「尺子换了轴」。
LIVENESS_RULE = "v2:mtime-freshness-for-cron-logs"


def _cron_dow_set(field: str) -> set[int] | None:
    """Expand a cron day-of-week field to {0..6}; None if unparseable/'*'."""
    if field == "*":
        return None
    days: set[int] = set()
    for part in field.split(","):
        match = re.match(r"^(\d)(?:-(\d))?$", part)
        if not match:
            return set()
        start = int(match.group(1)) % 7
        end = (int(match.group(2)) % 7) if match.group(2) else start
        if start <= end:
            days.update(range(start, end + 1))
        else:
            days.update(range(start, 7), range(0, end + 1))
    return days


def _cron_log_threshold_days(cron_line: str) -> int:
    """Max natural gap between runs implied by the schedule, + 1 tick."""
    fields = cron_line.split()[:5]
    if len(fields) < 5:
        return DEFAULT_CRON_LOG_THRESHOLD_DAYS
    minute, hour, dom, _month, dow = fields
    days = _cron_dow_set(dow)
    if days is None:
        if dom != "*":
            return 32  # monthly-style schedule
        return 2  # daily (+1 tick covers late runs / DST edges)
    if not days:
        return DEFAULT_CRON_LOG_THRESHOLD_DAYS
    max_gap = 1
    for day in range(7):
        if day in days:
            gap = 1
            while (day + gap) % 7 not in days:
                gap += 1
            max_gap = max(max_gap, gap)
    return max_gap + 1


def _cron_log_alive(cron_line: str, artifact: str, now: float | None = None) -> tuple[bool, dict]:
    """Freshness-based liveness for cron-redirected log artifacts."""
    import time as _time

    now = _time.time() if now is None else now
    path = Path(artifact)
    if not path.is_absolute():
        cd_match = re.search(r"\bcd\s+(\S+)", cron_line)
        base = Path(cd_match.group(1)) if cd_match else REPO
        path = base / path
    threshold = _cron_log_threshold_days(cron_line)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False, {
            "alive_basis": "mtime_freshness",
            "age_days": None,
            "threshold_days": threshold,
            "note": "file_missing",
        }
    age_days = (now - mtime) / 86400.0
    return age_days <= threshold, {
        "alive_basis": "mtime_freshness",
        "age_days": round(age_days, 2),
        "threshold_days": threshold,
    }


def _root_label(root: Path) -> str:
    parts = root.resolve().parts
    if len(parts) >= 2 and parts[-2] == ".hermes":
        return f"hermes/{root.name}"
    return root.name


def _scope(path: Path, roots: Sequence[Path]) -> str:
    for root in roots:
        try:
            if path.resolve().is_relative_to(root.resolve()):
                return _root_label(root)
        except OSError:
            _log.warning("_scope: resolve failed for %s", path, exc_info=True)
            continue
    return "other"


def iter_code_files(roots: Iterable[Path]) -> list[Path]:
    files = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in (".py", ".sh"):
                continue
            if "__pycache__" in path.parts:
                continue
            if path.is_file():
                files.append(path)
    return sorted(set(files), key=lambda item: str(item))


def _code_only(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if path.suffix == ".sh":
        return "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )

    if path.stat().st_size <= MAX_TOKENIZE_BYTES:
        kept = []
        code_on_line = False
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for token in tokens:
                if token.type == tokenize.COMMENT:
                    continue
                if token.type == tokenize.NEWLINE:
                    code_on_line = False
                elif token.type == tokenize.STRING and not code_on_line:
                    continue
                else:
                    code_on_line = True
                if token.type not in (
                    tokenize.NL,
                    tokenize.INDENT,
                    tokenize.DEDENT,
                    tokenize.NEWLINE,
                    tokenize.ENCODING,
                    tokenize.ENDMARKER,
                ):
                    kept.append(token.string)
            return " ".join(kept)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            _log.warning("_code_only: tokenize failed for %s", path, exc_info=True)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _load_code_cache(files: Sequence[Path]) -> dict[Path, tuple[str, list[str]]]:
    cache = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            _log.warning("_load_code_cache: read failed for %s", path, exc_info=True)
            continue
        if path.suffix == ".sh":
            code = "\n".join(
                line for line in lines if not line.lstrip().startswith("#")
            )
        else:
            code = _code_only(path)
        cache[path] = (code, lines)
    return cache


def _simple_code(lines: Sequence[str]) -> str:
    return "\n".join(
        line for line in lines if not line.lstrip().startswith("#")
    )


def find_refs(
    target: str,
    files: Sequence[Path],
    roots: Sequence[Path],
    cache: Mapping[Path, tuple[str, list[str]]] | None = None,
) -> list[dict]:
    target = str(target).strip()
    if not target:
        return []
    target_name = Path(target).name
    refs = []
    for path in files:
        if cache is not None and path in cache:
            code, lines = cache[path]
        else:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                _log.warning("find_refs: read failed for %s", path, exc_info=True)
                continue
            code = _simple_code(lines)
        if target in code or (len(target_name) > 4 and target_name in code):
            refs.append(
                {
                    "path": str(path),
                    "scope": _scope(path, roots),
                    "line": _first_line(lines, target, target_name),
                }
            )
    return refs


def _first_line(lines: Sequence[str], target: str, target_name: str) -> int | None:
    for number, line in enumerate(lines, start=1):
        if target in line or (len(target_name) > 4 and target_name in line):
            return number
    return None


def push_exit_census(
    files: Sequence[Path],
    cache: Mapping[Path, tuple[str, list[str]]] | None = None,
) -> dict[str, dict]:
    rows = {}
    for path in files:
        relative = path.resolve().parts
        if "app" in relative and "push" in relative[relative.index("app") + 1:]:
            continue
        if "app" in relative and "notifier" in relative[relative.index("app") + 1:]:
            continue

        categories = []
        matching_lines = []
        if cache is not None and path in cache:
            lines = cache[path][1]
        else:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                _log.warning("push_exit_census: read failed for %s", path, exc_info=True)
                continue
        for number, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            line_categories = []
            if re.search(r"\bfrom app\.push\b|\bimport app\.push\b|get_push_hub|push_text_safe", line):
                line_categories.append("app.push")
            if "app.notifier" in line:
                line_categories.append("notifier")
            if re.search(r"requests\.post|urllib\.request|webhook", line, re.IGNORECASE):
                line_categories.append("webhook")
            if re.search(r"\bcurl\b", line):
                line_categories.append("curl")
            if line_categories:
                categories.extend(line_categories)
                matching_lines.append({"line": number, "text": line.strip()})
        if categories:
            rows[str(path)] = {
                "categories": sorted(set(categories)),
                "matches": matching_lines[:10],
            }
    return rows


def build_report(
    cron_text: str,
    registry_items: Sequence[Mapping[str, object]],
    roots: Sequence[Path],
    today: str,
) -> dict:
    files = iter_code_files(roots)
    code_cache = _load_code_cache(files)
    scope_counts = {}
    for path in files:
        scope = _scope(path, roots)
        scope_counts[scope] = scope_counts.get(scope, 0) + 1

    producers = []
    for item in registry_items:
        data_path = str(item.get("data_path") or "").split(" (")[0].strip()
        if not data_path:
            continue
        refs = find_refs(data_path, files, roots, code_cache)
        producers.append(
            {
                "id": str(item.get("id")),
                "source": "shadow_registry",
                "artifact": data_path,
                "refs": refs,
                "alive": any(ref["scope"] != "tests" for ref in refs),
            }
        )

    for cron_line in cron_text.splitlines():
        stripped = cron_line.strip()
        if not stripped or stripped.startswith("#") or not REVIEW_LINE.search(stripped):
            continue
        redirect = re.search(r">>\s*(\S+)", stripped)
        if not redirect:
            continue
        artifact = redirect.group(1)
        refs = find_refs(artifact, files, roots, code_cache)
        # Liveness axis = mtime freshness against the schedule's own max
        # natural interval, NOT code references (cron-redirected logs are
        # written by the crontab line alone and are never imported/read by
        # code; the old axis produced 25/25 false 'dead' on 2026-09-26).
        alive, alive_detail = _cron_log_alive(stripped, artifact)
        producers.append(
            {
                "id": f"cron:{artifact}",
                "source": "crontab",
                "artifact": artifact,
                "refs": refs,
                "alive": alive,
                "alive_detail": alive_detail,
            }
        )

    dead_producers = [row for row in producers if not row["alive"]]
    return {
        "generated_for": today,
        "corpus": {
            "roots": [_root_label(root) for root in roots],
            "file_count": len(files),
            "scope_counts": dict(sorted(scope_counts.items())),
            "rules": {
                "file_suffixes": [".py", ".sh"],
                "excluded_path_parts": ["__pycache__"],
                "selection": "regular files only",
            },
        },
        "producers": producers,
        "dead_producers": dead_producers,
        "push_exit_census": push_exit_census(files, code_cache),
        "push_exit_census_meta": {
            "categories_are_overlapping": True,
            "note": "A file may use multiple exit classes; category counts must not be summed.",
        },
    }


def _load_registry_items() -> list[dict]:
    sys.path.insert(0, str(REPO))
    from app.playbook.shadow_registry import REGISTRY

    return [asdict(item) for item in REGISTRY]


def _read_crontab() -> str:
    try:
        return subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    today = datetime.now().strftime("%Y-%m-%d")
    roots = [
        REPO / "app",
        REPO / "scripts",
        REPO / "ops",
        REPO / "tests",
        HERMES / "scripts",
        HERMES / "skills",
    ]
    report = build_report(_read_crontab(), _load_registry_items(), roots, today)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    output = args.out or REPO / "data" / "reviews" / f"coverage_{today}.json"
    _atomic_write(output, payload)

    summary = {
        "out": str(output),
        "corpus_file_count": report["corpus"]["file_count"],
        "producer_count": len(report["producers"]),
        "dead_producer_count": len(report["dead_producers"]),
        "push_exit_file_count": len(report["push_exit_census"]),
        "dead_producers": report["dead_producers"],
        # alive 判据口径版本 (RFC-109①换轴: log 类改 mtime+调度新鲜度, 不再要求代码引用)。
        # 为什么必须显式带版本: 载体按 dead 条目集的变化推「🆕/✅ 死产出」, 而**换轴本身**
        # 会让条目集整体变化 (实测 21→0) ⇒ 若不区分, 会把「尺子修好了」播报成「21 个僵尸被
        # 清理了」的假信号。载体把本值纳入签名, 变动时改口径变动播报 (见 ops/hermes
        # /review_coverage_carrier.py::_render)。改判据就要改这个常量。
        "liveness_rule": LIVENESS_RULE,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
