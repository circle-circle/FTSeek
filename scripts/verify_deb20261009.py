#!/usr/bin/env python3
"""[DEB-20261009 验证器] §13 复查节的 E 表逐格实跑 + 结构守卫.

跑法:
    cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/verify_deb20261009.py

设计:
- E 表期望片段支持 `同上` 继承 (与 verify_deb20261008.py 同款), 探针脚本只跑一次,
  避免同一条命令重复执行 N 次 (DEB-20261008 HEAD 版的刻意设计, 勿展开)。
- 期望片段列若混入注释文字 (如 `1`（单挂）), 比对必失败 ⇒ 期望列只准放可 grep 的真值。
- 反向能力: 改坏文档任一数字 ⇒ rc=1 (见 --negative 自检模式)。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs/debates/DEB-20261009-unified-session-scan-dispatch.md")
PROBE_REL = "scripts/deb20261009_session_caliber_probe.py"


def _run(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO, timeout=300)
    return (r.stdout or "") + (r.stderr or "")


def parse_e_table(src: str) -> list[dict]:
    """解析 §13.6 E 表; `同上` 继承上一条命令。"""
    m = re.search(r"### 13\.6 E 表.*?\n\n(###|\Z)", src, re.S)
    assert m, "找不到 §13.6 E 表"
    rows, last_cmd = [], None
    for line in m.group(0).splitlines():
        if not line.startswith("| E"):
            continue
        # 只切未转义的竖线 — 命令里的 \| 是 shell 管道, 若一并切会把命令截断
        # (首跑实测: E7/E9 因此拿到半截命令, crontab 报 usage error)
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 3:
            continue
        eid, raw_cmd, expect = cells[0], cells[1], cells[2]
        cmd = raw_cmd.strip("`")
        if cmd in ("同上", ""):
            assert last_cmd, f"{eid} 用『同上』但无上一条命令"
            cmd = last_cmd
        else:
            last_cmd = cmd
        # markdown 表格转义还原: \| → |
        cmd = cmd.replace("\\|", "|")
        expect = expect.strip("`").replace("\\|", "|")
        rows.append({"id": eid, "cmd": cmd, "expect": expect, "raw": line})
    return rows


def verify_e_table(src: str) -> tuple[int, int, list[str]]:
    rows = parse_e_table(src)
    probe_out = None
    ok, fails = 0, []
    for r in rows:
        if PROBE_REL in r["cmd"]:
            if probe_out is None:  # 探针只跑一次
                probe_out = _run(f"env -u PYTHONPATH venv/bin/python {PROBE_REL} 2>&1")
            out = probe_out
        else:
            out = _run(r["cmd"] + " 2>&1")
        # 期望列可含多个片段 (以 ` 与 ` 连接, 如 E4), 全部命中才算通过
        frags = [f.strip().strip("`") for f in re.split(r"`?\s+与\s+`?", r["expect"]) if f.strip()]
        missing = [f for f in frags if f and f not in out]
        if frags and not missing:
            ok += 1
            print(f"  ✅ {r['id']:<4} {r['expect'][:64]}")
        else:
            fails.append(r["id"])
            print(f"  ❌ {r['id']:<4} missing={missing!r}")
            print(f"       got={out.strip()[:160]!r}")
    return ok, len(rows), fails


def verify_doc_claims(src: str, probe_out: str) -> list[str]:
    """L 系列: 从 §13 正文 regex 提取 claim, 与探针实测值对账.

    为什么需要: E 表钉的是「探针输出含某片段」, 改坏**正文**里的数字 E 表抓不到
    (首跑负向实测: 把 §13.4「真边界差异 0 处」改成 9 处, E 表仍 11/11 全绿)。
    期望值不硬编码 — 全部现算, 文档或探针任一漂移都报红。
    """
    problems = []
    sec13 = src[src.index("## 13. 第二 Agent 独立复查"):]

    # L1 真边界差异数
    m = re.search(r"真边界差异 \| \*\*(\d+) 处\*\*", sec13)
    if not m:
        problems.append("L1: §13.4 找不到『真边界差异 N 处』claim (格式漂移?)")
    else:
        doc_n = int(m.group(1))
        pm = re.search(r"真边界差异\s+: (\d+) 处", probe_out)
        if not pm:
            problems.append("L1: 探针输出无『真边界差异』读数")
        elif doc_n != int(pm.group(1)):
            problems.append(f"L1: 正文『真边界差异 {doc_n} 处』≠ 探针实测 {pm.group(1)} 处")

    # L2 死口径数
    m = re.search(r"零消费方死代码 = \*\*(\d+) 处\*\*|\*\*(\d+) 处零消费方死代码\*\*", sec13)
    pm = re.search(r"零消费方死口径\s+: (\d+) 处", probe_out)
    if pm:
        # 正文用表格行 "💀 死" 计数, 直接数它
        tbl_dead = len(re.findall(r"💀 死", sec13))
        if tbl_dead != int(pm.group(1)):
            problems.append(f"L2: §13.4 表格标 💀死 {tbl_dead} 处 ≠ 探针实测 {pm.group(1)} 处")

    # L3 crontab 行数 (正文两处引用 122 / 104)
    for pat, label in ((r"有效 \*\*(\d+)\*\* 行", "crontab 有效行"),
                       (r"\*\*(\d+) 行漏触发\*\*", "周日漏触发行")):
        m = re.search(pat, sec13)
        if m:
            key = "crontab 有效行数 = " if "有效" in label else "周日不触发的 cron 行数 = "
            pm = re.search(re.escape(key) + r"(\d+)", probe_out)
            if pm and int(m.group(1)) != int(pm.group(1)):
                problems.append(f"L3: 正文『{label} {m.group(1)}』≠ 探针实测 {pm.group(1)}")
    return problems


def verify_structure(src: str) -> list[str]:
    """结构守卫: 表格列数一致 / 裸竖线 / 悬空 §引用 / 正文未被改动。"""
    problems = []

    # 1) §13 各表列数一致 (剥行内代码后数竖线; 裸竖线会被当列分隔符)
    for sec in re.finditer(r"### (13\.\d)[^\n]*\n(.*?)(?=\n###|\n##|\Z)", src, re.S):
        name, body = sec.group(1), sec.group(2)
        # 按「连续表格块」分组: 同一节内可以有多张不同列数的表 (如 §13.4 有 2 张),
        # 混算会把合法的 3 列表与 5 列表判成"列数不一致"。
        blocks, cur = [], []
        for line in body.splitlines():
            if line.startswith("|"):
                cur.append(line)
            elif cur:
                blocks.append(cur); cur = []
        if cur:
            blocks.append(cur)
        for bi, blk in enumerate(blocks, 1):
            counts = {}
            for line in blk:
                stripped = re.sub(r"`[^`]*`", "", line)
                # 剥掉转义竖线再数 (\| 是内容不是分隔符)
                counts.setdefault(stripped.replace("\\|", "").count("|"), []).append(line[:60])
            if len(counts) > 1:
                problems.append(f"§{name} 表{bi} 竖线数不一致: {sorted(counts)} ⇒ 疑裸竖线或缺列")

    # 2) 悬空 § 引用: §13 引用的章节号必须存在于正文
    for ref in set(re.findall(r"§(\d+(?:\.\d+)?)", src)):
        base = ref.split(".")[0]
        if not re.search(rf"^##+ {re.escape(base)}[\.\s]", src, re.M) and not re.search(
            rf"^### {re.escape(ref)}", src, re.M
        ):
            problems.append(f"悬空章节引用 §{ref}")

    # 3) 必备节存在
    for needed in ("## 13. 第二 Agent 独立复查", "### 13.6 E 表", "### 13.7 复查自曝"):
        if needed not in src:
            problems.append(f"缺少必备节: {needed}")

    # 4) 行号引用必须真实 (timezone.py 闭区间那条)
    for m in re.finditer(r"app/utils/timezone\.py:(\d+)", src):
        ln = int(m.group(1))
        try:
            content = open(os.path.join(REPO, "app/utils/timezone.py"), encoding="utf-8").read().splitlines()
        except OSError as e:
            problems.append(f"无法读 timezone.py: {e}")
            continue
        if ln > len(content):
            # 越界必须优雅报红, 不许 IndexError 崩溃 (崩溃不是判定, 复查者看不出改坏了什么)
            problems.append(f"timezone.py:{ln} 越界 (文件仅 {len(content)} 行)")
        elif "16.0 <= hour_min <= 20.0" not in content[ln - 1]:
            problems.append(f"timezone.py:{ln} 行号与内容不符 (该行={content[ln-1].strip()[:50]!r})")
    return problems


def main() -> int:
    negative = "--negative" in sys.argv
    src = open(DOC, encoding="utf-8").read()
    if negative:
        # 反向能力自检: 改坏一个数字必须转红
        tampered = src.replace("真边界差异           : 0 处", "真边界差异           : 7 处", 1)
        assert tampered != src, "篡改未生效 (期望片段找不到)"
        print("[negative] 注入错误数字『7 处』, 期望 rc=1")
        ok, tot, fails = verify_e_table(tampered)
        print(f"[negative] E 表 {ok}/{tot}  fails={fails}")
        return 0 if fails else 1  # 有 fail = 守卫有牙

    print("=" * 74)
    print("DEB-20261009 §13 验证器 · E 表逐格实跑 + 结构守卫")
    print("=" * 74)
    ok, tot, fails = verify_e_table(src)
    print(f"\nE 表: {ok}/{tot} 通过")
    probe_out = _run(f"env -u PYTHONPATH venv/bin/python {PROBE_REL} 2>&1")
    claims = verify_doc_claims(src, probe_out)
    print(f"正文 claim 对账 (L 系列): {len(claims)} 处问题")
    for c in claims:
        print(f"  ⚠️ {c}")
    probs = verify_structure(src)
    print(f"结构守卫: {len(probs)} 处问题")
    for p in probs:
        print(f"  ⚠️ {p}")
    rc = 0 if (not fails and not probs and not claims) else 1
    print(f"\n{'✅ 全绿' if rc == 0 else '🔴 有失败'} (rc={rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
