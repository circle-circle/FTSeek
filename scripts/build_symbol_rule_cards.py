# -*- coding: utf-8 -*-
"""股性卡构建器 — 合并三个规则族(R5/R9/R10/M系列), 只写四关门全过的规则.

输出: data/symbol_rule_cards/{symbol}.json
口径声明: 日K/分钟K收盘对收盘, 样本期 2021-2026(日K)/2026-01~08(分钟K),
         未做 regime 分桶 — 卡是 L2 软提示素材, 非硬规则.
"""
import json
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
OUT_DIR = ROOT / "data" / "symbol_rule_cards"
OUT_DIR.mkdir(exist_ok=True)

cards = {}  # sym -> [rule_entry]

def add(sym, rule, detail, family):
    cards.setdefault(sym, []).append({
        "rule": rule, "family": family,
        "n": detail["n"], "next_day_mean_pct": detail["mean"],
        "t": detail["t"], "is_mean": detail["is_mean"], "oos_mean": detail["oos_mean"],
    })

# 1) R1-R8 族 (仅 R5 过四关, verdict 从回放结果读)
r18 = json.load(open("/tmp/symbol_rule_replay.json"))
R5_SYMS = {"US.SNDK", "US.MU", "US.BWXT", "US.NFLX"}  # 8/19 四关门 PASS 名单
for sym, rules in r18.items():
    r5 = rules.get("R5_跳空高开>2%")
    if r5 and r5.get("PASS") and sym in R5_SYMS:
        add(sym, "R5_跳空高开>2%延续", r5, "日K缺口性格")

# 2) R9/R9b/R10 族
r910 = json.load(open("/tmp/daily_r9r10_replay.json"))
for sym, rules in r910.get("results", {}).items():
    for rule, d in rules.items():
        if d.get("PASS") and r910.get("verdict", {}).get(rule):
            fam = "日K收盘位置" if rule.startswith("R9") else "日K窄幅日"
            add(sym, rule, d, fam)

# 3) 分钟级族
mm = json.load(open("/tmp/minute_rules_replay.json"))
for rule, syms in mm.get("results", {}).items():
    if not mm.get("verdict", {}).get(rule):
        continue
    for sym, d in syms.items():
        if d.get("PASS"):
            add(sym, rule, d, "分钟级开盘性格")

n_cards = 0
for sym in sorted(cards):
    rules = sorted(cards[sym], key=lambda x: -abs(x["t"]))
    card = {
        "symbol": sym,
        "schema": "symbol_rule_card/L0@0.1",
        "generated": "2026-08-19",
        "gate": "n>=30 + |t|>=2.0 + IS/OOS同号 + 规则级安慰剂>期望+2σ",
        "usage": "L2软提示素材 — DA可凭独立强逻辑覆盖, 须thesis留痕",
        "rules": rules,
    }
    fp = OUT_DIR / f"{sym.replace('.', '_')}.json"
    fp.write_text(json.dumps(card, ensure_ascii=False, indent=1))
    n_cards += 1
    print(f"{sym}: {len(rules)}条 — " + " | ".join(f"{r['rule']}({r['next_day_mean_pct']:+.2f}%)" for r in rules))

print(f"\n股性卡落盘: {n_cards} 只 → {OUT_DIR}")
