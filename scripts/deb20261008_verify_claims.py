#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261008] 文档 claim ↔ DB 实测对账器 (真守卫: 不硬编码期望值, 从 DEB 正文提取).

为什么需要: 第一版对账脚本把期望值硬编码成 79.0%/+2.0pp/6条, 文档改成真值后**脚本反而报红**
  ⇒ 那是「脚本陈旧」不是「文档错」。本脚本改为从 DEB 正文 regex 提取 claim, 再与 DB 实测比对,
  这样文档与数据任一漂移都会被抓, 且不需要每次改数字都同步改守卫。

覆盖的 claim (每个都给提取正则 + 实测重算):
  C1 L3 最终多头占比        正文「多头 XX.X%」
  C2 闸的总量贡献 pp        正文「只贡献 +X.Xpp」
  C3 UE 通道空头占比        正文「空头占比 XX.X%」
  C4 P1-4 漏计空头条数      正文「其中 N 条 SELL_SHORT」
  C5 被拦行 fwd 填充率      正文 §13「F4 后被拦行填充率实测口径 **X%**」(F4 后口径, 曾 0.0%)
  C6 对照行 fwd 填充率      正文「成交行 XXX.X%」
  C7 空头清零日天数         正文「= N 天」

只读。任意 cwd + 无 PYTHONPATH 可冷跑。退出码非 0 = 有不一致 (可作 CI 守卫)。
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEB = ROOT / "docs/debates/DEB-20261008-direction-gates-long-short-bias.md"
# 复现脚本禁止依赖的临时路径 (拼接构造以免本文件自命中检测行)
_NEEDLES = ("hermes/cache/" + "scratch", '"' + "/" + "tmp/", "'" + "/" + "tmp/")
SINCE = "2026-09-17"
DAY = "2026-10-07"


