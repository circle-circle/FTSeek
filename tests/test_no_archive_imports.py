"""
减法审计 R2 方法论固化 (2026-07-26):
archive/ 目录必须零活 import — F1 教训: api_limit_monitor 被误归档后
finnhub/twelvedata provider 仍从 scripts.archive import, 主进程带病运行 3 个月。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["app", "scripts", "config"]
PATTERN = re.compile(r"(from\s+scripts\.archive|import\s+scripts\.archive|scripts/archive/)")


def test_no_live_imports_from_archive():
    offenders = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(ROOT)
            if "archive" in rel.parts or "__pycache__" in rel.parts:
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if PATTERN.search(stripped):
                    offenders.append(f"{rel}:{lineno}: {stripped[:100]}")
    main_py = ROOT / "main.py"
    if main_py.exists():
        for lineno, line in enumerate(main_py.read_text().splitlines(), 1):
            if not line.strip().startswith("#") and PATTERN.search(line):
                offenders.append(f"main.py:{lineno}: {line.strip()[:100]}")
    assert not offenders, (
        "archive/ 必须零活 import (活模块请移回 scripts/, 见 DEB-20260726 F1):\n"
        + "\n".join(offenders)
    )
