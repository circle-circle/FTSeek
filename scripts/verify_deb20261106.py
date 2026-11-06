#!/usr/bin/env python3
"""DEB-20261106 E/C 表验证器 (只读).

作用: 从落盘正文 (不是内存表格) 解析 E 表与 C 表, 逐行执行命令并核对:
  E 行 = 期望列内所有反引号片段必须逐字出现在命令输出中
  C 行 = 只验命令可执行 (rc=0); 反例判据是给人读的, 不参与机械比对
并做一道结构守卫: 每个表格块内各行列数必须与表头一致.

用法: cd /opt/ftseek/FTSeek-US && venv/bin/python3.11 scripts/verify_deb20261106.py
回滚: 删本文件.
"""
import re
import subprocess
import sys

DOC = "/opt/ftseek/FTSeek-US/docs/debates/DEB-20261106-market-risk-switch-review.md"
ROOT = "/opt/ftseek/FTSeek-US"
lines = open(DOC).read().splitlines()


def split_row(line):
    parts, cur, i = [], "", 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            cur += "\\|"
            i += 2
            continue
        if line[i] == "|":
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += line[i]
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts[1:-1]]


# 结构: 每个表格行 (以 | 开头且含 >=2 个未转义 |) 的有效列数应与所在表头一致
cur_cols, struct_bad = None, 0
for n, l in enumerate(lines, 1):
    if not l.startswith("|"):
        cur_cols = None
        continue
    cells = split_row(l)
    if all(set(c) <= set("-: ") for c in cells):
        cur_cols = len(cells)
        continue
    if cur_cols is not None and len(cells) != cur_cols:
        print("  [结构] L%d 列数 %d != 表头 %d: %s" % (n, len(cells), cur_cols, l[:70]))
        struct_bad += 1
print("结构异常:", struct_bad)

rows = [l for l in lines if re.match(r"^\|\s*([EC])\d+[a-z]?\s*\|", l)]
print("E/C 行数:", len(rows))
bad = 0
for ln in rows:
    cells = split_row(ln)
    if len(cells) != 4:
        print("  [列数] %s" % ln[:70])
        bad += 1
        continue
    rid, claim, cmd, expect = cells
    cmd = cmd.strip().strip("`").strip()
    try:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=900)
        out = r.stdout + r.stderr
        rc = r.returncode
    except Exception as e:
        print("  [异常] %s: %s" % (rid, e))
        bad += 1
        continue
    if rid.startswith("C"):
        ok = rc == 0
        print(("  ✅ " if ok else "  ❌ ") + rid + " (rc=%d, 反例列不比对)" % rc)
        if not ok:
            bad += 1
            print("       out:", out.replace("\n", " ")[:200])
        continue
    frags = re.findall(r"`([^`]+)`", expect)
    miss = [f for f in frags if f not in out]
    ok = not miss and (rc == 0 or bool(frags))
    print(("  ✅ " if ok else "  ❌ ") + rid + (" (frags=%d)" % len(frags)))
    if not ok:
        bad += 1
        for m in miss[:3]:
            print("       miss: %r" % m[:90])
        print("       out-head:", out.replace("\n", " ")[:200])
print("BAD:", bad)
sys.exit(0 if bad == 0 else 1)