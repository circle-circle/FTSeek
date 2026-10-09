#!/usr/bin/env python3
"""[DEB-20261009-event-to-trade 验证器] §11.7 / §12.7 / §13.7 三张 E 表逐格实跑 + 结构守卫.

跑法:
    cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/verify_deb20261009_event.py
    venv/bin/python scripts/verify_deb20261009_event.py --negative   # 反向能力自检

设计:
- E 表期望片段支持 `同上` 继承 (与 verify_deb20261008.py 同款), 探针脚本只跑一次。
- 期望列可含多片段 (` 与 ` 连接), 全部命中才通过。
- 单元格切分只切**未转义**竖线 (命令里的 \\| 是 shell 管道, 切了会把命令截断)。
- L 系列: 从 §13 正文 regex 提取 claim 与探针实测对账 (篡改正文数字也要抓到)。
- 结构守卫按「连续表格块」分组 (同节内可有多张不同列数的表)。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs/debates/DEB-20261009-event-to-trade-pipeline.md")
PROBE_REL = "scripts/deb20261009_event_pipeline_probe.py"


def _run(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO, timeout=300)
    return (r.stdout or "") + (r.stderr or "")


def _parse_e_rows(source: str) -> list[dict]:
    """解析一段文本中的 E 表行; §14 P0 后与旧逻辑解耦."""
    rows = []
    last_cmd = None
    for line in source.splitlines():
        if not re.match(r"^\| E\d+", line):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 3:
            continue
        row_id, raw_cmd, expect = cells[0], cells[1], cells[2]
        cmd = raw_cmd.strip("`")
        if cmd in ("同上", ""):
            assert last_cmd, f"{row_id} 用『同上』但无上一条命令"
            cmd = last_cmd
        else:
            last_cmd = cmd
        rows.append({
            "id": row_id,
            "cmd": cmd.replace("\\|", "|"),
            "expect": expect.strip("`").replace("\\|", "|"),
        })
    return rows


def parse_e_tables(src: str) -> tuple[list[dict], list[dict]]:
    """解析 E 表; `同上` 继承上一条命令。

    返回 (own, upstream): own = §13.7 (本复查自建, 失败计入 rc);
    upstream = §11.7/§12.7 (并行 agent 的节, 只报告可跑性、不计 rc — 不代他人修节,
    但其命令不可复跑/期望列为描述文字属复查发现, 登记进 §13.9)。
    """
    own, upstream = [], []
    i13 = src.find("## 13. 第二 Agent 独立复查")
    i14 = src.find("## 14. P0 前置执行记录")
    if i14 >= 0:
        return _parse_e_rows(src[i14:]), _parse_e_rows(src[:i13])
    for sec in re.finditer(r"###? (\d+\.\d+)? ?E 表[^\n]*\n(.*?)(?=\n##|\Z)", src, re.S):
        in_own = i13 >= 0 and sec.start() > i13
        last_cmd = None
        for line in sec.group(0).splitlines():
            if not re.match(r"^\| E\d+", line):
                continue
            # 只切未转义竖线 — 命令里的 \| 是 shell 管道
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
            cmd = cmd.replace("\\|", "|")
            expect = expect.strip("`").replace("\\|", "|")
            (own if in_own else upstream).append({"id": eid, "cmd": cmd, "expect": expect})
    return own, upstream


def _check_rows(rows: list[dict], probe_cache: dict) -> tuple[int, int, list[str]]:
    ok, fails = 0, []
    for r in rows:
        if PROBE_REL in r["cmd"]:
            if "out" not in probe_cache:
                probe_cache["out"] = _run(f"env -u PYTHONPATH venv/bin/python {PROBE_REL} 2>&1")
            out = probe_cache["out"]
        else:
            out = _run(r["cmd"] + " 2>&1")
        frags = [f.strip().strip("`") for f in re.split(r"`?\s+与\s+`?", r["expect"]) if f.strip()]
        missing = [f for f in frags if f and f not in out]
        if frags and not missing:
            ok += 1
            print(f"  ✅ {r['id']:<5} {r['expect'][:62]}")
        else:
            fails.append(r["id"])
            print(f"  ❌ {r['id']:<5} missing={[m[:60] for m in missing]!r}")
            print(f"       got={out.strip()[:150]!r}")
    return ok, len(rows), fails


def verify_e_table(src: str) -> tuple[int, int, list[str], list[str]]:
    own, upstream = parse_e_tables(src)
    label = "--- §14.3 P0 后 E 表 (失败计入 rc) ---" if "## 14. P0 前置执行记录" in src else "--- §13.7 本复查 E 表 (失败计入 rc) ---"
    print(label)
    ok, tot, fails = _check_rows(own, {})
    print("\n--- §11.7/§12.7 上游 E 表 (只报告可跑性, 不计 rc; 期望列多为描述文字属已知形态) ---")
    u_ok, u_tot, u_fails = _check_rows(upstream, {})
    return ok, tot, fails, u_fails


def verify_doc_claims(src: str, probe_out: str) -> list[str]:
    """L 系列: §13 正文 claim ↔ 探针实测对账 (期望值不硬编码, 全部现算)."""
    problems = []
    i14 = src.find("## 14. P0 前置执行记录")
    i13 = src.find("## 13. 第二 Agent 独立复查")
    if i14 >= 0:
        sec13 = src[i14:]
    elif i13 >= 0:
        sec13 = src[i13:]
    else:
        return ["找不到 §13/§14"]

    # L1 词表命中 0/4
    m = re.search(r"事件样本 (\d+)/(\d+) 命中，阳性对照 (\d+)/(\d+) 命中", sec13)
    pm = re.search(r"事件样本词表命中\s+: (\d+)/(\d+)", probe_out)
    pc = re.search(r"阳性对照命中\s+: (\d+)/(\d+)", probe_out)
    if m and pm and pc:
        if (m.group(1), m.group(2)) != (pm.group(1), pm.group(2)):
            problems.append(f"L1: 正文『事件样本 {m.group(1)}/{m.group(2)}』≠ 探针 {pm.group(1)}/{pm.group(2)}")
        if (m.group(3), m.group(4)) != (pc.group(1), pc.group(2)):
            problems.append(f"L1: 正文『对照 {m.group(3)}/{m.group(4)}』≠ 探针 {pc.group(1)}/{pc.group(2)}")
    elif not m:
        problems.append("L1: §13.3 找不到『事件样本 N/M 命中』claim")

    # L2 catalyst 消费方文件数
    m = re.search(r"\*\*(\d+) 个文件\*\*", sec13)
    pm = re.search(r"CatalystClassifier 消费方文件数 : (\d+)", probe_out)
    if m and pm and m.group(1) != pm.group(1):
        problems.append(f"L2: 正文『{m.group(1)} 个文件』≠ 探针 {pm.group(1)}")

    # L3 HARD_EVENT 产出率
    m = re.search(r"\*\*(\d+)/(\d+) = ([\d.]+)%\*\*", sec13)
    pm = re.search(r"HARD_EVENT 总产出率\s+: (\d+)/(\d+)", probe_out)
    if m and pm:
        if (m.group(1), m.group(2)) != (pm.group(1), pm.group(2)):
            problems.append(f"L3: 正文『{m.group(1)}/{m.group(2)}』≠ 探针 {pm.group(1)}/{pm.group(2)}")
        elif abs(float(m.group(3)) - 100.0 * int(pm.group(1)) / int(pm.group(2))) > 0.05:
            problems.append(f"L3: 正文百分比 {m.group(3)}% 与 {pm.group(1)}/{pm.group(2)} 不符")

    if i14 >= 0:
        return problems

    # L4 news_quality.db 死表读数 (字面查找, 不用 regex — 正文含 markdown 粗体星号)
    for frag, label in (("37 行", "行数"), ("**2/37**", "tickers 非空"),
                        ("**0/37**", "sentiment≠0"), ("('other', 37)", "event_type 分布")):
        if frag not in sec13:
            problems.append(f"L4: §13.5 找不到『{frag}』({label})")

    # L5 REGULATORY 单向声称
    if not re.search(r"REGULATORY` 全 9 条 direction=SHORT", sec13):
        problems.append("L5: §13.3 找不到『REGULATORY 全 9 条 SHORT』claim")
    return problems


def verify_structure(src: str) -> list[str]:
    problems = []
    # 1) 表格块列数一致 (按连续块分组)
    for sec in re.finditer(r"^### ((?:13|14)\.\d)[^\n]*\n(.*?)(?=\n###|\n##|\Z)", src, re.S | re.M):
        name, body = sec.group(1), sec.group(2)
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
                counts.setdefault(stripped.replace("\\|", "").count("|"), []).append(line[:60])
            if len(counts) > 1:
                problems.append(f"§{name} 表{bi} 竖线数不一致: {sorted(counts)} ⇒ 疑裸竖线或缺列")

    # 2) 悬空章节引用 (f-string 内不得含反斜杠 ⇒ 先拼好 pattern 再传)
    for ref in set(re.findall(r"§(\d+(?:\.\d+)?)", src)):
        base = ref.split(".")[0]
        pat_h2 = r"^##+ " + re.escape(base) + r"[\.\s]"
        pat_h3 = r"^### " + re.escape(ref)
        if not re.search(pat_h2, src, re.M) and not re.search(pat_h3, src, re.M):
            problems.append(f"悬空章节引用 §{ref}")

    # 3) 必备节
    needed = ("## 13. 第二 Agent 独立复查", "### 13.7 E 表", "### 13.8 复查自曝")
    if "## 14. P0 前置执行记录" in src:
        needed = needed + ("## 14. P0 前置执行记录", "### 14.3 E 表")
    for item in needed:
        if item not in src:
            problems.append(f"缺少必备节: {item}")

    # 4) 行号引用真实性
    srcfile = os.path.join(REPO, "config/unified_config.yaml")
    for m in re.finditer(r"unified_config\.yaml:(\d+)", src):
        ln = int(m.group(1))
        try:
            content = open(srcfile, encoding="utf-8").read().splitlines()
        except OSError as e:
            problems.append(f"无法读 unified_config.yaml: {e}")
            break
        if ln > len(content):
            problems.append(f"unified_config.yaml:{ln} 越界 (文件仅 {len(content)} 行)")
    return problems


def main() -> int:
    negative = "--negative" in sys.argv
    src = open(DOC, encoding="utf-8").read()
    if negative:
        if "## 14. P0 前置执行记录" in src:
            tampered = src.replace("事件样本 3/4 命中，阳性对照 2/2 命中",
                                   "事件样本 4/4 命中，阳性对照 0/2 命中", 1)
        else:
            tampered = src.replace("事件样本 0/4 命中，阳性对照 2/2 命中",
                                   "事件样本 4/4 命中，阳性对照 0/2 命中", 1)
        assert tampered != src, "篡改未生效"
        print("[negative] 注入反向数字 (0/4→4/4, 2/2→0/2), 期望 rc=1")
        probe_out = _run(f"env -u PYTHONPATH venv/bin/python {PROBE_REL} 2>&1")
        claims = verify_doc_claims(tampered, probe_out)
        # L2 消费方数篡改也必须抓到
        print(f"[negative] L 系列 {len(claims)} 处问题: {claims[:3]}")
        return 0 if claims else 1

    print("=" * 76)
    print("DEB-20261009-event-to-trade 验证器 · E 表逐格实跑 + L 对账 + 结构守卫")
    print("=" * 76)
    ok, tot, fails, u_fails = verify_e_table(src)
    print(f"\n§13.7 E 表: {ok}/{tot} 通过")
    print(f"§11.7/§12.7 上游 E 表: {len(u_fails)} 条不可机械比对 (登记为发现, 不计 rc)")
    probe_out = _run(f"env -u PYTHONPATH venv/bin/python {PROBE_REL} 2>&1")
    claims = verify_doc_claims(src, probe_out)
    print(f"正文 claim 对账 (L 系列): {len(claims)} 处问题")
    for c in claims:
        print(f"  ⚠️ {c}")
    probs = verify_structure(src)
    print(f"结构守卫: {len(probs)} 处问题")
    for p in probs:
        print(f"  ⚠️ {p}")
    rc = 0 if (not fails and not claims and not probs) else 1
    print(f"\n{'✅ 全绿' if rc == 0 else '🔴 有失败'} (rc={rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
