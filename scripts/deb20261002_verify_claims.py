"""DEB-20261002 复查守卫: 从**已落盘的 DEB 正文**解析 C 表与 E 表的命令单元格, 逐条实跑。
复查者可自行重跑: PYTHONPATH=. venv/bin/python scripts/deb20261002_verify_claims.py
判据:
  ① 命令 rc==0 且 stdout 非空
  ② 命令格剥掉 `代码块` 后残余不得含括号 (括号注解 ⇒ 复制粘贴即 shell 报错)
  ③ 命令格不得含「同上」(复查者无法独立执行)
  ④ E 表抽样重验: 期望行逐字命中 (E1/E5/E8/E11/E13/E26, 覆盖脚本类+shell类)
  ⑤ 文档级守卫: 表格列数一致 / 章节号无缺号重号 / §引用无悬空 (scripts/deb20261002_doc_guard.py)
"""
import re
import subprocess

BASE = "/opt/ftseek/FTSeek-US"
P = f"{BASE}/docs/debates/DEB-20261002-universe-and-direction-entry-error.md"
s = open(P).read()
lines = s.split("\n")


def cells(row):
    """拆 markdown 表格行, 尊重 \\| 转义"""
    tmp = row.replace("\\|", "\x00")
    parts = [p.replace("\x00", "|").strip() for p in tmp.split("|")]
    return [p for p in parts if p != ""]


def code_spans(cell):
    return re.findall(r"`([^`]+)`", cell)


# ── 1. 收集 C 表与 E 表命令行 ──────────────────────────────
targets = []
for i, l in enumerate(lines, start=1):
    m = re.match(r"^\| \*\*(C\d+|E\d+[a-z]?)\*\*", l)
    if not m:
        continue
    cs = cells(l)
    eid = m.group(1)
    # C 表: | 编号 | 声称 | 复查命令 | 反例判据 |  → 命令在第3格
    # E 表: | 编号 | 声称 | 复现命令 | 期望输出 |  → 命令在第3格
    cmdcell = cs[2] if len(cs) >= 3 else ""
    targets.append((eid, i, cmdcell, cs))

print(f"=== 解析到 {len(targets)} 条 (C表+E表) ===")
bad_suffix, bad_run = [], []
for eid, ln, cmdcell, cs in targets:
    # ③ 结构性规则: 命令单元格剥掉所有 `代码块` 后, 剩余文本不得含括号
    #    (括号注解 = 复查者复制即报错; 人群定义类"读 X"格无代码块, 本规则不触发)
    cmd_spans = [sp for sp in code_spans(cmdcell)
                 if sp.startswith(("PYTHONPATH=", "git ", "venv/", "wc "))]
    residue = re.sub(r"`[^`]*`", "", cmdcell)
    if cmd_spans and re.search(r"[（(]", residue):
        bad_suffix.append((eid, ln, cmdcell[:110]))
    # ③b 命令格出现「同上」= 复查者无法独立复制执行
    if "同上" in cmdcell:
        bad_suffix.append((eid, ln, "含「同上」,不可独立复制: " + cmdcell[:80]))
    spans = code_spans(cmdcell)
    if not spans:
        bad_run.append((eid, ln, "无代码块命令", ""))
        continue
    for cmd in spans:
        if not (cmd.startswith("PYTHONPATH=") or cmd.startswith("git ")
                or cmd.startswith("wc ") or cmd.startswith("venv/")):
            continue  # 非可执行命令(如期望值里的字面量), 跳过
        r = subprocess.run(cmd, shell=True, cwd=BASE, capture_output=True,
                           text=True, timeout=500)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            bad_run.append((eid, ln, cmd[:90],
                            f"rc={r.returncode} out={out[:80]!r} err={(r.stderr or '')[:120]!r}"))

print(f"\n① 命令单元格含人类注解后缀: {len(bad_suffix)}")
for eid, ln, c in bad_suffix:
    print(f"   🔴 {eid} line{ln}: {c}")
print(f"② 命令跑不通/空输出: {len(bad_run)}")
for eid, ln, c, why in bad_run:
    print(f"   🔴 {eid} line{ln}: {c}\n      {why}")

# ── 2. E 表抽样重验 (期望行逐字命中) ──────────────────────────
SAMPLE = ["E1", "E5", "E8", "E11", "E13", "E26"]
print(f"\n=== E 表抽样重验 {SAMPLE} ===")
sample_fail = []
for eid, ln, cmdcell, cs in targets:
    if eid not in SAMPLE:
        continue
    cmd = code_spans(cmdcell)[0]
    expect_cell = cs[3] if len(cs) >= 4 else ""
    expects = [x.strip() for x in re.findall(r"`([^`]+)`", expect_cell)]
    r = subprocess.run(cmd, shell=True, cwd=BASE, capture_output=True,
                       text=True, timeout=500)
    out = (r.stdout or "") + (r.stderr or "")
    miss = [e for e in expects if e not in out]
    if miss:
        sample_fail.append((eid, miss))
        print(f"   🔴 {eid} 缺 {len(miss)}/{len(expects)}")
        for m in miss[:3]:
            print(f"        {m!r}")
    else:
        print(f"   ✅ {eid} {len(expects)}/{len(expects)} 逐字命中")

# ── 3. 文档级守卫复跑 ───────────────────────────────────────
print("\n=== 文档级守卫 (表格列数/章节号/§引用) ===")
r = subprocess.run("PYTHONPATH=. timeout 120 venv/bin/python "
                   "scripts/deb20261002_doc_guard.py",
                   shell=True, cwd=BASE, capture_output=True, text=True, timeout=180)
for l in (r.stdout or "").split("\n"):
    if any(k in l for k in ("发现", "✅", "🔴", "悬空")):
        print(f"   {l}")
print(f"   rc={r.returncode}")

ok = not bad_suffix and not bad_run and not sample_fail and r.returncode == 0
print(f"\n{'✅ 全部通过' if ok else '🔴 有失败项'}")
raise SystemExit(0 if ok else 1)
