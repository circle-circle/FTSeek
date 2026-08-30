#!/usr/bin/env python3
"""settlement_cap_f2.py — DEB-20260830 第12轮 CAP-1~4 + F2 结算脚本 (预注册, 阈值冻结)

背景: E11 修复 (04150669) 放行历史被错误值 0.35 拒掉的桥接候选 (54对/6日/全BUY),
叠加 UE 切 glm-5.2 供给 ~3×。方案A (2026-08-30 用户终裁): 帽不动 + 三层护栏
自然限流 + 本脚本 9/14 结算。阈值全部来自 DEB-20260830 第12轮, **冻结不得擅改**
(改阈值 = 挪门柱, 须先更新 DEB 再改本脚本)。

用法:
  venv/bin/python scripts/settlement_cap_f2.py [--start 2026-08-31] [--end 2026-09-14]
  --calibrate   用全期数据校准口径 (须复现基线 −$4,291.66/n348)

只读脚本: 不写任何文件/库。输出结算报告到 stdout。
"""
import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
SHADOW = ROOT / "data/shadow/edge_gate_shadow.jsonl"
DB = ROOT / "data/ftseek_us.db"

# ── 冻结阈值 (来源: DEB-20260830 第12轮, 2026-08-30) ──────────────
BASELINE_RELEASED_PER_DAY = 9.0        # CAP-1 基线: 54对/6覆盖日
BASELINE_BUY_NET_PER_TRADE = -12.33    # CAP-2 基线: −$4,291.66/348 (腿均, 冻结口径复算)
BASELINE_BUY_ARM_TOTAL = -4291.66      # 校准锚: DEB-20260829 D3 Hermes口径
BASELINE_BUY_ARM_N = 348
# 冻结口径 (2026-08-30 校准实锤): executed=1 AND pnl != 0
#   全量 357 行含 9 笔零值行 (5×06/24 void + 4×already_held), 剔除后
#   精确复现 −$4,291.66 / n=348。零值行 = 未结算/作废单, 语义上本就不该计入。
#   SHORT 臂基线 +$1,800.30/n73 口径为 executed=1 全量。
ARM_FILTER = "executed=1 AND pnl != 0"
ARM_FILTER_SHORT = "executed=1"
CAP3_REF_NOTIONAL = 75000.0            # CAP-3 参考: 名义帽$77,632的~97% (净值浮动取近似)
CAP3_CONSECUTIVE_LIMIT = 3             # 连续3+日触顶 → 议质量分配


