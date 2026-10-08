#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E4 归因核验: 今日 5→2 到底是哪个门砍的 + G5 是否已实弹生效 + 时区格式核验.

自查动机 (自我推翻): 上一轮口头结论称「执行前组合偏斜门 (Codex G5) 今天把 5 条砍到 2 条」,
  实测 skip_reason 文案是**旧门**措辞「方向偏斜超限 guardrail 降级」(G4) /
  「sector 集中度 guardrail 降级」(G6), 而 G5 专属文案「组合方向偏斜 guardrail 拒绝执行」全库 n=0
  且审计文件 data/shadow/portfolio_execution_guardrail.jsonl **不存在** ⇒ 该归因错误, 本脚本钉死。

时区纪律 (MEMORY): SQLite 三格式混存 ⇒ 必须实测判定 created_at/updated_at 各自时区, 不靠假设。
"""
from __future__ import annotations

import collections
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-07"
CST = timezone(timedelta(hours=8))
# 2026-10-08 棘轮清剿: 硬编码 UTC-4 假钟 → 真时区 (冬令时 EST=UTC-5 不错位)
ET = ZoneInfo("America/New_York")


def _ro(p: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def section(t: str) -> None:
    print("\n" + "=" * 106)
    print(t)
    print("=" * 106)


def main() -> int:
    conn = _ro(ROOT / "data/ftseek_us.db")

    section("① 今日 5 条直写行的时间戳原文 (判时区格式)")
    now_utc = datetime.now(timezone.utc)
    print(f"  脚本运行时刻: UTC {now_utc:%Y-%m-%d %H:%M:%S} | CST {now_utc.astimezone(CST):%H:%M:%S} "
          f"| ET {now_utc.astimezone(ET):%H:%M:%S}")
    for r in conn.execute(
        """SELECT id, symbol, direction, confidence, skip_reason, created_at, updated_at, executed,
                  execution_state
           FROM playbook_instructions WHERE date=? AND id BETWEEN 3839 AND 3843 ORDER BY id""", (DAY,)
    ):
        print(f"  id={r['id']} {str(r['symbol']):10s} dir={str(r['direction']):5s} conf={r['confidence']} "
              f"exec={r['executed']} state={r['execution_state']}")
        print(f"      created_at={r['created_at']!r}")
        print(f"      updated_at={r['updated_at']!r}")
        print(f"      skip_reason={str(r['skip_reason'] or '(无)')[:76]!r}")

    section("② 时区判定: created_at/updated_at 是否 ISO8601 带偏移, 还是裸串")
    samples = [r["created_at"] for r in conn.execute(
        "SELECT created_at FROM playbook_instructions WHERE created_at IS NOT NULL ORDER BY id DESC LIMIT 5")]
    samples += [r["updated_at"] for r in conn.execute(
        "SELECT updated_at FROM playbook_instructions WHERE updated_at IS NOT NULL ORDER BY id DESC LIMIT 5")]
    fmt = collections.Counter()
    for s in samples:
        s = str(s)
        if s.endswith("Z"):
            fmt["ISO+Z (UTC)"] += 1
        elif "+" in s[10:] or (len(s) > 19 and s[19] in "+-"):
            fmt["ISO+offset"] += 1
        elif "T" in s:
            fmt["ISO裸串(无时区)"] += 1
        elif " " in s:
            fmt["空格分隔裸串(无时区)"] += 1
        else:
            fmt["其他"] += 1
    print(f"  近10个时间戳格式分布: {dict(fmt)}")
    print(f"  样例: {samples[:4]}")
    print("  ⇒ 裸串无时区 ⇒ 必须与**已知事件时刻**对齐才能判读, 不得假设")

    section("③ 用已知事件对齐时区: skew 影子 ts (ISO+UTC) vs 指令 updated_at")
    sk = ROOT / "data/shadow/skew_gate_vision.jsonl"
    g4_ts = None
    if sk.exists():
        n_bad = 0
        for ln in sk.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except Exception:
                n_bad += 1  # 2026-10-08 棘轮清剿: 坏行计数留痕 (原为静默 continue)
                continue
            if d.get("date") == DAY:
                g4_ts = d.get("ts")
        if n_bad:
            print(f"  [warn] skew_gate_vision 坏行跳过 {n_bad} 条", file=sys.stderr)
    print(f"  skew_gate_vision 今日 ts = {g4_ts}")
    if g4_ts:
        t = datetime.fromisoformat(str(g4_ts).replace("Z", "+00:00"))
        print(f"    = UTC {t:%H:%M:%S} | CST {t.astimezone(CST):%H:%M:%S} | ET {t.astimezone(ET):%H:%M:%S}")
        print("    ⇒ 该 ts 是 **G4 生成期门**在 builder 内的埋点 (RFC-122 阶段0 record-only)")
    upd = [str(r["updated_at"]) for r in conn.execute(
        "SELECT updated_at FROM playbook_instructions WHERE date=? AND skip_reason LIKE '%guardrail%'", (DAY,))]
    print(f"  今日被砍 3 条的 updated_at = {upd}")
    print("  ⇒ 若 updated_at 为 UTC: 20:00:19Z = CST 04:00(次日) 不合理")
    print("     若为 CST: 20:00:19 CST = ET 08:00:19 ⇒ 与 DA 直写批 (ET 08:27 created) 时序需再核")

    section("④ G5 (执行前组合偏斜门) 实弹生效核验 — 决定性")
    n5 = conn.execute(
        "SELECT COUNT(*) FROM playbook_instructions WHERE skip_reason LIKE '%组合方向偏斜 guardrail 拒绝执行%'"
    ).fetchone()[0]
    n5s = conn.execute(
        "SELECT COUNT(*) FROM playbook_instructions WHERE skip_reason LIKE '%组合行业集中度 guardrail 拒绝执行%'"
    ).fetchone()[0]
    audit = ROOT / "data/shadow/portfolio_execution_guardrail.jsonl"
    print(f"  G5 专属文案「组合方向偏斜 guardrail 拒绝执行」全库 n = {n5}")
    print(f"  G6' 专属文案「组合行业集中度 guardrail 拒绝执行」全库 n = {n5s}")
    print(f"  G5 审计留痕 {audit.name}: {'存在 '+str(audit.stat().st_size)+'B' if audit.exists() else '不存在'}")
    print(f"  ⇒ {'🔴 G5 至今零实弹拦截' if (n5 == 0 and n5s == 0 and not audit.exists()) else 'G5 已有实弹记录'}")
    print("  归因更正: 今日 5→2 由 **G4 生成期偏斜门 + G6 行业集中度门** 造成 (文案为旧门措辞),")
    print("           **不是** Codex 新增的 G5。G5 于 CST 23:10 随服务重启加载, 当时已收盘 (ET 11:10),")
    print("           无执行机会 ⇒ 首次实弹检验 = 下一交易日开盘。")

    section("⑤ G5 加载时刻 vs 收盘时刻 (判「为何零拦截」是否合理)")
    import subprocess
    r = subprocess.run(["systemctl", "show", "ftseek-us", "-p", "ActiveEnterTimestamp"],
                       capture_output=True, text=True, timeout=60)
    print(f"  服务启动: {r.stdout.strip()}")
    r2 = subprocess.run(["git", "log", "-1", "--format=%ad %h", "--date=format:%Y-%m-%d %H:%M:%S %z",
                         "10adea0c8"], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    print(f"  G5 提交:   {r2.stdout.strip()}")
    print(f"  美股收盘 (EDT 10月) = ET 16:00 = CST 04:00(次日); G5 加载于 CST 23:10 = ET 11:10 (盘中)")
    print("  ⚠️ 但 SIM 环境当日是否还有执行轮次需查 auto_execute 日志 — 见 ⑥")

    section("⑥ G5 加载后是否有执行轮次经过它 (若有轮次而零拦截 = 门未接线; 若无轮次 = 时机未到)")
    log = ROOT / "logs/ftseek_us.log"
    hits = []
    if log.exists():
        for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if ("2026-10-07 23:" in ln or "2026-10-08 0" in ln) and (
                "execute_instruction" in ln or "auto_execute" in ln
                or "PORTFOLIO_GUARDRAIL" in ln or "组合guardrail" in ln):
                hits.append(ln)
    print(f"  G5 加载后 (CST 23:10 起) 执行路径日志命中 n={len(hits)}")
    for h in hits[-8:]:
        print(f"    {h[:170]}")
    if not hits:
        print("    ⇒ 无执行轮次经过 ⇒ G5「零拦截」= **时机未到 (TOO_EARLY)**, 不等于接线死")
        print("    ⇒ 按 Gate 6 时段依赖埋点三态纪律: 不可判 LIVE/DEAD, 须等下一交易日")

    conn.close()
    print("\n[OK] deb20261008_gate_attribution 完成 (只读)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
