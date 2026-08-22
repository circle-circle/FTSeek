#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Debate Index Builder — 辩论库索引生成器 (只读文档工具)

立项 (2026-08-20, DEB-20260820-architecture-quant-dual-audit P2-2, 白名单·做减法):
  docs/debates/README.md 的索引表是手工维护的, 曾停更 2 周 (8/06→8/20 空窗)。
  本脚本生成机器索引 index_generated.md 作为对账基线:
    - 全量 DEB 文件清单 (日期/标题/状态/讨论轮数/行数)
    - 与 README 手工索引对账: 标出"文件存在但 README 未收录"的漏登项
  **不覆盖 README 手工表** — 手工表承载决策摘要, 机器生成替不了; 只做漏登检查。

用法:
  python scripts/build_debate_index.py
输出:
  docs/debates/index_generated.md (覆盖重写, 生成物勿手改)
零行为影响: 只读 .md 文件 + 写一个生成文件, 不碰 DB/交易链路。
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

PROJECT_ROOT = Path("/opt/ftseek/FTSeek-US")
DEBATES_DIR = PROJECT_ROOT / "docs" / "debates"
README = DEBATES_DIR / "README.md"
OUT = DEBATES_DIR / "index_generated.md"

DEB_RE = re.compile(r"^DEB-(\d{8})-(.+)\.md$")


def _extract(text: str, pattern: str, maxlen: int = 160) -> str:
    m = re.search(pattern, text, re.M)
    if not m:
        return ""
    s = m.group(1).strip().strip("*").strip()
    return s[:maxlen] + ("…" if len(s) > maxlen else "")


def parse_deb(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = DEB_RE.match(path.name)
    date_raw, topic = m.group(1), m.group(2)
    try:
        date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"
    except Exception:
        date = date_raw
    title = _extract(text, r"^#\s+(.+)$", 200) or topic
    # 状态: 优先 "> **状态**: ..." 或 "状态 → ..." 或 "| 状态 | ... |"
    status = (
        _extract(text, r"\*\*状态\*\*[:：]\s*(.+)")
        or _extract(text, r"^状态\s*→\s*(.+)")
        or _extract(text, r"^\|\s*状态\s*\|\s*(.+?)\s*\|")
        or "(未标注)"
    )
    rounds = len(set(re.findall(r"第\s*(\d+)\s*轮", text)))
    return {
        "id": path.stem,
        "date": date,
        "topic": topic,
        "title": title,
        "status": status,
        "rounds": rounds,
        "lines": text.count("\n") + 1,
    }


def main():
    files = sorted(DEBATES_DIR.glob("DEB-*.md"))
    entries = [parse_deb(p) for p in files]
    entries.sort(key=lambda e: (e["date"], e["id"]))

    # README 手工索引对账
    # 2026-08-22 (结案A4): 漏登警告加 14 天窗口 — 手工索引定位"重要 DEB 才登
    # (承载决策摘要)", 历史执行记录类本不该登. 原实现对全部 101 个文件对账,
    # 92 条"需补登"警告失去判别力. 改: 仅近 14 天新文件警告需补登, 历史归档.
    readme_text = README.read_text(encoding="utf-8", errors="replace") if README.exists() else ""
    indexed_in_readme = set(re.findall(r"\[(DEB-\d{8}-[^\]]+)\]", readme_text))
    file_ids = {e["id"] for e in entries}
    missing_from_readme = sorted(file_ids - indexed_in_readme)
    stale_readme_links = sorted(indexed_in_readme - file_ids)
    today_d = dt.date.today()
    def _deb_date(mid: str):
        try:
            return dt.date(int(mid[4:8]), int(mid[8:10]), int(mid[10:12]))
        except Exception:
            return today_d
    recent_missing = [m for m in missing_from_readme
                      if (today_d - _deb_date(m)).days <= 14]
    archived_missing = [m for m in missing_from_readme if m not in recent_missing]

    today = dt.date.today().isoformat()
    out = []
    out.append("# 辩论库机器索引 (index_generated.md)")
    out.append("")
    out.append(f"> **本文件由 `scripts/build_debate_index.py` 生成** ({today}), 请勿手改。")
    out.append("> 决策摘要以 [README.md](README.md) 手工索引为准; 本页只做全量清单与漏登对账。")
    out.append("")
    out.append(f"共 **{len(entries)}** 个 DEB 文件。")
    out.append("")
    out.append("## 漏登对账")
    out.append("")
    if recent_missing:
        out.append(f"⚠️ 以下 **{len(recent_missing)}** 个近 14 天新文件未在 README 索引收录 (需补登):")
        out.append("")
        for mid in recent_missing:
            out.append(f"- `{mid}.md`")
    else:
        out.append("✅ 近 14 天新 DEB 均已在 README 索引中收录。")
    out.append("")
    if archived_missing:
        out.append(f"📦 历史归档 **{len(archived_missing)}** 个未登文件 (执行记录类, 不强制登记, 本页全量清单可查):")
        out.append("")
        for mid in archived_missing:
            out.append(f"- `{mid}.md`")
        out.append("")
    if stale_readme_links:
        out.append(f"⚠️ README 引用了 **{len(stale_readme_links)}** 个不存在的文件 (死链):")
        out.append("")
        for sid in stale_readme_links:
            out.append(f"- `{sid}`")
        out.append("")
    out.append("## 全量清单 (按日期)")
    out.append("")
    out.append("| 编号 | 日期 | 状态 | 轮数 | 行数 |")
    out.append("|---|---|---|---|---|")
    for e in entries:
        out.append(f"| [{e['id']}]({e['id']}.md) | {e['date']} | {e['status']} | "
                   f"{e['rounds'] or '-'} | {e['lines']} |")
    out.append("")
    OUT.write_text("\n".join(out), encoding="utf-8")

    print(f"[debate_index] {len(entries)} 个 DEB 文件 → {OUT}")
    if recent_missing:
        print(f"  ⚠️ README 漏登 {len(recent_missing)} 个 (近14天):")
        for mid in recent_missing:
            print(f"    - {mid}")
    else:
        print("  ✅ README 索引无漏登 (近14天)")
    if archived_missing:
        print(f"  📦 历史归档未登 {len(archived_missing)} 个 (不强制)")
    if stale_readme_links:
        print(f"  ⚠️ README 死链 {len(stale_readme_links)} 个: {stale_readme_links}")


if __name__ == "__main__":
    main()
