#!/usr/bin/env python3
"""[DEB-20261016 L3] 高信心档倒挂守卫影子 — record-only, 零生产耦合.

背景: 审计 + DEB-20261016 信心面归因 — 0.7 档 (双模型共识) 逐月众数 −6~−8bps, 9 月
(+20.2) 是唯一强正月; conf 本身近零 IC (r=0.031, RFC-124)。本影子**不是择时器**,
是防尾部哨兵: 当高档 (0.7) 滚动 20 交易日窗口的方向调整后日聚类均值持续为负时,
产出「降权候选建议」标记 (record-only, 不自动改仓/改权)。

口径纪律 (DEB-20261016): 精确档值 (禁 ROUND 并桶); era ≥2026-07-01 (A10);
fwd_ret_t1 已方向调整; 日聚类 (先逐日均值再对日序列统计); n<30 只记录不标旗。

预注册判据 (期间禁改): 高档滚动均值 <0 且 |t|>2 (日聚类) 且持续 ≥5 个观测日 ∧
窗口内高档已决笔 n≥30 ⇒ 标 suggestion=CONSIDER_DOWNWEIGHT (供人工裁决, 不自动执行)。
死线: 2026-11-30 复核 (注册表 recheck)。

cron: 55 6 * * 2-6 (IC 回填 06:30 之后); 幂等 (按 date UPSERT 语义=当日重跑覆盖)。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "signal_snapshots.db"
OUT = REPO / "data" / "shadow" / "conf_tier_guard.jsonl"
ERA_START = "2026-07-01"
ROLL_DAYS = 20            # 滚动窗 (交易日)
TIERS = ("0.35", "0.5", "0.55", "0.7")
HIGH_TIER = "0.7"
MIN_N = 30
MIN_PERSIST_DAYS = 5


def _tier(conf) -> str:
    """精确档 (L0-3 口径: 禁 ROUND 并桶); 非 canonical 值归 'other'。"""
    if conf is None:
        return "other"
    v = float(conf)
    for t in TIERS:
        if abs(v - float(t)) < 1e-9:
            return t
    return "other"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_date, confidence, direction, fwd_ret_t1 FROM signal_snapshots "
        "WHERE fwd_ret_t1 IS NOT NULL AND trade_date >= ? "
        "AND direction IN ('LONG','SHORT')", (ERA_START,)).fetchall()
    conn.close()

    # 日 × 档 聚合 (方向调整后 fwd_ret_t1 的日均)
    by_day_tier = defaultdict(lambda: defaultdict(list))
    for r in rows:
        t = _tier(r["confidence"])
        by_day_tier[r["trade_date"]][t].append(float(r["fwd_ret_t1"]))

    days = sorted(by_day_tier)
    window = days[-ROLL_DAYS:]
    today = days[-1] if days else None
    rec = {"date": today, "roll_days": ROLL_DAYS, "era_start": ERA_START,
           "tiers": {}, "suggestion": None,
           "generated_at": datetime.now().isoformat(timespec="seconds")}
    for t in TIERS:
        day_means = []
        for d in window:
            vals = by_day_tier[d].get(t) or []
            if vals:
                day_means.append(sum(vals) / len(vals))
        n_trades = sum(len(by_day_tier[d].get(t) or []) for d in window)
        cell = {"n": n_trades, "n_days": len(day_means)}
        if len(day_means) >= 2:
            import statistics as st
            mu = sum(day_means) / len(day_means)
            se = st.stdev(day_means) / (len(day_means) ** 0.5)
            cell.update({"day_mean_pct": round(mu, 4),
                         "t": round(mu / se, 3) if se > 0 else None})
        rec["tiers"][t] = cell

    # 持续性: 读历史 jsonl, 高档连续负向天数 (含今日)
    hist = []
    if OUT.exists():
        hist = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    high = rec["tiers"].get(HIGH_TIER, {})
    neg_today = bool(high.get("day_mean_pct") is not None and high["day_mean_pct"] < 0)
    streak = 0
    for h in reversed(hist):
        hm = (h.get("tiers") or {}).get(HIGH_TIER, {})
        if hm.get("day_mean_pct") is not None and hm["day_mean_pct"] < 0:
            streak += 1
        else:
            break
    streak += 1 if neg_today else 0
    rec["high_tier_negative_streak_days"] = streak
    if (neg_today and streak >= MIN_PERSIST_DAYS and (high.get("n") or 0) >= MIN_N
            and abs(high.get("t") or 0) > 2):
        rec["suggestion"] = "CONSIDER_DOWNWEIGHT"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [l for l in OUT.read_text().splitlines()
             if l.strip() and json.loads(l).get("date") != today] if OUT.exists() else []
    lines.append(json.dumps(rec, ensure_ascii=False))
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(rec, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
