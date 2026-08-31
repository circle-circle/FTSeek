#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成交口径股性档案生成器 (DEB-20260831 §四).

现有 symbol_rule_cards 是"规则×标的"统计 (R5跳空延续等), 缺三样真正有预测力的股性:
  1. 成交口径胜负 (本系统实际交易该股的结果 — DA分析次数口径无预测力, 8/31实锤)
  2. 盘前行为档案 (该股盘前±x%后开盘30min延续/回归率 — 来自 premarket_trend_outcomes)
  3. 开盘动量留存率 (开盘15分钟方向与当日收盘方向的一致性 — 来自 1m bar)

口径纪律 (用户 8/22 四原则 + n≥30 纪律):
  - 样本 < 30 只显示样本数, 不给评级/倾向词 (防"0笔看多"式选样混淆)
  - 输出是素材 (L2), 非交易指令; 消费方注入时须附"背景事实非方向先验"声明
产物: data/symbol_trading_cards/US_<SYM>.json (与 symbol_rule_cards 分目录防混)
用法: python3 scripts/build_symbol_trading_cards.py
"""
import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"
OUT_DIR = REPO / "data" / "symbol_trading_cards"
OUT_DIR.mkdir(exist_ok=True)
MIN_SAMPLE = 30  # n<30 不给评级


def trading_stats():
    """成交口径: 本系统实际交易结果 (非DA分析次数)."""
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT symbol, COUNT(*) n,
               SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(pnl_net), 2) net,
               SUM(CASE WHEN COALESCE(mfe_pct, 0) <= 0.001 THEN 1 ELSE 0 END) mfe0,
               SUM(CASE WHEN exit_reason = 'SL_INITIAL' THEN 1 ELSE 0 END) sl_initial
        FROM trade_records
        WHERE exit_price > 0 AND entry_time >= '2026-06-27'
        GROUP BY symbol""").fetchall()
    db.close()
    out = {}
    for r in rows:
        n = r["n"]
        stat = {"n": n, "net": r["net"],
                "wr": round(r["wins"] / n, 3) if n else None,
                "avg": round(r["net"] / n, 1) if n else None,
                "mfe0_pct": round(r["mfe0"] / n, 2) if n else None,
                "sl_initial_pct": round(r["sl_initial"] / n, 2) if n else None,
                "tier": "rated" if n >= MIN_SAMPLE else f"insufficient(n={n})"}
        out[r["symbol"]] = stat
    return out


def premarket_behavior():
    """盘前行为档案: 该股盘前方向后开盘30min的延续/回归统计."""
    p = REPO / "data" / "premarket_trend_outcomes.jsonl"
    if not p.exists():
        return {}
    acc = defaultdict(lambda: {"n": 0, "triggered": 0, "sum_pnl": 0.0})
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "instruction":
            continue
        sym = rec.get("symbol", "")
        sh = rec.get("shadow") or {}
        a = acc[sym]
        a["n"] += 1
        if sh.get("triggered"):
            a["triggered"] += 1
            a["sum_pnl"] += sh.get("pnl_pct", 0.0) or 0.0
    out = {}
    for sym, a in acc.items():
        if a["n"] < 5:
            continue
        out[sym] = {"n": a["n"], "trigger_rate": round(a["triggered"] / a["n"], 2),
                    "avg_shadow_pnl_pct": round(a["sum_pnl"] / max(a["triggered"], 1), 2),
                    "tier": "rated" if a["triggered"] >= MIN_SAMPLE else f"insufficient(triggered={a['triggered']})"}
    return out


def momentum_retention():
    """开盘动量留存率: 开盘15分钟方向与当日收盘方向一致的比例 (1m bar)."""
    bar_dir = REPO / "data" / "replay_bars_1m"
    out = {}
    for fp in sorted(bar_dir.glob("US_*.json")):
        sym = "US." + fp.stem.replace("US_", "")
        try:
            data = json.loads(fp.read_text())
        except Exception:
            continue
        days = defaultdict(list)
        for row in data:
            days[str(row[0])[:10]].append(row)
        agree, n = 0, 0
        for d, bars in days.items():
            bars = sorted(bars, key=lambda b: str(b[0]))
            if len(bars) < 20:
                continue
            o = bars[0][1]
            m15 = next((b[4] for b in bars if str(b[0])[11:16] >= "09:45"), None)
            last_c = bars[-1][4]
            if m15 is None:
                continue
            n += 1
            if (m15 - o) * (last_c - o) > 0:
                agree += 1
        if n < MIN_SAMPLE:
            continue
        out[sym] = {"n_days": n, "retention": round(agree / n, 2), "tier": "rated"}
    return out


def main():
    ts = trading_stats()
    pb = premarket_behavior()
    mr = momentum_retention()
    syms = sorted(set(ts) | set(pb) | set(mr))
    written = rated = 0
    for sym in syms:
        card = {
            "symbol": sym,
            "schema": "symbol_trading_card/1.0",
            "generated": date.today().isoformat(),
            "usage": "L2素材: 本系统成交结果+盘前行为+动量留存, 非方向先验; "
                     "insufficient 档不得读出评级倾向",
            "trading_record": ts.get(sym),
            "premarket_behavior": pb.get(sym),
            "momentum_retention": mr.get(sym),
        }
        if not any(card[k] for k in ("trading_record", "premarket_behavior", "momentum_retention")):
            continue
        (OUT_DIR / f"{sym.replace('.', '_')}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=1))
        written += 1
        if any((card[k] or {}).get("tier") == "rated" for k in
               ("trading_record", "premarket_behavior", "momentum_retention")):
            rated += 1
    print(f"写卡 {written} 只 (含 rated 档 {rated} 只) → {OUT_DIR}")
    # 实证样例: 8/31 事故标的
    for s in ("US.MU", "US.SNDK", "US.AFRM"):
        fp = OUT_DIR / f"{s.replace('.', '_')}.json"
        if fp.exists():
            c = json.loads(fp.read_text())
            tr = c.get("trading_record") or {}
            print(f"  {s}: n={tr.get('n')} net={tr.get('net')} wr={tr.get('wr')} "
                  f"mfe0={tr.get('mfe0_pct')} sl_initial={tr.get('sl_initial_pct')} tier={tr.get('tier')}")


if __name__ == "__main__":
    main()