def load_shadow(start, end):
    rows = []
    if not SHADOW.exists():
        return rows
    for line in open(SHADOW):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = str(d.get("ts", ""))[:10]
        if start <= ts <= end:
            rows.append(d)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-31")
    ap.add_argument("--end", default="2026-09-14")
    ap.add_argument("--calibrate", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row

    # ── 校准: 全期 BUY 臂口径复现 (必须命中 −$4,291.66/n348) ──
    if args.calibrate:
        for arm, direction, filt in (("BUY", "BUY", ARM_FILTER),
                                     ("SHORT", "SELL_SHORT", ARM_FILTER_SHORT)):
            r = db.execute(
                f"SELECT SUM(pnl) s, COUNT(*) n FROM playbook_instructions "
                f"WHERE direction='{direction}' AND {filt}"
            ).fetchone()
            print(f"校准 {arm} 臂 ({filt}): ${r['s']:.2f} / n={r['n']}")
        print(f"锚: BUY ${BASELINE_BUY_ARM_TOTAL}/n{BASELINE_BUY_ARM_N}; SHORT +$1,800.30/n73")
        return

    start, end = args.start, args.end
    print(f"═══ CAP/F2 结算窗口: {start} ~ {end} (DEB-20260830 第12轮判据) ═══\n")

    # ── CAP-1: BUY 臂放行量 (影子带 decision_id 行) ──
    rows = load_shadow(start, end)
    da_rows = [r for r in rows if str(r.get("decision_id", "")).startswith("DA")]
    buy_pass = [r for r in da_rows
                if r.get("direction") == "BUY" and r.get("live_verdict") == "PASS"]
    pairs = {(str(r.get("ts", ""))[:10], r.get("symbol")) for r in buy_pass}
    by_day = defaultdict(int)
    for d_, _ in pairs:
        by_day[d_] += 1
    n_days = len(by_day)
    avg = len(pairs) / n_days if n_days else 0.0
    has_did = sum(1 for r in rows if r.get("decision_id"))
    print(f"CAP-1 BUY放行量: {len(pairs)} (日期,标的)对 / {n_days} 影子日 = {avg:.1f} 笔/日")
    print(f"   基线: 9.0 笔/日 (释放部分) | 影子行带decision_id: {has_did}/{len(rows)} "
          f"(0 = F4未生效或窗口早于8/31)")
    if has_did == 0:
        print("   ⚠️ 无 decision_id 行 — 结算不可执行, 按 DEB 通用条款 '到期无数据' 上报")
    elif avg > BASELINE_RELEASED_PER_DAY + 6.0:
        print("   ⚠️ 显著偏高 → 按判据先排除采样偏差 (第10轮D项) 再查上游")
    else:
        print("   ✅ 量级在基线带内")

    # ── CAP-2: BUY 臂放行单净额 (冻结口径, 含零值剔除) ──
    r = db.execute(
        f"SELECT SUM(pnl) s, COUNT(*) n FROM playbook_instructions "
        f"WHERE direction='BUY' AND {ARM_FILTER} "
        "AND date BETWEEN ? AND ?", (start, end)).fetchone()
    if r["n"]:
        mean = r["s"] / r["n"]
        verdict = "✅ 不劣于基线" if mean >= BASELINE_BUY_NET_PER_TRADE else \
            "🔴 劣于基线 → 触发方案B (抬 BUY min_confidence 55→65 或收紧BUY单名帽)"
        print(f"\nCAP-2 BUY单净额: ${r['s']:.2f} / n={r['n']} = ${mean:.2f}/笔 "
              f"(基线 ${BASELINE_BUY_NET_PER_TRADE}/笔)\n   {verdict}")
    else:
        print("\nCAP-2: 窗口内无已结算 BUY 指令 — 样本不足, 顺延观察")

    # ── CAP-3: 名义帽触顶频率 ──
    print("\nCAP-3 名义帽触顶:")
    days_cap = 0
    consec = max_consec = 0
    for row in db.execute(
            "SELECT date, SUM(position_size*execution_price) notional "
            "FROM playbook_instructions WHERE executed=1 AND execution_price>0 "
            "AND date BETWEEN ? AND ? GROUP BY date ORDER BY date", (start, end)):
        hit = row["notional"] >= CAP3_REF_NOTIONAL
        days_cap += hit
        consec = consec + 1 if hit else 0
        max_consec = max(max_consec, consec)
        print(f"   {row['date']}: ${row['notional']:,.0f} {'← 触参考线' if hit else ''}")
    print(f"   触参考线 {days_cap} 日, 最长连续 {max_consec} 日 "
          f"({'🔴 ≥3连续 → 议质量分配' if max_consec >= CAP3_CONSECUTIVE_LIMIT else '✅ 未达连续3日'})")
    print("   (注: 参考线为固定近似, 真实帽=净值×0.8 浮动, 精确口径以 capital_safety 为准)")

    # ── CAP-4: UE 指令执行率 (观察项) ──
    r = db.execute(
        "SELECT COUNT(*) n, SUM(executed) ex FROM playbook_instructions "
        "WHERE decision_id LIKE 'universe_expansion%' AND date BETWEEN ? AND ?",
        (start, end)).fetchone()
    rate = (r["ex"] or 0) / r["n"] if r["n"] else 0.0
    print(f"\nCAP-4 UE: 指令 {r['n']} / 执行 {r['ex'] or 0} = {rate:.0%} (观察项)")
    if r["n"] and r["n"] > 60 and rate < 0.05:
        print("   ⚠️ 指令洪泛但执行率~0 → 瓶颈在触发不在供给")

    # ── SHORT 腿对照 (F2 附带, 冻结口径) ──
    r = db.execute(
        f"SELECT SUM(pnl) s, COUNT(*) n FROM playbook_instructions "
        f"WHERE direction='SELL_SHORT' AND {ARM_FILTER_SHORT} "
        "AND date BETWEEN ? AND ?", (start, end)).fetchone()
    if r["n"]:
        print(f"\n附带 SHORT腿: ${r['s']:.2f} / n={r['n']} = ${r['s']/r['n']:.2f}/笔")

    print("\n═══ 结算完毕 — 判据冻结自 DEB-20260830 第12轮, 改判据须先改 DEB ═══")


if __name__ == "__main__":
    main()
