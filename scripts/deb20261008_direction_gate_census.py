#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261008] E1–E3 方向闸全清单普查 + 下游消费者 + 开关可得性.

只读。任意 cwd + 无 PYTHONPATH 可冷跑:
    cd /tmp && env -u PYTHONPATH /opt/ftseek/FTSeek-US/venv/bin/python \
        /opt/ftseek/FTSeek-US/scripts/deb20261008_direction_gate_census.py

判据锚点 (逐个 grep 出的真实代码位置, 非推断):
  G1 DA 破位硬买拦截   skill signal_gate.py:428  `scanner_direction=="bearish" and direction=="BUY"` → tier=SKIP
  G2 DA 方向冲突降档   skill signal_gate.py:119-132 + 419-422  quality_score -= 10 且 tier 降 2 档
  G3 UE 动量门        scripts/llm_universe_scan.py:588 `_long_momentum_gate` (仅 direction=="LONG")
  G4 生成期方向偏斜    app/playbook/pipeline.py:2019 调用 + 2179 判定 (max_direction_skew=0.80)
  G5 执行前组合偏斜    app/playbook/portfolio_execution_guardrail.py (MAX_DIRECTION_SKEW=0.80 常量)
  G6 行业集中度       pipeline.py:2189 + portfolio_execution_guardrail.py (MAX_SECTOR_PCT=0.40)
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
DAY = "2026-10-07"
SINCE = "2026-09-17"          # 近 ~15 交易日


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _grep(pattern: str, paths: list[str], limit: int = 8) -> list[str]:
    """在仓内 grep, 返回 'path:line:text' 列表 (只读, 失败返回空)."""
    cmd = ["grep", "-rn", "--include=*.py", "--include=*.yaml", pattern] + paths
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=90)
    except Exception:
        return []
    out = [l for l in r.stdout.splitlines() if l.strip()]
    return out[:limit]


def section(title: str) -> None:
    print("\n" + "=" * 106)
    print(title)
    print("=" * 106)


def e1_gate_inventory() -> None:
    """E1: 六道闸的代码锚点实存性 (每行给可复跑的 grep)."""
    section("E1 六道方向闸的代码锚点 (实存性核验, 非文档转述)")
    gates = [
        ("G1", "DA 破位硬买拦截", "硬 SKIP",
         'scanner_direction == "bearish" and direction == "BUY"',
         str(SKILL / "signal_gate.py"), "仅拦 BUY ⇒ 结构上不可能拦 SHORT"),
        ("G2", "DA 方向冲突降档", "soft: -10分 + tier降2档",
         "dir_conflict and tier in", str(SKILL / "signal_gate.py"), "双向对称命中, 但降2档常跌出 PASS"),
        ("G3", "UE 动量门", "硬 SKIP",
         'def _long_momentum_gate', str(ROOT / "scripts/llm_universe_scan.py"), "仅 direction=='LONG'"),
        ("G4", "生成期方向偏斜门", "降级 SKIP",
         "max_direction_skew", str(ROOT / "app/playbook/pipeline.py"), "分母=builder 本批 valid"),
        ("G5", "执行前组合偏斜门", "拒绝执行 + 写 SKIP",
         "MAX_DIRECTION_SKEW", str(ROOT / "app/playbook/portfolio_execution_guardrail.py"),
         "分母=当日全域 (跨写入方); Codex 10adea0c8 新增"),
        ("G6", "行业集中度门", "降级 SKIP",
         "MAX_SECTOR_PCT", str(ROOT / "app/playbook/portfolio_execution_guardrail.py"), "非方向闸但会砍单"),
    ]
    print(f"  {'ID':3s} {'闸名':20s} {'类型':22s} {'锚点实存':8s} 判据/备注")
    for gid, name, kind, anchor, fpath, note in gates:
        p = Path(fpath)
        exists = "✅" if p.exists() and anchor in p.read_text(encoding="utf-8", errors="replace") else "🔴缺失"
        # 找出行号
        lineno = "-"
        if p.exists():
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if anchor in line:
                    lineno = str(i)
                    break
        print(f"  {gid:3s} {name:20s} {kind:22s} {exists:8s} {p.name}:{lineno}")
        print(f"      判据串: {anchor!r}")
        print(f"      备注: {note}")