def _ro(p: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _grab(txt: str, pattern: str, label: str) -> float | None:
    """从正文提取第一个数字 claim; 找不到返回 None 并留痕 (不静默通过)."""
    m = re.search(pattern, txt)
    if not m:
        print(f"  ⚠️ {label}: 正则未命中 (pattern={pattern!r}) — 文档措辞可能已改")
        return None
    return float(m.group(1))


def main() -> int:
    if not DEB.exists():
        print(f"🔴 DEB 不存在: {DEB}")
        return 1
    txt = DEB.read_text(encoding="utf-8")
    conn = _ro(ROOT / "data/ftseek_us.db")
    gate = ROOT / "data/da_gate_decisions.jsonl"

    bad: list[str] = []

    def check(label: str, claim: float | None, actual: float, tol: float = 0.06) -> None:
        if claim is None:
            bad.append(f"{label}: 无法从文档提取 claim (不静默通过)")
            return
        ok = abs(claim - actual) <= tol
        print(f"  {'✅' if ok else '🔴'} {label:26s} 文档={claim:<8g} 实测={actual:<8.1f} 差={claim-actual:+.2f}")
        if not ok:
            bad.append(f"{label}: 文档 {claim} ≠ 实测 {actual:.1f}")

    # ── 实测重算 (与 deb20261008_bias_funnel.py 同口径) ──────────────────────
    l1b = l1s = 0
    surv_b = surv_s = 0
    n_bad_gate = 0
    day_l1: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for ln in gate.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            n_bad_gate += 1  # 2026-10-08 棘轮清剿: 坏行计数留痕 (原为静默 continue)
            continue
        if not isinstance(d, dict) or str(d.get("trade_date") or "") < SINCE:
            continue
        dirn = str(d.get("direction") or "")
        dec = str(d.get("decision") or "")
        day_l1[str(d["trade_date"])][dirn] += 1
        if dirn == "BUY":
            l1b += 1
            if dec in ("PASS", "WARN"):
                surv_b += 1
        elif dirn == "SELL_SHORT":
            l1s += 1
            if dec in ("PASS", "WARN"):
                surv_s += 1
    p1 = 100.0 * l1b / (l1b + l1s) if (l1b + l1s) else 0.0
    if n_bad_gate:
        print(f"  [warn] gate jsonl 坏行跳过 {n_bad_gate} 条", file=sys.stderr)

    fin_l = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date>=?
                            AND direction='BUY' AND (skip_reason IS NULL OR skip_reason='')""",
                         (SINCE,)).fetchone()[0]
    fin_s = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date>=?
                            AND direction='SELL_SHORT' AND (skip_reason IS NULL OR skip_reason='')""",
                         (SINCE,)).fetchone()[0]
    p3 = 100.0 * fin_l / (fin_l + fin_s) if (fin_l + fin_s) else 0.0

    ue_l = ue_s = 0
    for r in conn.execute("""SELECT direction FROM playbook_instructions WHERE date>=?
                             AND raw_instruction_json LIKE '%ue_candidate%'""", (SINCE,)):
        if r["direction"] == "BUY":
            ue_l += 1
        elif r["direction"] == "SELL_SHORT":
            ue_s += 1
    ue_pct = 100.0 * ue_s / (ue_l + ue_s) if (ue_l + ue_s) else 0.0

    miss_s = 0
    for r in conn.execute("""SELECT direction, skip_reason, decision_id FROM playbook_instructions
                             WHERE date>=date(?,'-14 day')""", (DAY,)):
        did = str(r["decision_id"] or "")
        if did.startswith("DA-") or r["skip_reason"]:
            continue
        if r["direction"] == "SELL_SHORT":
            miss_s += 1

    CUT = ("skip_reason LIKE '%方向偏斜%' OR skip_reason LIKE '%sector%' "
           "OR skip_reason LIKE '%行业集中度%' OR skip_reason LIKE '%组合方向偏斜%'")
    q = conn.execute(f"""SELECT COUNT(*) n, SUM(fwd_return_intraday IS NOT NULL) fi
                         FROM playbook_instructions WHERE date>=? AND ({CUT})""", (SINCE,)).fetchone()
    q2 = conn.execute("""SELECT COUNT(*) n, SUM(fwd_return_intraday IS NOT NULL) fi
                         FROM playbook_instructions WHERE date>=?
                         AND direction IN ('BUY','SELL_SHORT')
                         AND (skip_reason IS NULL OR skip_reason='')""", (SINCE,)).fetchone()
    cut_pct = 100.0 * (q["fi"] or 0) / q["n"] if q["n"] else 0.0
    ctrl_pct = 100.0 * (q2["fi"] or 0) / q2["n"] if q2["n"] else 0.0

    zero_days = 0
    for d, cnt in day_l1.items():
        if cnt["SELL_SHORT"] <= 0:
            continue
        fs = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date=?
                             AND direction='SELL_SHORT' AND (skip_reason IS NULL OR skip_reason='')""",
                          (d,)).fetchone()[0]
        if fs == 0:
            zero_days += 1

    print("=" * 100)
    print("claim ↔ 实测对账 (从 DEB 正文提取, 不硬编码)")
    print("=" * 100)
    check("C1 L3 最终多头占比",
          _grab(txt, r"L3 过完组合层闸:\s+BUY\s+\d+ / SHORT \d+\s+⇒ 多头 (\d+\.\d)%", "C1"), p3)
    check("C2 闸的总量贡献 pp", _grab(txt, r"只贡献 \+(\d+\.\d)pp", "C2"), p3 - p1)
    check("C3 UE 空头占比", _grab(txt, r"空头占比 (\d+\.\d)%", "C3"), ue_pct)
    check("C4 漏计空头条数", _grab(txt, r"其中 (\d+) 条 SELL_SHORT", "C4"), float(miss_s), tol=0.0)
    check("C5 被拦行 fwd 填充率", _grab(txt, r"后被拦行填充率实测口径 \*\*(\d+\.\d)%\*\*", "C5"), cut_pct)
    check("C6 对照行 fwd 填充率", _grab(txt, r"成交行 (\d+\.\d)%", "C6"), ctrl_pct)
    check("C7 空头清零日天数", _grab(txt, r"的日子 = (\d+) 天", "C7"), float(zero_days), tol=0.0)

    print("\n" + "=" * 100)
    print("结构自检")
    print("=" * 100)
    lines = txt.splitlines()
    in_code = False
    blocks: list[list[int]] = []
    cur: list[int] = []
    for i, l in enumerate(lines, 1):
        if l.strip().startswith("```"):
            in_code = not in_code
        is_row = (not in_code) and l.strip().startswith("|")
        if is_row:
            cur.append(i)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    n_bad = 0
    for b in blocks:
        cnts = collections.Counter(re.sub(r"`[^`]*`", "", lines[i - 1]).count("|") for i in b)
        maj = cnts.most_common(1)[0][0]
        for i in b:
            p = re.sub(r"`[^`]*`", "", lines[i - 1]).count("|")
            if p != maj:
                n_bad += 1
                print(f"  🔴 line {i}: pipes={p} vs 多数={maj}  {lines[i-1][:88]}")
    print(f"  表格列数不一致行: {n_bad}")
    if n_bad:
        bad.append(f"表格列数不一致 {n_bad} 行")

    secs = set(re.findall(r"^## (\d+)\.", txt, re.M))
    refs = set(re.findall(r"§(\d+)", txt))
    missing = sorted(r for r in refs if r not in secs)
    print(f"  实有章节 {sorted(secs, key=int)}")
    print(f"  引用不存在章节: {missing if missing else '(无)'}")
    if missing:
        bad.append(f"引用不存在章节 {missing}")

    # scratch 依赖检查 (纪律: 复现脚本不得依赖 /tmp 或 hermes scratch)
    #   两条纪律约束:
    #   (a) 只查**代码行**的真实路径依赖, 跳过注释与 docstring ⇒ 否则本守卫的 docstring
    #       提到 scratch 就会自指报红 (第一版实测踩到)。
    #   (b) 只查本 DEB 附录明确列名的 4 个脚本, **不用 glob** ⇒ 同日期前缀下可能有其他
    #       agent 并行新建的脚本 (实测存在 deb20261008_scaleout_*.py 3 个), glob 会让本守卫
    #       因他人的文件漂移而报红, 也可能把他人的文件卷入本批提交。
    OWN = ["deb20261008_direction_gate_census.py",
           "deb20261008_gate_attribution.py",
           "deb20261008_bias_funnel.py",
           "deb20261008_verify_claims.py"]
    # 自指排除: 本文件是 _NEEDLES 的**定义处**, 字面量必然出现在自己的源码里
    #   (实测三版均自命中: 检测行里写 needle ⇒ needle 命中检测行)。
    #   因此本文件跳过 scratch/tmp 检查, 但仍参与「文件必须存在」检查。
    #   其余 3 个附录脚本一律照检, 不排除。
    SKIP_SELF = {Path(__file__).resolve().name}
    for name in OWN:
        f = ROOT / "scripts" / name
        if not f.exists():
            bad.append(f"附录脚本缺失: {name}")
            print(f"  🔴 {name} 不存在")
            continue
        if name in SKIP_SELF:
            continue
        in_doc = False
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            st = line.strip()
            if st.startswith('\"\"\"') and st.count('\"\"\"') == 1:
                in_doc = not in_doc
                continue
            if in_doc or st.startswith("#"):
                continue
            needles = _NEEDLES
            if any(nd in line for nd in needles):
                bad.append(f"{name}:{i} 依赖 scratch/tmp")
                print(f"  🔴 {name}:{i} 代码行含 scratch/tmp 依赖: {st[:80]}")
    print(f"  本 DEB 附录脚本 (只查这 4 个, 不 glob): {len(OWN)} 个, 全部存在={all((ROOT/'scripts'/n).exists() for n in OWN)}")
    others = sorted(x.name for x in ROOT.glob("scripts/deb20261008_*.py") if x.name not in OWN)
    if others:
        print(f"  ℹ️ 同日期前缀下另有他人/他议题脚本 (本守卫不检查、不提交): {others}")

    print("\n" + "=" * 100)
    if bad:
        print(f"🔴 对账失败 {len(bad)} 处:")
        for x in bad:
            print(f"   · {x}")
        return 1
    print("✅ claim ↔ 实测全部一致, 结构自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
