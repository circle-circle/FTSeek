#!/usr/bin/env python3
"""入场后「首次不利时刻」分布 — 判断 maefirst-adverse 轴有没有判别力 (record-only)。

问题: 上一轮发现 MFE∈[0.4,0.8) 的仓被 BE 剪掉后价格继续不利 ⇒ 怀疑根因在入场时点而非退出线。
本脚本只回答两件事:
  ① 亏单的"第一次明显不利"发生在入场后多久? (中位/分位)
  ② 这个量在**入场后可观测的时刻**上, 能不能分开最终的盈/亏? (AUC + 阈值规则的 precision/recall)
②若不能分开 ⇒ 该轴没有门前置, 结论是负面的 (不建门)。

口径: A10 已平仓, 1m bar 从 entry 到 exit; 不利幅度按价格相对 entry 的方向调整。
"""
from __future__ import annotations

import json
import sqlite3
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "data/ftseek_us.db"
BARDIR = REPO / "data" / "replay_bars"
ET = ZoneInfo("America/New_York")
OUT = REPO / "data" / "first_adverse_timing.json"

CHECKPOINTS = [15, 30, 60]
ADVERSE = [0.25, 0.5, 1.0]


def load_bars(sym: str, days: list[str]) -> list[dict]:
    out = []
    for d in days:
        p = BARDIR / f"{sym.replace('.', '_')}_{d}.json"
        if not p.exists():
            continue
        for b in json.loads(p.read_text()):
            out.append({"t": datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET),
                        "h": b[2], "l": b[3], "c": b[4]})
    out.sort(key=lambda x: x["t"])
    return out


def auc(pos: list[float], neg: list[float]) -> float:
    """Mann-Whitney AUC: 越大 = 该特征越大越像正例。"""
    if not pos or not neg:
        return 0.5
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else (0.5 if a == b else 0.0)
    return wins / (len(pos) * len(neg))


