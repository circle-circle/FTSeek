#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标的短名单生成器 (2026-08-03) — 实盘切换前的重点标的筛选.

三层证据管道的逐标的合并 (全部来自现有记录, 只读):
  L1 信号层   signal_snapshots.db  : 方向命中率 (LONG/SHORT 分开, 全期+近30天), IC_dir, alpha_T1
  L2 台账层   playbook_instructions: 已执行指令的 fwd_return_t1 方向命中率 (前向台账, 非回测)
  L3 执行层   trade_records (6/27+) : 模拟盘 playbook 时代的成交胜率/盈亏

分层规则 (每层达标 +1):
  L1 信号层  近30天命中率 ≥45% (playbook 时代干净样本为主闸门; 全期≥50% 仅作稳定性标注)
  L2 台账层  已执行指令 T+1 方向命中率 ≥50% (n≥5)
  L3 执行层  模拟盘胜率 ≥55% 或 累计PnL>0 (n≥3)
  A级 = 3/3, B级 = 2/3, C级 = ≤1/3 (C 含"反指"与"近期衰减"告警)

方向倾斜: 每标的 L/S 命中率差 ≥15pp 记为该方向优势; 任一方向命中率 ≤25% 记"反指告警"。
近期衰减: 全期命中率 ≥50% 但近30天 <40% → 旧边缘已失效, 告警。

用法: ./venv/bin/python scripts/symbol_shortlist.py
输出: obsidian_vault/strategies/2026-08-03_symbol_shortlist.md + data/symbol_shortlist.json
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "ftseek_us.db"
SNAP = ROOT / "data" / "signal_snapshots.db"
VAULT = ROOT / "obsidian_vault" / "strategies"
OUT_JSON = ROOT / "data" / "symbol_shortlist.json"

SIGNAL_MIN_N = 30      # 信号层样本门槛 (用户偏好 n≥30)
LEDGER_MIN_N = 5       # 台账层门槛
EXEC_MIN_N = 3         # 执行层门槛
L1_RECENT_HIT = 0.45   # 近30天命中率
L1_ALL_HIT = 0.50      # 全期命中率
L2_HIT = 0.50
EXEC_WR = 0.55
RECENT_DATE = "2026-07-01"  # playbook 时代起点


