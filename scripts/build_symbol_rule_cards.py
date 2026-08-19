# -*- coding: utf-8 -*-
"""股性卡构建器 v2 (DEB §14.2 修订版).

v2 变更 (Qoder §14.2-1/2/3/4 裁决):
  1. 数据源从 /tmp 改为 data/*_replay_latest.json (cron 化地基, 废除易失依赖)
  2. R5 名单从 rule_verdict 派生 (废除硬编码 R5_SYMS), 全池写卡不再限 watchlist
  3. 第五关效应量地板: |next_day_mean| >= 0.3% 才入卡 (t够大但经济意义不足不占DA注意力)
  4. schema 增 sample_period / sample_regime / pool_baseline / evidence_tier 字段
     (regime 失配停注 + 防共性读成个性 的支撑)
  5. 分钟族带 evidence_tier=marginal 标签 (7.5月单regime边际证据)

口径声明: 卡是 L2 软提示素材; 效应量地板阈值 0.3% 为 Qoder §14.2-3 建议值,
         首版卡缩水属预期 (37→约25条)。
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
DATA = ROOT / "data"
OUT_DIR = DATA / "symbol_rule_cards"
OUT_DIR.mkdir(exist_ok=True)

EFFECT_FLOOR = 0.3  # 第五关: 次日均值绝对值地板 (%)
TOP_N_PER_CARD = 3  # Q3: 单卡条数上限 (注入侧再用, 此处也截断防膨胀)

# ── 当前 regime (复用 B1a 同源: playbook_regime_log) ──
cur_regime = "UNKNOWN"
try:
    con = sqlite3.connect(DATA / "ftseek_us.db")
    row = con.execute("SELECT regime FROM playbook_regime_log ORDER BY date DESC LIMIT 1").fetchone()
    if row:
        cur_regime = (row[0] or "UNKNOWN").upper()
    con.close()
except Exception:
    pass

# ── 三族回放结果 ──
def load_latest(name, fallback_tmp):
    p = DATA / name
    if p.exists():
        return json.loads(p.read_text())
    # 首轮兼容: 落盘文件不存在时用 /tmp 旧产物 (仅本次过渡)
    fp = Path(fallback_tmp)
    return json.loads(fp.read_text()) if fp.exists() else None

r18 = load_latest("r18_replay_latest.json", "/tmp/symbol_rule_replay.json")
r910 = load_latest("r9r10_replay_latest.json", "/tmp/daily_r9r10_replay.json")
mm = load_latest("minute_rules_replay_latest.json", "/tmp/minute_rules_replay.json")
if not (r18 and r910 and mm):
    raise SystemExit("回放结果缺失 — 请先跑三个回放脚本")

# ── pool 基线: 每规则全池 PASS 票均值的中位数 (防共性读成个性) ──
def pool_median(sym_rule_map):
    vals = [d["mean"] for d in sym_rule_map.values() if d.get("PASS")]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 3)

def fam_meta(rule):
    if rule.startswith("M"):
        return {"family": "分钟级开盘性格", "sample_period": "2026-01-02~2026-08-18",
                "sample_regime": "RISK_ON(单段)", "evidence_tier": "marginal"}
    if rule.startswith("R9") or rule.startswith("R10"):
        return {"family": "日K收盘位置/窄幅", "sample_period": "2021-02~2026-08-18(5年)",
                "sample_regime": "混合(牛市段为主)", "evidence_tier": "standard"}
    return {"family": "日K缺口性格", "sample_period": "2021-02~2026-08-18(5年)",
            "sample_regime": "混合(牛市段为主)", "evidence_tier": "standard"}

cards = {}  # sym -> [rule_entry]
dropped_floor = 0

def add(sym, rule, detail, pool_base):
    global dropped_floor
    if abs(detail["mean"]) < EFFECT_FLOOR:
        dropped_floor += 1
        return  # 第五关: 效应量不足
    meta = fam_meta(rule)
    cards.setdefault(sym, []).append({
        "rule": rule, "family": meta["family"],
        "n": detail["n"], "next_day_mean_pct": detail["mean"],
        "t": detail["t"], "is_mean": detail["is_mean"], "oos_mean": detail["oos_mean"],
        "pool_baseline_pct": pool_base,
        "sample_period": meta["sample_period"],
        "sample_regime": meta["sample_regime"],
        "evidence_tier": meta["evidence_tier"],
    })

# 1) R1-R8 族 — R5 名单从 rule_verdict 派生 (废除硬编码)
if r18.get("rule_verdict", {}).get("R5_跳空高开>2%"):
    r5_pool = {sym: d for sym, rules in r18["results"].items()
               for r, d in rules.items() if r == "R5_跳空高开>2%"}
    pb = pool_median(r5_pool)
    for sym, rules in r18["results"].items():
        r5 = rules.get("R5_跳空高开>2%")
        if r5 and r5.get("PASS"):
            add(sym, "R5_跳空高开>2%延续", r5, pb)

# 2) R9/R9b/R10 族
for rule, rule_verdict in r910.get("verdict", {}).items():
    if not rule_verdict:
        continue
    pool = {sym: d for sym, rules in r910["results"].items() for r, d in rules.items() if r == rule}
    pb = pool_median(pool)
    for sym, d in pool.items():
        if d.get("PASS"):
            add(sym, rule, d, pb)

# 3) 分钟级族
for rule, rule_verdict in mm.get("verdict", {}).items():
    if not rule_verdict:
        continue
    pool = mm["results"].get(rule, {})
    pb = pool_median(pool)
    for sym, d in pool.items():
        if d.get("PASS"):
            add(sym, rule, d, pb)

n_cards = n_rules = 0
for sym in sorted(cards):
    rules = sorted(cards[sym], key=lambda x: -abs(x["t"]))[:TOP_N_PER_CARD]
    card = {
        "symbol": sym,
        "schema": "symbol_rule_card/L0@0.2",
        "generated": date.today().isoformat(),
        "current_regime": cur_regime,
        "gate": "n>=30 + |t|>=2.0 + IS/OOS同号 + 安慰剂>期望+2σ + |效应量|>=0.3%",
        "usage": "L2软提示素材 — 与K线技术面同源勿双重计权; DA可凭独立强逻辑覆盖须thesis留痕",
        "rules": rules,
    }
    fp = OUT_DIR / f"{sym.replace('.', '_')}.json"
    fp.write_text(json.dumps(card, ensure_ascii=False, indent=1))
    n_cards += 1
    n_rules += len(rules)

# 清理 v1 遗留卡 (全池重建, 防旧卡残留)
keep = {f"{s.replace('.', '_')}.json" for s in cards}
for fp in OUT_DIR.glob("*.json"):
    if fp.name not in keep:
        fp.unlink()

print(f"股性卡重建: {n_cards} 只 × {n_rules} 条 (效应量地板剔除 {dropped_floor} 条)")
for sym in sorted(cards):
    rs = sorted(cards[sym], key=lambda x: -abs(x["t"]))[:TOP_N_PER_CARD]
    print(f"{sym}: " + " | ".join(f"{r['rule']}({r['next_day_mean_pct']:+.2f}%)" for r in rs))
