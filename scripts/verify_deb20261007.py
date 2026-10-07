#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""DEB-20261007 E表/C表 命令逐格实跑验证器 (从落盘正文解析, 不从内存).

纪律 (review-system-design): 期望列必须逐字 `in` 实跑输出; 改一个单元格也要重跑.
用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/verify_deb20261007.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
DOC = ROOT / "docs/debates/DEB-20261007-entry-pricing-reprice-mfe-capture.md"


def parse_rows(text: str, table_id: str) -> list[tuple[str, str, list[str]]]:
    """提取指定表的 (编号, 命令, 期望片段列表). 只取含反引号命令的行.

    分割前先把转义竖线 ``\\|`` 换成占位符 — 否则 grep 管道里的 `\\| head -3`
    会把一行拆成 5-8 列, 导致该行被 len(cells)<4 之外的错位逻辑静默跳过。
    """
    rows = []
    for ln in text.split("\n"):
        if not ln.startswith("| " + table_id):
            continue
        safe = ln.replace("\\|", "\x00")
        cells = [c.strip().replace("\x00", "\\|") for c in safe.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        eid = cells[0].strip("* ")
        cmds = re.findall(r"`([^`]+)`", cells[2])
        exps = re.findall(r"`([^`]+)`", cells[3])
        if cmds and exps:
            rows.append((eid, cmds[0], exps))
    return rows


def check_structure(text: str) -> int:
    """结构化守卫 (机器可判三项): 表格列数一致 / 章节号无缺号重号 / §N 引用不悬空."""
    bad = 0
    # ① 每个表格块内各行有效列数一致 (剥代码块与行内代码后判)
    stripped = re.sub(r"```.*?```", "", text, flags=re.S)
    stripped = re.sub(r"`[^`]*`", "", stripped)
    block: list[tuple[int, int]] = []
    for i, ln in enumerate(stripped.split("\n"), 1):
        if ln.startswith("|"):
            n = ln.replace("\\|", "").count("|")
            block.append((i, n))
        elif block:
            counts = {}
            for li, n in block:
                counts.setdefault(n, []).append(li)
            if len(counts) > 1:
                majority = max(counts, key=lambda k: len(counts[k]))
                for n, lis in counts.items():
                    if n != majority:
                        bad += 1
                        print(f"  🔴 表格列数不一致 line {lis}: pipes={n} vs 多数={majority}")
            block = []
    # ② 章节号无缺号无重号
    nums = [int(m.group(1)) for m in re.finditer(r"^## (\d+)\.", text, flags=re.M)]
    if nums:
        dup = [n for n in set(nums) if nums.count(n) > 1]
        gap = [n for n in range(1, max(nums) + 1) if n not in nums]
        if dup:
            bad += 1; print(f"  🔴 章节重号: {dup}")
        if gap:
            bad += 1; print(f"  🔴 章节缺号: {gap}")
    # ③ 内部 §N 引用不悬空 (剥代码块后扫)
    valid = set(nums)
    for m in re.finditer(r"§(\d+)", stripped):
        if int(m.group(1)) not in valid:
            bad += 1; print(f"  🔴 悬空引用 §{m.group(1)}")
    return bad


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    print("=== 结构化守卫 ===")
    struct_bad = check_structure(text)
    print(f"  结构问题数: {struct_bad}")
    fail = 0
    for tid in ("E", "C"):
        rows = parse_rows(text, tid)
        print(f"\n=== {tid} 表可执行行数: {len(rows)} ===")
        fail += run_rows(rows, expect_hit=(tid == "E"))
    print(f"\n命令验证失败: {fail} | 结构问题: {struct_bad}")
    return 1 if (fail or struct_bad) else 0


def run_rows(rows, expect_hit: bool = True) -> int:
    """expect_hit=True (E 表): 期望片段必须逐字出现在输出里。
    expect_hit=False (C 表): 第 4 列是**反例判据**（什么输出说明作者错），
      本来就不该出现在输出里 ⇒ 只验命令可执行 (rc=0)，不验命中。"""
    fail = 0
    for eid, cmd, exps in rows:
        if not cmd.startswith(("venv/bin/python", "grep", "ls", "stat")):
            print(f"  {eid}: SKIP (非 shell 直执行形式) {cmd[:60]}")
            continue
        p = subprocess.run(cmd.replace("\\|", "|"), shell=True, cwd=str(ROOT),
                           capture_output=True, text=True, timeout=300)
        out = p.stdout + p.stderr
        # 期望片段里的 `\|` 是 markdown 转义, 实际输出是裸 `|` ⇒ 同样还原
        exps = [e.replace("\\|", "|") for e in exps]
        missing = [e for e in exps if e not in out] if expect_hit else []
        if p.returncode != 0 or missing:
            fail += 1
            print(f"  🔴 {eid}: rc={p.returncode} 未命中 {len(missing)}/{len(exps)}")
            for m in missing[:3]:
                print(f"       期望片段未命中: {m[:90]}")
            if p.returncode != 0:
                print(f"       stderr: {out.strip()[:200]}")
        else:
            print(f"  ✅ {eid}: {len(exps)}/{len(exps)} 命中")
    return fail


if __name__ == "__main__":
    sys.exit(main())