def spearmanr(x, y):
    """纯 Python Spearman 秩相关 (scipy 版本冲突时仍可用)."""
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(x), rank(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


def hit_rate(rows):
    if not rows:
        return None, 0
    hits = sum(1 for s in rows if (s["direction"] == "LONG" and s["fwd_ret_t1"] > 0) or
               (s["direction"] == "SHORT" and s["fwd_ret_t1"] < 0))
    return hits / len(rows), len(rows)


def main() -> int:
    # ── L1 信号层 ──
    con = sqlite3.connect(str(SNAP))
    con.row_factory = sqlite3.Row
    sigs = [dict(r) for r in con.execute("""
        SELECT symbol, direction, strength, fwd_ret_t1, alpha_t1, trade_date
        FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL""")]
    con.close()

    by_sym = {}
    for s in sigs:
        by_sym.setdefault(s["symbol"], []).append(s)

    l1 = {}
    for sym, ss in by_sym.items():
        if len(ss) < SIGNAL_MIN_N:
            continue
        longs = [s for s in ss if s["direction"] == "LONG"]
        shorts = [s for s in ss if s["direction"] == "SHORT"]
        recent = [s for s in ss if s["trade_date"] >= RECENT_DATE]
        hr_all, _ = hit_rate(ss)
        hr_l, _ = hit_rate(longs)
        hr_s, _ = hit_rate(shorts)
        hr_recent, n_recent = hit_rate(recent)
        if hr_l is not None and hr_s is not None:
            ic_dir = spearmanr([s["strength"] or 0 for s in ss],
                               [(s["fwd_ret_t1"] if s["direction"] == "LONG" else -s["fwd_ret_t1"]) for s in ss])
        else:
            ic_dir = None
        alpha1 = sum(s["alpha_t1"] or 0 for s in ss) / len(ss)
        l1[sym] = {"n": len(ss), "hit_all": hr_all, "hit_long": hr_l, "hit_short": hr_s,
                   "hit_recent": hr_recent, "n_recent": n_recent, "ic_dir": round(ic_dir, 3) if ic_dir is not None else None,
                   "alpha_t1": round(alpha1, 4)}

    # ── L2 台账层 (playbook_instructions 已执行, fwd_return_t1 前向) ──
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    ledger = [dict(r) for r in con.execute("""
        SELECT symbol, direction, fwd_return_t1 FROM playbook_instructions
        WHERE executed=1 AND fwd_return_t1 IS NOT NULL AND direction IN ('BUY','SELL_SHORT')""")]
    l2 = {}
    for sym in set(r["symbol"] for r in ledger):
        rows = [r for r in ledger if r["symbol"] == sym]
        if len(rows) < LEDGER_MIN_N:
            continue
        hits = sum(1 for r in rows if r["fwd_return_t1"] > 0)  # 已方向调整
        l2[sym] = {"n": len(rows), "hit_t1": hits / len(rows)}

    # ── L3 执行层 (trade_records, playbook 时代) ──
    execs = [dict(r) for r in con.execute("""
        SELECT symbol, direction, realized_pnl, pnl_pct FROM trade_records
        WHERE trade_date >= ?""", (RECENT_DATE,))]
    con.close()
    l3 = {}
    for sym in set(r["symbol"] for r in execs):
        rows = [r for r in execs if r["symbol"] == sym]
        if len(rows) < EXEC_MIN_N:
            continue
        wins = sum(1 for r in rows if (r["realized_pnl"] or 0) > 0)
        pnl = sum(r["realized_pnl"] or 0 for r in rows)
        l3[sym] = {"n": len(rows), "wr": wins / len(rows), "pnl": round(pnl, 2)}

    # ── 合并评分 ──
    rows = []
    for sym in set(list(l1) + list(l2) + list(l3)):
        a, b, c = l1.get(sym), l2.get(sym), l3.get(sym)
        score = 0
        reasons = []
        if a:
            if a["hit_recent"] is not None and a["hit_recent"] >= L1_RECENT_HIT:
                score += 1
                reasons.append(f"L1信号近30d{a['hit_recent']*100:.0f}%")
        if b:
            if b["hit_t1"] >= L2_HIT:
                score += 1
                reasons.append(f"L2台账{b['hit_t1']*100:.0f}%")
        if c:
            if c["wr"] >= EXEC_WR or c["pnl"] > 0:
                score += 1
                reasons.append(f"L3执行{c['wr']*100:.0f}%WR/{c['pnl']:+.0f}")
        # 方向倾斜
        bias = ""
        if a and a["hit_long"] is not None and a["hit_short"] is not None:
            if a["hit_long"] - a["hit_short"] >= 0.15:
                bias = "偏多"
            elif a["hit_short"] - a["hit_long"] >= 0.15:
                bias = "偏空"
            else:
                bias = "双向"
        # 反指告警
        invert = []
        if a:
            if a["hit_long"] is not None and a["hit_long"] <= 0.25:
                invert.append(f"LONG{a['hit_long']*100:.0f}%反指")
            if a["hit_short"] is not None and a["hit_short"] <= 0.25:
                invert.append(f"SHORT{a['hit_short']*100:.0f}%反指")
            if a["hit_all"] is not None and a["hit_recent"] is not None and a["hit_all"] >= 0.50 and a["hit_recent"] < 0.40:
                invert.append(f"近期衰减({a['hit_all']*100:.0f}%→{a['hit_recent']*100:.0f}%)")
        tier = "A" if score >= 3 else ("B" if score == 2 else "C")
        rows.append({"symbol": sym, "tier": tier, "score": score, "reasons": reasons,
                     "bias": bias, "invert": " / ".join(invert),
                     "l1": a, "l2": b, "l3": c})

    rows.sort(key=lambda r: (-r["score"], r["symbol"]))
    now = datetime.now().strftime("%Y-%m-%d")
    fname = f"{now}_symbol_shortlist.md"
    L = [f"# 标的短名单 ({now}) — 实盘切换前重点标的\n",
         f"> 生成: scripts/symbol_shortlist.py | 三层证据: 信号层(signal_snapshots n≥{SIGNAL_MIN_N}) + 台账层(playbook_instructions 已执行 n≥{LEDGER_MIN_N}) + 执行层(trade_records {RECENT_DATE}+ n≥{EXEC_MIN_N})\n",
         f"> 分层: A=3/3 三重验证 | B=2/3 | C=≤1/3 或反指\n",
         f"> 阈值: L1近30d≥{L1_RECENT_HIT:.0%} (全期≥{L1_ALL_HIT:.0%} 仅稳定性标注) / L2≥{L2_HIT:.0%} / L3 WR≥{EXEC_WR:.0%}或PnL>0\n",
         "\n## A 级 (重点候选)\n"]
    for r in rows:
        if r["tier"] != "A":
            continue
        L.append(_fmt(r))
    L.append("\n## B 级 (观察)\n")
    for r in rows:
        if r["tier"] != "B":
            continue
        L.append(_fmt(r))
    L.append("\n## C 级 (降权/避免)\n")
    for r in rows:
        if r["tier"] != "C":
            continue
        L.append(_fmt(r))
    L.append("\n---\n_口径: 命中=方向调整后 T+1 收益>0; n<30 的信号层结论仅作参考; 执行层含 SIM 模拟盘。_")

    os.makedirs(VAULT, exist_ok=True)
    with open(VAULT / fname, "w") as f:
        f.write("\n".join(L))
    with open(OUT_JSON, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)

    # console
    print(f"═══ 标的短名单 ({now}) ═══")
    print(f"{'标的':6s} {'级':>2s} {'分':>2s} {'方向':>4s} {'L命中':>6s} {'S命中':>6s} {'近30d':>6s} {'台账':>6s} {'执WR':>5s} {'执PnL':>8s} {'告警'}")
    for r in rows:
        a = r["l1"]; b = r["l2"]; c = r["l3"]
        f = lambda v, p=0: f"{v:.{p}f}" if v is not None else "-"
        print(f"{r['symbol'][3:]:6s} {r['tier']:>2s} {r['score']:2d} {r['bias']:>4s} "
              f"{f(a['hit_long']*100,0) if a else '-':>5s}% {f(a['hit_short']*100,0) if a else '-':>5s}% "
              f"{f(a['hit_recent']*100,0) if a and a['hit_recent'] is not None else '-':>5s}% "
              f"{f(b['hit_t1']*100,0) if b else '-':>5s}% "
              f"{f(c['wr']*100,0) if c else '-':>4s} {c['pnl'] if c else 0:8.1f} {r['invert']}")
    print(f"\n输出: {VAULT / fname}")
    return 0


def _fmt(r) -> str:
    a, b, c = r["l1"], r["l2"], r["l3"]
    f = lambda v, p=2: f"{v:.{p}f}" if v is not None else "-"
    parts = [f"### {r['symbol'][3:]} ({r['tier']}级, {r['score']}/3, {r['bias']})",
             f"- 信号层: n={a['n'] if a else '-'} | LONG命中{f(a['hit_long']*100,0) if a and a['hit_long'] is not None else '-'}% SHORT命中{f(a['hit_short']*100,0) if a and a['hit_short'] is not None else '-'}% 近30d{f(a['hit_recent']*100,0) if a and a['hit_recent'] is not None else '-'}% | IC_dir={f(a['ic_dir'],2) if a else '-'} | alpha_T1={f((a['alpha_t1'] or 0)*100,2) if a else '-'}%",
             f"- 台账层: n={b['n'] if b else '-'} T+1命中{f(b['hit_t1']*100,0) if b else '-'}%",
             f"- 执行层: n={c['n'] if c else '-'} WR={f(c['wr']*100,0) if c else '-'}% PnL={c['pnl'] if c else 0:+.0f}",
             f"- 依据: {'; '.join(r['reasons']) if r['reasons'] else '证据不足'}" + (f" | ⚠️{r['invert']}" if r["invert"] else ""),
             ""]
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())