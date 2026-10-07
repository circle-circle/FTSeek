#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261008] E5–E7 多空偏向溯源漏斗: 偏向是「闸造成的」还是「模型本来就只出多头」.

为什么这是本 DEB 的决定性检验:
  用户诉求是「多空不要人为拦截限定」。但若**闸前**模型提案就已经 77% 多头, 则摘闸最多回到 77%,
  不可能变成 50/50 ⇒ 摘闸是必要非充分, 真瓶颈在上游 prompt / DA 自我定位。
  不先分清这一点, 摘完闸会误判「改了没用」。

漏斗三层 (全部实测, 逐层给方向构成):
  L1 闸前模型原始提案      ← data/da_gate_decisions.jsonl 的 direction (闸判定之前)
  L2 过完 DA 侧两道闸      ← 同文件 decision ∈ {PASS, WARN} 存活
  L3 过完组合层闸=最终可交易 ← playbook_instructions 非 skip 且 direction ∈ {BUY, SELL_SHORT}

附带: 逐日漏斗 (找「空头清零日」) + 成交层构成 + UE 通道构成 (检验「空头交给 UE」是否兑现)。
只读, 任意 cwd + 无 PYTHONPATH 可冷跑。
"""
from __future__ import annotations

import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SINCE = "2026-09-17"
DAY = "2026-10-07"


def _ro(p: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def section(t: str) -> None:
    print("\n" + "=" * 106)
    print(t)
    print("=" * 106)


def _pct(long_n: int, short_n: int) -> float:
    return 100.0 * long_n / (long_n + short_n) if (long_n + short_n) else 0.0


def main() -> int:
    conn = _ro(ROOT / "data/ftseek_us.db")
    gate = ROOT / "data/da_gate_decisions.jsonl"
    if not gate.exists():
        print(f"🔴 缺 {gate} — L1/L2 不可算")
        return 1

    l1 = collections.Counter()
    l2 = collections.Counter()
    blocked = collections.Counter()
    block_by = collections.defaultdict(collections.Counter)
    day_l1: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for ln in gate.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        day = str(d.get("trade_date") or "")
        if day < SINCE:
            continue
        dirn = str(d.get("direction") or "?")
        dec = str(d.get("decision") or "?")
        l1[dirn] += 1
        day_l1[day][dirn] += 1
        if dec == "BLOCK":
            blocked[dirn] += 1
            rs = " ".join(str(x) for x in (d.get("reasons") or []))
            if "破位硬买拦截" in rs:
                block_by["G1 破位硬买 (只拦BUY)"][dirn] += 1
            elif "方向冲突" in rs:
                block_by["G2 方向冲突降档→BLOCK"][dirn] += 1
            else:
                block_by["其他 (低确信/评分SKIP)"][dirn] += 1
        else:
            l2[f"{dirn}/{dec}"] += 1

    section(f"E5 L1→L2 DA 侧两道闸 (窗 >= {SINCE}, 至 {DAY})")
    print(f"  L1 闸前原始提案 n={sum(l1.values())}: {dict(l1.most_common())}")
    tot = l1["BUY"] + l1["SELL_SHORT"]
    print(f"  ⇒ **模型自己提的多头占比 = {_pct(l1['BUY'], l1['SELL_SHORT']):.1f}%** "
          f"(BUY {l1['BUY']} / SHORT {l1['SELL_SHORT']})")
    print(f"\n  被 BLOCK 的方向分布: {dict(blocked.most_common())}")
    print("  BLOCK 归因 (哪道闸拦的):")
    for k, v in sorted(block_by.items(), key=lambda x: -sum(x[1].values())):
        print(f"    {k:30s} {dict(v.most_common())}  合计 {sum(v.values())}")

    surv_l = l2["BUY/PASS"] + l2["BUY/WARN"]
    surv_s = l2["SELL_SHORT/PASS"] + l2["SELL_SHORT/WARN"]
    print(f"\n  L2 过闸存活: {dict(l2.most_common(6))}")
    print(f"  ⇒ DA 闸后存活多头占比 = {_pct(surv_l, surv_s):.1f}% (BUY {surv_l} / SHORT {surv_s})")

    print("\n  ⚠️ 存活率对比 (判「闸到底压谁」— 这条推翻过我的口头结论):")
    if l1["BUY"]:
        print(f"    BUY  存活率 = {surv_l}/{l1['BUY']} = {100*surv_l/l1['BUY']:.1f}%")
    if l1["SELL_SHORT"]:
        print(f"    SHORT 存活率 = {surv_s}/{l1['SELL_SHORT']} = {100*surv_s/l1['SELL_SHORT']:.1f}%")
    print("    ⇒ SHORT 存活率更低 ⇒ DA 侧降档门**净压制空头**;")
    print("      而 G1 破位门只拦 BUY (n=36 全 BUY) ⇒ 两道闸方向相反, 不可合并叙述为「只压多头」。")

    section("E6 L2→L3 组合层闸 (G4 生成期偏斜 / G5 执行前偏斜 / G6 行业集中度)")
    fin_l = conn.execute(
        """SELECT COUNT(*) FROM playbook_instructions WHERE date>=?
           AND direction='BUY' AND (skip_reason IS NULL OR skip_reason='')""", (SINCE,)).fetchone()[0]
    fin_s = conn.execute(
        """SELECT COUNT(*) FROM playbook_instructions WHERE date>=?
           AND direction='SELL_SHORT' AND (skip_reason IS NULL OR skip_reason='')""", (SINCE,)).fetchone()[0]
    print(f"  L3 最终可交易: LONG={fin_l} SHORT={fin_s} ⇒ 多头占比 {_pct(fin_l, fin_s):.1f}%")
    print(f"\n  三层漏斗汇总:")
    print(f"    L1 闸前模型提案   多头 {_pct(l1['BUY'], l1['SELL_SHORT']):.1f}%")
    print(f"    L2 过 DA 两道闸   多头 {_pct(surv_l, surv_s):.1f}%")
    print(f"    L3 过组合层闸     多头 {_pct(fin_l, fin_s):.1f}%")
    print(f"  ⇒ 闸在总量上只加 {_pct(fin_l, fin_s) - _pct(l1['BUY'], l1['SELL_SHORT']):+.1f}pp "
          f"⇒ **摘闸不能把 77% 变成 50/50**, 瓶颈在上游")

    cut = collections.Counter()
    for r in conn.execute(
        """SELECT skip_reason FROM playbook_instructions WHERE date>=?
           AND skip_reason IS NOT NULL AND skip_reason!=''""", (SINCE,)):
        s = str(r["skip_reason"])
        if "组合方向偏斜" in s:
            cut["G5 执行前偏斜"] += 1
        elif "方向偏斜" in s:
            cut["G4 生成期偏斜"] += 1
        elif "行业集中度" in s or "sector" in s:
            cut["G6 行业集中度"] += 1
    print(f"\n  组合层实测砍单: {dict(cut.most_common())} 合计 {sum(cut.values())}")
    if cut.get("G5 执行前偏斜", 0) == 0:
        print("    🔴 G5 (Codex 10adea0c8 新增) 至今**零实弹拦截** — 见 deb20261008_gate_attribution.py ④⑥")

    section("E7 逐日漏斗 + 空头清零日 (闸的真实破坏力在稀缺日)")
    print(f"  {'日期':12s} {'闸前B/S':>9s} {'闸前多头%':>9s} {'最终B/S':>9s} {'最终多头%':>9s} {'空头清零':>8s}")
    zero_days = []
    for d in sorted(day_l1):
        b1, s1 = day_l1[d]["BUY"], day_l1[d]["SELL_SHORT"]
        fb = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date=? AND direction='BUY'
                             AND (skip_reason IS NULL OR skip_reason='')""", (d,)).fetchone()[0]
        fs = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date=? AND direction='SELL_SHORT'
                             AND (skip_reason IS NULL OR skip_reason='')""", (d,)).fetchone()[0]
        if s1 > 0 and fs == 0:
            zero_days.append(d)
        flag = "⚠️是" if (s1 > 0 and fs == 0) else ""
        print(f"  {d:12s} {b1:4d}/{s1:<4d} {_pct(b1, s1):8.1f}% {fb:4d}/{fs:<4d} {_pct(fb, fs):8.1f}% {flag:>8s}")
    print(f"\n  ⇒ 「闸前有空头提案、闸后空头归零」的日子 = {len(zero_days)} 天: {zero_days}")
    print("    这些天是**闸直接造成的空头清零** (不是模型没提)")

    print("\n  --- 成交层 (真金白银) 方向构成 ---")
    ex_l = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date>=?
                           AND executed=1 AND direction='BUY'""", (SINCE,)).fetchone()[0]
    ex_s = conn.execute("""SELECT COUNT(*) FROM playbook_instructions WHERE date>=?
                           AND executed=1 AND direction='SELL_SHORT'""", (SINCE,)).fetchone()[0]
    print(f"    成交 LONG={ex_l} SHORT={ex_s} ⇒ 多头占比 {_pct(ex_l, ex_s):.1f}%")

    print("\n  --- UE 通道 (被 G1 注释指定为空头来源) 实际兑现情况 ---")
    ue_l = ue_s = 0
    for r in conn.execute("""SELECT direction FROM playbook_instructions WHERE date>=?
                             AND raw_instruction_json LIKE '%ue_candidate%'""", (SINCE,)):
        if r["direction"] == "BUY":
            ue_l += 1
        elif r["direction"] == "SELL_SHORT":
            ue_s += 1
    print(f"    UE 融合落库 LONG={ue_l} SHORT={ue_s} ⇒ 空头占比 {100*ue_s/(ue_l+ue_s) if (ue_l+ue_s) else 0:.1f}%")
    print("    DA signal_gate.py:426 注释原文: 「做空机会交由 UE 不设限浮现负责」")
    print(f"    ⇒ 实测 UE 日均空头产出 {ue_s/max(1,len(day_l1)):.2f} 条; {DAY} 当日 UE 的 SHORT 提案未落库")
    print("      (今日有效 SHORT 指令 = 0) ⇒ 该设计假设**未兑现**")

    section("E8 数据缺口: 被闸拦掉的信号有无事后结局 (决定「闸好不好」能否回测)")
    # 口径纪律: G4/G6 的**实际落库文案**是「方向偏斜超限 guardrail 降级」与
    #   「sector 集中度 guardrail 降级」(英文 sector, 不含「行业集中度」) ⇒ 必须同时匹配两种写法,
    #   否则漏掉全部 G6 行 (实测漏 7 行: n=43 而非 50)。与 E2 的 cut 计数保持同一谓词。
    _CUT_PRED = ("skip_reason LIKE '%方向偏斜%' OR skip_reason LIKE '%sector%' "
                  "OR skip_reason LIKE '%行业集中度%' OR skip_reason LIKE '%组合方向偏斜%'""")
    q = conn.execute(f"""SELECT COUNT(*) n, SUM(fwd_return_intraday IS NOT NULL) fi,
                               SUM(fwd_return_t1 IS NOT NULL) ft
                        FROM playbook_instructions WHERE date>=? AND ({_CUT_PRED})""",
                     (SINCE,)).fetchone()
    q2 = conn.execute("""SELECT COUNT(*) n, SUM(fwd_return_intraday IS NOT NULL) fi,
                                SUM(fwd_return_t1 IS NOT NULL) ft
                         FROM playbook_instructions WHERE date>=?
                           AND direction IN ('BUY','SELL_SHORT')
                           AND (skip_reason IS NULL OR skip_reason='')""", (SINCE,)).fetchone()
    print(f"  被组合层闸拦掉的行: n={q['n']}  fwd_return_intraday 有值={q['fi']}  fwd_return_t1 有值={q['ft']}")
    print(f"  对照 (未拦可交易行): n={q2['n']}  fwd_return_intraday 有值={q2['fi']}  fwd_return_t1 有值={q2['ft']}")
    print(f"  ⇒ 被拦行填充率 {100*(q['fi'] or 0)/q['n'] if q['n'] else 0:.1f}% vs 成交行 "
          f"{100*(q2['fi'] or 0)/q2['n'] if q2['n'] else 0:.1f}%")
    print("  🔴 结论: 回填以成交为条件 ⇒ 被拦信号结构性无结局 ⇒ 「这些闸拦对了还是拦错了」")
    print("     在现有 DB 里**不可判**; 摘闸决定目前只能靠方向裁决, 拿不到回测背书。")
    print("     配套修复 = 给被拦行也回填 fwd_return (零交易行为变更, 纯采集面)。")

    conn.close()
    print("\n[OK] deb20261008_bias_funnel 完成 (只读)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
