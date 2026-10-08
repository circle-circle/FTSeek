#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""DEB-20261008 E表 命令逐格实跑验证器 (从落盘正文解析, 不从内存).

纪律 (review-system-design): 期望列必须逐字 `in` 实跑输出; 改一个单元格也要重跑.
「同 E#」行复用被引用行的命令输出 (同一命令只跑一次, 节省 shadow 全量重算).
用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/verify_deb20261008.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
DOC = ROOT / "docs/debates/DEB-20261008-profit-ladder-scaleout-feasibility.md"


def parse_rows(text: str) -> list[tuple[str, str, list[str]]]:
    """提取 E 表 (编号, 命令单元, 期望片段列表). 命令可为反引号命令或「同 E#」."""
    rows = []
    for ln in text.split("\n"):
        if not ln.startswith("| E"):
            continue
        safe = ln.replace("\\|", "\x00")
        cells = [c.strip().replace("\x00", "\\|") for c in safe.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        eid = cells[0].strip("* ")
        cmds = re.findall(r"`([^`]+)`", cells[2])
        ref = re.fullmatch(r"同 (E\d+)", cells[2].strip())
        exps = re.findall(r"`([^`]+)`", cells[3])
        if cmds and exps:
            rows.append((eid, cmds[0], exps))
        elif ref and exps:
            rows.append((eid, ref.group(1), exps))
    return rows


def check_structure(text: str) -> int:
    """结构化守卫: 表格列数一致 / 章节号无缺号重号 / §N 引用不悬空."""
    bad = 0
    stripped = re.sub(r"```.*?```", "", text, flags=re.S)
    stripped = re.sub(r"`[^`]*`", "", stripped)
    block: list[tuple[int, int]] = []
    for i, ln in enumerate(stripped.split("\n"), 1):
        if ln.startswith("|"):
            n = ln.replace("\\|", "").count("|")
            block.append((i, n))
        elif block:
            counts: dict[int, list[int]] = {}
            for li, n in block:
                counts.setdefault(n, []).append(li)
            if len(counts) > 1:
                majority = max(counts, key=lambda k: len(counts[k]))
                for n, lis in counts.items():
                    if n != majority:
                        bad += 1
                        print(f"  🔴 表格列数不一致 line {lis}: pipes={n} vs 多数={majority}")
            block = []
    nums = [int(m.group(1)) for m in re.finditer(r"^## (\d+)\.", text, flags=re.M)]
    if nums:
        dup = [n for n in set(nums) if nums.count(n) > 1]
        gap = [n for n in range(1, max(nums) + 1) if n not in nums]
        if dup:
            bad += 1
            print(f"  🔴 章节重号: {dup}")
        if gap:
            bad += 1
            print(f"  🔴 章节缺号: {gap}")
    valid = set(nums)
    for m in re.finditer(r"§(\d+)", stripped):
        if int(m.group(1)) not in valid:
            bad += 1
            print(f"  🔴 悬空引用 §{m.group(1)}")
    return bad


def run_command(cmd: str) -> tuple[int, str]:
    p = subprocess.run(cmd.replace("\\|", "|"), shell=True, cwd=str(ROOT),
                       capture_output=True, text=True, timeout=450)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    print("=== 结构化守卫 ===")
    struct_bad = check_structure(text)
    print(f"  结构问题数: {struct_bad}")

    rows = parse_rows(text)
    print(f"\n=== E 表可执行行数: {len(rows)} ===")
    # cmd 字段: 反引号命令 = 真命令; 「同 E#」= 被引用行编号 (一层解引用).
    raw = {eid: (cell, exps) for eid, cell, exps in rows}
    resolved: dict[str, tuple[str, list[str]]] = {}
    for eid, cell, exps in rows:
        if cell in raw and cell != eid:
            ref_cmd = raw[cell][0]
            resolved[eid] = (ref_cmd, exps)
        else:
            resolved[eid] = (cell, exps)

    outputs: dict[str, tuple[int, str]] = {}
    fail = 0
    for eid, (cmd, exps) in resolved.items():
        if not cmd.startswith(("venv/bin/python", "grep", "timeout")):
            print(f"  {eid}: SKIP (非 shell 直执行形式) {cmd[:60]}")
            continue
        if cmd not in outputs:
            outputs[cmd] = run_command(cmd)
        rc, out = outputs[cmd]
        clean_exps = [e.replace("\\|", "|") for e in exps]
        missing = [e for e in clean_exps if e not in out]
        if rc != 0 or missing:
            fail += 1
            print(f"  🔴 {eid}: rc={rc} 未命中 {len(missing)}/{len(clean_exps)}")
            for m in missing[:3]:
                print(f"       期望片段未命中: {m[:90]}")
            if rc != 0:
                print(f"       stderr: {out.strip()[:200]}")
        else:
            print(f"  ✅ {eid}: {len(clean_exps)}/{len(clean_exps)} 命中")

    print(f"\n命令验证失败: {fail} | 结构问题: {struct_bad}")
    return 1 if (fail or struct_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
