#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capital_flow 日级信号回放验证 (四关门+安慰剂)
==============================================
研究问题: 当日大单净流入/流出的方向是否携带次日(收盘→收盘)信息.

  事件: (symbol, date) 日级 large_in_flow > 0 (INFLOW) / < 0 (OUTFLOW)
  结果: 次日原始收益 raw_t1 = close_t1/close_t0 - 1
        (signal_snapshots.fwd_ret_t1 是按信号方向调整过的收益 — LONG 原值 /
        SHORT 反号 — 直接拿来当原始收益会污染统计, 此处从 close_t0/close_t1
        重算原始口径; 2026-08-22b 复查修复, 原内嵌版本有两处致命错:
        ①查错 db — signal_snapshots 在独立 signal_snapshots.db 而非
        ftseek_us.db, 30天到点自动触发必然崩; ②用了方向调整收益)

  四关门 (预注册, 全过才算有效):
    G1 n:    每组事件数 ≥ 30
    G2 t:    单样本 t 检验 |t| ≥ 2.0 (组均值 vs 0)
    G3 IS/OOS: 按日期对半分样本内/外, 两组均值同号
    G4 安慰剂: 事件标签随机重排 100 轮, 观察到的组间均值差需落在安慰剂
       分布 95% 之外 (p < 0.05)
  效应量地板: |组均值| ≥ 0.3%

只读: 零交易路径改动. 输出 data/capital_flow_replay_verdict.json.
回滚: 删本文件 + verdict json.
"""
import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats

BASE = Path("/opt/ftseek/FTSeek-US")
FLOW_DAILY = BASE / "data" / "capital_flow_daily.jsonl"
SNAP_DB = BASE / "data" / "signal_snapshots.db"   # signal_snapshots 在独立 db
VERDICT_OUT = BASE / "data" / "capital_flow_replay_verdict.json"

MIN_N = 30
MIN_T = 2.0
MIN_EFFECT = 0.003   # 0.3%
PLACEBO_ROUNDS = 100
SEED = 42


def load_events():
    """日级流事件 × 次日原始收益 (快照同日多行时收益口径一致, 任取一行)."""
    flow = {}
    try:
        for line in open(FLOW_DAILY):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            flow[(r["symbol"], r["date"])] = r
    except FileNotFoundError:
        return []

    raw_t1 = {}
    if SNAP_DB.exists():
        db = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
        try:
            for sym, date, c0, c1 in db.execute("""
                    SELECT symbol, trade_date, close_t0, close_t1
                    FROM signal_snapshots
                    WHERE close_t0 IS NOT NULL AND close_t0 > 0
                      AND close_t1 IS NOT NULL AND close_t1 > 0
                """):
                raw_t1[(sym, str(date))] = float(c1) / float(c0) - 1.0
        finally:
            db.close()

    events = []
    for (sym, date), r in flow.items():
        ret = raw_t1.get((sym, date))
        if ret is None:
            continue
        large = float(r.get("large_in_flow") or 0)
        if large > 0:
            events.append({"symbol": sym, "date": date, "signal": "INFLOW", "ret": ret})
        elif large < 0:
            events.append({"symbol": sym, "date": date, "signal": "OUTFLOW", "ret": ret})
    return events


def _group_stats(rets):
    out = {"n": len(rets),
           "mean": float(np.mean(rets)) if rets else 0.0,
           "t": 0.0}
    if len(rets) >= 2:
        out["t"] = float(stats.ttest_1samp(rets, 0).statistic)
    return out


def replay():
    events = load_events()
    inflow = [e["ret"] for e in events if e["signal"] == "INFLOW"]
    outflow = [e["ret"] for e in events if e["signal"] == "OUTFLOW"]

    verdict = {
        "generated_at": datetime.now().isoformat(),
        "n_events": len(events),
        "inflow": _group_stats(inflow),
        "outflow": _group_stats(outflow),
        "gates": {},
    }

    # G1/G2/G5(效应量)
    for name, rets in (("inflow", inflow), ("outflow", outflow)):
        g = verdict[name]
        verdict["gates"][f"n_{name}"] = g["n"] >= MIN_N
        verdict["gates"][f"t_{name}"] = abs(g["t"]) >= MIN_T
        verdict["gates"][f"effect_{name}"] = abs(g["mean"]) >= MIN_EFFECT

    # G3: IS/OOS 按日期对半, 均值同号
    dates = sorted({e["date"] for e in events})
    if len(dates) >= 2:
        mid = dates[len(dates) // 2]
        is_is = [e["ret"] for e in events if e["date"] < mid]
        oos = [e["ret"] for e in events if e["date"] >= mid]
        m_is = float(np.mean(is_is)) if is_is else 0.0
        m_oos = float(np.mean(oos)) if oos else 0.0
        verdict["is_oos"] = {"split_date": mid, "is_mean": m_is, "oos_mean": m_oos}
        verdict["gates"]["is_oos_same_sign"] = (m_is * m_oos) > 0
    else:
        verdict["is_oos"] = {"split_date": None, "is_mean": 0.0, "oos_mean": 0.0}
        verdict["gates"]["is_oos_same_sign"] = False

    # G4: 安慰剂 — 随机重排 INFLOW/OUTFLOW 标签, 组间均值差的分布
    if inflow and outflow:
        rng = random.Random(SEED)
        rets_all = [e["ret"] for e in events]
        n_in = len(inflow)
        obs_diff = verdict["inflow"]["mean"] - verdict["outflow"]["mean"]
        hits = 0
        for _ in range(PLACEBO_ROUNDS):
            rng.shuffle(rets_all)
            d = float(np.mean(rets_all[:n_in])) - float(np.mean(rets_all[n_in:]))
            if abs(d) >= abs(obs_diff):
                hits += 1
        p = hits / PLACEBO_ROUNDS
        verdict["placebo"] = {"rounds": PLACEBO_ROUNDS, "observed_diff": obs_diff, "p": p}
        verdict["gates"]["placebo"] = p < 0.05
    else:
        verdict["placebo"] = {"rounds": PLACEBO_ROUNDS, "observed_diff": None, "p": None}
        verdict["gates"]["placebo"] = False

    verdict["verdict"] = {
        "inflow_pass": all(verdict["gates"].get(f"{g}_inflow", False)
                           for g in ("n", "t", "effect")),
        "outflow_pass": all(verdict["gates"].get(f"{g}_outflow", False)
                            for g in ("n", "t", "effect")),
        "all_gates_pass": all(verdict["gates"].values()),
    }
    verdict["note"] = ("四关门全过才构成'有效信号'证据; 任一门不过 = 证据不足, "
                       "门维持现状 (证伪门是 fail-open 的, 不依赖本判定的通过).")

    VERDICT_OUT.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return verdict


if __name__ == "__main__":
    replay()