def main() -> int:
    from app.utils.timezone import to_et
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("""
        SELECT symbol, direction, entry_price, exit_price, pnl_net, mfe_pct, mae_pct,
               exit_reason, entry_time, exit_time, trade_date
        FROM trade_records
        WHERE trade_date >= '2026-07-01' AND source_type='PLAYBOOK_SIM'
          AND COALESCE(strategy_id,'playbook_sim')='playbook_sim'
          AND COALESCE(exit_price,0)>0 AND pnl_net IS NOT NULL
          AND direction IN ('LONG','SHORT')
    """)]

    recs, skipped = [], 0
    for r in rows:
        try:
            t0 = to_et(datetime.fromisoformat(r["entry_time"]))
            t1 = to_et(datetime.fromisoformat(r["exit_time"]))
        except Exception:
            skipped += 1
            continue
        days = []
        d = t0.date()
        while d <= t1.date() and len(days) < 4:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += timedelta(days=1)
        bars = [b for b in load_bars(r["symbol"], days) if t0 <= b["t"] <= t1]
        if len(bars) < 5:
            skipped += 1
            continue
        sign = 1 if r["direction"] == "LONG" else -1
        e = r["entry_price"]
        adverse = lambda px: sign * (e - px) / e * 100   # 正 = 不利
        fav = lambda px: sign * (px - e) / e * 100
        first = {}
        for thr in ADVERSE:
            hit = next((b["t"] for b in bars
                        if adverse(b["l"] if sign > 0 else b["h"]) >= thr), None)
            first[thr] = round((hit - t0).total_seconds() / 60, 1) if hit else None
        peak_t = min(range(len(bars)), key=lambda i: -fav(bars[i]["h"] if sign > 0 else bars[i]["l"]))
        rec = {"symbol": r["symbol"], "day": r["trade_date"], "dir": r["direction"],
               "pnl_net": round(r["pnl_net"], 2), "win": r["pnl_net"] > 0,
               "reason": r["exit_reason"],
               "t_first_adv_025": first[0.25], "t_first_adv_05": first[0.5],
               "t_first_adv_10": first[1.0],
               "t_to_peak_min": round((bars[peak_t]["t"] - t0).total_seconds() / 60, 1),
               "mfe_pct": r["mfe_pct"], "mae_pct": r["mae_pct"]}
        for cp in CHECKPOINTS:
            win = [b for b in bars if b["t"] <= t0 + timedelta(minutes=cp)]
            if win:
                rec[f"mae_{cp}"] = round(max(adverse(b["l"] if sign > 0 else b["h"]) for b in win), 3)
                rec[f"mfe_{cp}"] = round(max(fav(b["h"] if sign > 0 else b["l"]) for b in win), 3)
        recs.append(rec)

    win = [r for r in recs if r["win"]]
    los = [r for r in recs if not r["win"]]
    out = {"n": len(recs), "n_win": len(win), "n_loss": len(los), "skipped": skipped,
           "groups": {}, "discrim": {}, "rows": recs}

    def g(name, items, key):
        v = [r[key] for r in items if r.get(key) is not None]
        if not v:
            return
        v.sort()
        out["groups"].setdefault(name, {})[key] = {
            "n": len(v), "median": round(st.median(v), 1),
            "p25": round(v[len(v) // 4], 1), "p75": round(v[len(v) * 3 // 4], 1),
        }

    for key in ("t_first_adv_025", "t_first_adv_05", "t_first_adv_10", "t_to_peak_min",
                "mae_15", "mae_30", "mae_60", "mfe_15", "mfe_30", "mfe_60"):
        g("win", win, key)
        g("loss", los, key)

    # 判别力: 入场后可观测的特征 (mae_cp 越小 = 不利越大; 取负号使"越大越像亏")
    for key in ("mae_15", "mae_30", "mae_60", "mfe_15", "mfe_30", "mfe_60"):
        a = [-(r[key]) for r in los if r.get(key) is not None]
        b = [-(r[key]) for r in win if r.get(key) is not None]
        out["discrim"][key] = round(auc(a, b), 3)   # >0.5 = 该特征越大越像亏单

    # 规则检验: "30 分钟时不利率 ≥ x% ⇒ 预测亏" 的 precision/recall
    rules = []
    for key in ("mae_15", "mae_30", "mae_60"):
        for thr in (0.3, 0.5, 0.75, 1.0):
            pred = [r for r in recs if r.get(key) is not None and r[key] >= thr]
            if len(pred) < 20:
                continue
            tp = sum(1 for r in pred if not r["win"])
            all_loss = len(los)
            rules.append({"feature": key, "thr": thr, "n_pred": len(pred),
                          "precision": round(tp / len(pred) * 100, 1),
                          "recall": round(tp / max(all_loss, 1) * 100, 1),
                          "base_loss_rate": round(all_loss / len(recs) * 100, 1)})
    out["rules"] = rules
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print(f"== 入场后首次不利分布 (n={len(recs)} 盈 {len(win)} / 亏 {len(los)}, 跳过 {skipped}) ==")
    for key, label in (("t_first_adv_05", "首次 -0.5% 时刻(min)"),
                       ("t_first_adv_10", "首次 -1.0% 时刻(min)"),
                       ("t_to_peak_min", "到峰值时刻(min)"),
                       ("mae_30", "30min 内最大不利(%)"),
                       ("mfe_30", "30min 内最大有利(%)")):
        w = out["groups"].get("win", {}).get(key)
        l = out["groups"].get("loss", {}).get(key)
        if w and l:
            print(f"  {label:22} 盈单 中位 {w['median']:>6} (p25 {w['p25']}, p75 {w['p75']}) | "
                  f"亏单 中位 {l['median']:>6} (p25 {l['p25']}, p75 {l['p75']})")
    print("  判别力 AUC (>0.5=越大越像亏单):",
          {k: v for k, v in out["discrim"].items()})
    print("  规则检验 (precision/recall vs 基础亏率 "
          f"{rules[0]['base_loss_rate'] if rules else 0}%):")
    for r in rules:
        print(f"    {r['feature']} ≥ {r['thr']}% → 预测亏: n={r['n_pred']} "
              f"precision {r['precision']}% recall {r['recall']}%")
    print(f"产物: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())