#!/usr/bin/env python3
"""[治理审计] DA signal_gate 61% BLOCK 到底是拦垃圾还是误杀好票 (只读).

回答用户「提升合理有质量标的供应」: 门台账(§36)定位真供应约束在 DA signal_gate(BLOCK 61%),
本审计核该 61% 拦的是什么。三层发现 (每层都更正了上一层的原始数):

  ① "61% BLOCK" 误导: 80.5% 的 BLOCK 是 direction=SKIP(分析自身未给交易信号),
     非质量门拦截。真方向拦截仅 239 笔(11.9% of 决策 / 23.2% of 有方向候选)。
  ② 量纲更正: da_gate_decisions.quality_score(0-100) ≠ 门驱动变量!
     真驱动 = tier_score(0-10, "分析结论排序=X(score=Y/10)") + decision_conf(辩论一致度)。
     quality_score/tier_score 比值中位 7.5, 仅 11% 等于×10 ⇒ 两个不同变量。
  ③ 预测效度(已成交已平仓 n≈440, 真驱动):
     - Spearman(tier_score, pnl_net) ≈ 0.000 (零预测力; 分桶非单调, 7-8桶最差WR44%)
     - Spearman(decision_conf, pnl_net) ≈ +0.159 (弱但真; conf≥0.7→+6.51/WR72% vs 0.5-0.6→−4.44/WR54%)
     ⇒ 门拦在 tier_score 上【无预测效度依据】; 拦在 decision_conf 上【有依据】。

纪律: 只读(DB mode=ro); 预测效度只在已成交样本算(被拦候选无可观测结局=根本局限,
  故 tier_score 零预测力只在已成交[6,8]区间成立, 不能证明被拦score<5的是好票, 只证 score 非可靠质量轴)。
  ⇒ 不据本审计改门(须预注册影子A/B: conf-only vs score+conf); 仅登记供裁决。
运行: ./venv/bin/python scripts/da_gate_block_audit.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict, Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "data" / "da_gate_decisions.jsonl"
DB = REPO / "data" / "ftseek_us.db"


def _rows(p):
    return [json.loads(l) for l in open(p, errors="replace") if l.strip()]


def _spearman(x, y):
    def rank(v):
        s = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(s):
            j = i
            while j + 1 < len(s) and v[s[j + 1]] == v[s[i]]:
                j += 1
            a = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[s[k]] = a
            i = j + 1
        return r
    rx, ry = rank(x), rank(y); n = len(rx)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = sum((rx[i] - mx) ** 2 for i in range(n)) ** .5
    sy = sum((ry[i] - my) ** 2 for i in range(n)) ** .5
    return cov / (sx * sy) if sx * sy > 0 else 0.0


def main():
    rows = _rows(GATE)
    blocks = [r for r in rows if r.get("decision") == "BLOCK"]
    print("═══ DA signal_gate BLOCK 审计 (只读) ═══")
    print(f"总决策行 {len(rows)}; BLOCK {len(blocks)} ({len(blocks)/len(rows)*100:.0f}%)\n")

    # ① direction 分布
    print("【① '61% BLOCK' 拆解: BLOCK 的 direction 字段】")
    dc = Counter(r.get("direction") for r in blocks)
    for d, n in dc.most_common():
        print(f"  {str(d):12} {n:5} ({n/len(blocks)*100:4.1f}%)")
    skip_n = dc.get("SKIP", 0)
    real = [r for r in blocks if r.get("direction") in ("BUY", "SELL_SHORT")]
    print(f"  ⇒ direction=SKIP(分析未给交易信号, 非门拦截) = {skip_n} ({skip_n/len(blocks)*100:.1f}%)")
    print(f"  ⇒ 真方向被门拦 = {len(real)} ({len(real)/len(rows)*100:.1f}% of 全体决策)")
    # 真方向拦截驱动
    g1 = sum(1 for r in real if "破位硬买拦截" in " ".join(str(x) for x in (r.get("reasons") or [])))
    cha = sum(1 for r in real if re.search(r"分析结论排序=差", " ".join(str(x) for x in (r.get("reasons") or []))))
    print(f"     驱动: G1破位硬买 {g1} + tier=差(score<5或conf<0.5) {cha} + 其他 {len(real)-g1-cha}")

    # ② 量纲
    print("\n【② 量纲核: quality_score字段(0-100) vs tier_score(0-10, 真驱动)】")
    pr = []
    for r in rows:
        txt = " ".join(str(x) for x in (r.get("reasons") or []))
        m = re.search(r"score=([\d.]+)/10", txt)
        if m and r.get("quality_score") is not None:
            pr.append((float(m.group(1)), r["quality_score"]))
    ratios = [qs / ts for ts, qs in pr if ts > 0]
    exact = sum(1 for ts, qs in pr if abs(qs - ts * 10) < 0.5)
    print(f"  quality_score/tier_score 比值中位={statistics.median(ratios):.2f}; ==×10 仅 {exact}/{len(pr)} ({exact/len(pr)*100:.0f}%)")
    print(f"  ⇒ 两个不同变量; 门决策由 tier_score+conf 驱动, quality_score 字段不驱动")

    # ③ 预测效度 (真驱动)
    print("\n【③ 预测效度: 真驱动 tier_score / decision_conf vs 已成交 pnl_net】")
    gate = defaultdict(list)
    for r in rows:
        if r.get("decision") in ("PASS", "WARN") and r.get("direction") in ("BUY", "SELL_SHORT"):
            txt = " ".join(str(x) for x in (r.get("reasons") or []))
            m = re.search(r"分析结论排序=\S+?\(score=([\d.]+)/10 decision_conf=([\d.]+)", txt)
            if m:
                gate[(r.get("symbol"), r.get("trade_date"))].append((float(m.group(1)), float(m.group(2))))
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    pairs = []
    for r in con.execute("""SELECT symbol,entry_time,pnl_net FROM trade_records
                            WHERE exit_reason IS NOT NULL AND exit_reason!='' AND exit_time IS NOT NULL
                            AND pnl_net IS NOT NULL AND symbol!=''"""):
        c = gate.get((r["symbol"], str(r["entry_time"])[:10])) or gate.get((r["symbol"].replace("US.", ""), str(r["entry_time"])[:10]))
        if c:
            pairs.append((max(x[0] for x in c), max(x[1] for x in c), float(r["pnl_net"])))
    con.close()
    ts = [p[0] for p in pairs]; cf = [p[1] for p in pairs]; pnl = [p[2] for p in pairs]
    print(f"  匹配已成交已平仓 n={len(pairs)}")
    print(f"  Spearman(tier_score, pnl)   = {_spearman(ts,pnl):+.3f}  ⇒ {'零/无预测力' if abs(_spearman(ts,pnl))<0.1 else '有'}")
    print(f"  Spearman(decision_conf, pnl) = {_spearman(cf,pnl):+.3f}  ⇒ {'弱但真' if 0.1<=_spearman(cf,pnl)<0.3 else '无' if _spearman(cf,pnl)<0.1 else '有'}")
    print(f"  tier_score 分桶(非单调=零Spearman根因):")
    for lo, hi, nm in [(6, 7, "良6-7"), (7, 8, "良/优7-8"), (8, 11, "优8+")]:
        bp = [p[2] for p in pairs if lo <= p[0] < hi]
        if bp:
            print(f"    {nm:10} n={len(bp):3} 净均={statistics.mean(bp):+7.2f} 胜率={sum(1 for x in bp if x>0)/len(bp)*100:.0f}%")
    print(f"  decision_conf 分桶(单调=有预测力):")
    for lo, hi, nm in [(0.5, 0.6, "conf.5-.6"), (0.7, 1.01, "conf.7+")]:
        bp = [p[2] for p in pairs if lo <= p[1] < hi]
        if bp:
            print(f"    {nm:10} n={len(bp):3} 净均={statistics.mean(bp):+7.2f} 胜率={sum(1 for x in bp if x>0)/len(bp)*100:.0f}%")

    print("\n【结论】")
    print("- 门非主供应杀手: 80.5% BLOCK 是分析自身 SKIP(无交易信号), 真方向拦截仅 239(11.9%)")
    print("- 真方向拦截: G1破位硬买(防接飞刀,用户裁保留) + tier=差(score<5或conf<0.5)")
    print("- tier_score 零预测力(已成交[6,8]区间) ⇒ 拦在 score 上无效度依据; conf 有预测力 ⇒ 拦在 conf 上合理")
    print("- ⚠️ 局限: 被拦候选无可观测结局, 不能证明 score<5 的是好票; 只证 score 非可靠质量轴")
    print("- 建议: 不据本审计改门; 预注册影子A/B(conf-only vs score+conf tier)攒样再裁")


if __name__ == "__main__":
    main()