def e2_block_volume() -> None:
    """E2: 各闸近 ~15 交易日实测拦截量 (DB skip_reason + DA gate jsonl + UE 影子)."""
    section(f"E2 各闸实测拦截量 (交易日 >= {SINCE}, 至 {DAY})")
    conn = _ro(ROOT / "data/ftseek_us.db")

    pat = collections.Counter()
    for r in conn.execute(
        """SELECT skip_reason FROM playbook_instructions
           WHERE date>=? AND skip_reason IS NOT NULL AND skip_reason!=''""", (SINCE,)
    ):
        s = str(r["skip_reason"])
        if "组合方向偏斜" in s:
            pat["G5 执行前偏斜"] += 1
        elif "方向偏斜" in s:
            pat["G4 生成期偏斜"] += 1
        elif "行业集中度" in s or "sector" in s:
            pat["G6 行业集中度"] += 1
    print("  --- 组合层 (DB skip_reason 侧) ---")
    for k in ("G4 生成期偏斜", "G5 执行前偏斜", "G6 行业集中度"):
        print(f"    {k:18s} {pat.get(k, 0):4d} 次")
    print(f"    合计 {sum(pat.values())} 次")

    gj = collections.Counter()
    gdir = collections.defaultdict(collections.Counter)
    p = ROOT / "data/da_gate_decisions.jsonl"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if not isinstance(d, dict) or str(d.get("trade_date") or "") < SINCE:
                continue
            rs = " ".join(str(x) for x in (d.get("reasons") or []))
            dirn = str(d.get("direction") or "?")
            if "破位硬买拦截" in rs:
                gj["G1 破位硬买 (硬BLOCK)"] += 1
                gdir["G1"][dirn] += 1
            if "方向冲突" in rs:
                gj["G2 方向冲突 (降档命中)"] += 1
                gdir["G2"][dirn] += 1
    print("\n  --- DA 生成期 (da_gate_decisions.jsonl 侧) ---")
    for k, v in gj.most_common():
        print(f"    {k:26s} {v:4d} 次   原方向分布={dict(gdir[k.split()[0]].most_common())}")
    if not gj:
        print("    (无记录)")

    p3 = ROOT / "data/shadow/ue_long_momentum_gate.jsonl"
    print("\n  --- UE 侧 (影子) ---")
    if p3.exists():
        g3 = collections.Counter()
        for ln in p3.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except Exception:
                continue
            g3[str(d.get("action") or d.get("decision") or d.get("blocked"))] += 1
        print(f"    G3 UE 动量门影子 n={sum(g3.values())} 分布={dict(g3)}")
        print("    ⚠️ n<20 ⇒ 样本不足, 不可单独裁决 (skill 纪律: 每组≥30)")
    else:
        print("    影子文件不存在")
    conn.close()


def e3_downstream_and_switches() -> None:
    """E3: 下游消费者 (Guard-First) + 开关可得性 (判 L4 vs L2)."""
    section("E3 下游消费者 (Guard-First: 判据退役≠采集退役) + 开关可得性")
    artifacts = [
        ("data/shadow/skew_gate_vision.jsonl", "G4 双口径影子 (RFC-122 阶段0)"),
        ("data/shadow/portfolio_execution_guardrail.jsonl", "G5 审计留痕 (Codex 新增)"),
        ("data/da_push_instruction_summary.jsonl", "推送口径留痕 (Codex f9d4de35a 新增)"),
        ("data/shadow/ue_long_momentum_gate.jsonl", "G3 影子"),
    ]
    print("  --- 产物文件的消费者 (摘闸前必查) ---")
    for rel, desc in artifacts:
        name = Path(rel).name
        hits = _grep(name, ["app", "scripts", "tests", "ops"], limit=10)
        files = sorted({h.split(":")[0] for h in hits})
        print(f"    {name:42s} ({desc})")
        print(f"        消费者 n={len(files)}: {[Path(f).name for f in files] or '(无=纯留痕)'}")

    print("\n  --- skip_reason 文案的消费者 (改文案会打断谁) ---")
    for key in ("方向偏斜", "组合方向偏斜", "破位硬买", "行业集中度"):
        hits = _grep(re.escape(key), ["app", "scripts", "tests"], limit=12)
        files = sorted({h.split(":")[0] for h in hits})
        print(f"    {key:12s} → {len(files)} 文件: {[Path(f).name for f in files]}")

    print("\n  --- 开关可得性 (有 yaml 开关 = L4 可关; 硬编码常量 = 需 L2 代码变更) ---")
    for gid, key, fpath in (
        ("G3", "long_momentum_gate_enabled", "config/unified_config.yaml"),
        ("G4", "max_direction_skew", "app/playbook/pipeline.py"),
        ("G5", "MAX_DIRECTION_SKEW", "app/playbook/portfolio_execution_guardrail.py"),
        ("G6", "MAX_SECTOR_PCT", "app/playbook/portfolio_execution_guardrail.py"),
    ):
        p = ROOT / fpath
        val = "-"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if key in line and not line.strip().startswith("#"):
                    val = line.strip()[:78]
                    break
        in_yaml = bool(_grep(key, ["config/unified_config.yaml"], limit=1))
        print(f"    {gid} {key:28s} yaml开关={'✅有' if in_yaml else '❌无'}  实值: {val}")
    for gid, anchor in (("G1", 'scanner_direction == "bearish" and direction == "BUY"'),
                        ("G2", "dir_conflict and tier in")):
        p = SKILL / "signal_gate.py"
        in_yaml = False
        print(f"    {gid} {anchor[:44]:46s} yaml开关=❌无 (skill 仓硬编码)")


def main() -> int:
    print(f"[DEB-20261008] 方向闸普查  ROOT={ROOT}")
    print(f"交易日锚点: {DAY} (ET) | 统计窗: >= {SINCE}")
    if not SKILL.exists():
        print(f"⚠️ DA skill 目录不存在: {SKILL} (G1/G2 锚点将报缺失)")
    e1_gate_inventory()
    e2_block_volume()
    e3_downstream_and_switches()
    print("\n[OK] deb20261008_direction_gate_census 完成 (只读, 未写任何生产文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
