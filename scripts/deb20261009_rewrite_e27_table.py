"""§27.7 E 表改回验证器契约的 3 列形态 (| # | 命令 | 期望输出 |)。

验证器 parse_e_tables 按位置取 cells[0]=id, cells[1]=cmd, cells[2]=expect,
4 列表会把「断言」列当命令 ⇒ 全红。断言描述移到表前列表, 不塞进 expect
(expect 必须是命令输出的字面子串)。
"""
import json
import re
from pathlib import Path

DEB = Path("/opt/ftseek/FTSeek-US/docs/debates/DEB-20261009-event-to-trade-pipeline.md")
E = json.load(open("/root/.hermes/cache/scratch/e27.json", encoding="utf-8"))

HEAD = """### 27.7 E 表（本节证据，逐条可复跑）

> 期望列**全部程序化取自实跑输出**（`gen_e27.py` 生成，禁手抄）。表形态遵守
> `scripts/verify_deb20261009_event.py::parse_e_tables` 的 **3 列契约**
> （`| # | 命令 | 期望输出 |`）—— 首版写成 4 列（多一列「断言」），验证器按位置取
> `cells[1]` 当命令 ⇒ 12 条全红，已改回（见 §27.8-6）。断言描述列在表前，不塞进期望列
> （期望必须是命令输出的字面子串）。
> `grep -c` 零命中时 rc=1 属 grep 语义，故 E63 带 `|| true`（0 命中正是期望）。

**各条断言内容**：

"""

titles = "\n".join(f"- `{x['id']}` — {x['title']}" for x in E)

rows = []
for x in E:
    cmd = x["cmd"].replace("|", "\\|").replace("\n", " ")
    got = x["got"].replace("|", "\\|")
    rows.append(f"| {x['id']} | `{cmd}` | `{got}` |")

table = (
    HEAD + titles + "\n\n"
    "| # | 命令 | 期望输出（2026-10-10 实跑） |\n"
    "|---|---|---|\n"
    + "\n".join(rows) + "\n"
)

src = DEB.read_text(encoding="utf-8")
pat = re.compile(r"### 27\.7 E 表.*?(?=\n### 27\.8 )", re.S)
m = pat.search(src)
assert m, "未找到 §27.7 节"
new_src = src[: m.start()] + table + src[m.end():]
DEB.write_text(new_src, encoding="utf-8")
print(f"§27.7 已改为 3 列契约形态。文件行数 {src.count(chr(10))+1} → {new_src.count(chr(10))+1}")
# 列数自检
sec = pat.search(new_src).group(0)
bad = [l for l in sec.splitlines()
       if l.startswith("| E") and len(re.split(r"(?<!\\)\|", l.strip().strip("|"))) != 3]
print(f"非 3 列的 E 行数: {len(bad)}")
for b in bad[:3]:
    print("  ", b[:110])
for anchor in ("### 27.8 复查自曝", "### 27.9 交下一 agent", "## 26. §25 复查"):
    print(f"  {anchor}: {'✓' if anchor in new_src else '✗ 丢失'}")
