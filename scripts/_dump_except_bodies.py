#!/usr/bin/env python3
"""批量提取 silent_except_audit 标记的 except handler 代码体, 供一次性分诊."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
data = json.loads(Path(sys.argv[1]).read_text())

for f in data:
    path, lineno = f["at"].rsplit(":", 1)
    lineno = int(lineno)
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    lo = max(0, lineno - 6)
    hi = min(len(lines), lineno + 8)
    body = "\n".join(f"  {i+1:5d}| {lines[i]}" for i in range(lo, hi))
    print(f"\n{'='*72}\n[{f['level']}] {f['at']}  (函数: {f['ctx']})\n{body}")
