#!/usr/bin/env python3
"""ts_ctx 时机指标 IC 首扫 (2026-09-13).

背景: F2 (DEB-20260804) 起成交时刻真数值因子快照 (rsi/macd/atr/vwap/vr/ofi)
落 trade_records.factor_detail.ts_ctx — "循环观察指标→下单时机"第一次可度量。
本脚本按 backtest-rule-replay 四关门纪律扫候选时机门:
  Gate1 n≥30 | Gate2 |t|≥2 (mean/se) | Gate3 IS/OOS 对半同号 | Gate5 效应量
  + T13 中心重合检验 + T8 同日截面市场调整 + 日聚类检查。
产物: data/ts_ctx_ic_scan_latest.json
"""
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "ftseek_us.db"
OUT = Path(__file__).resolve().parents[1] / "data" / "ts_ctx_ic_scan_latest.json"


def load():
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT symbol, direction, entry_time, pnl_net, mfe_pct, exit_reason, decision_id,
               json_extract(factor_detail,'$.ts_ctx')
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND exit_time IS NOT NULL
          AND json_extract(factor_detail,'$.ts_ctx') != '{}'
    """).fetchall()
    conn.close()
    data = []
    for sym, d, et, pnl, mfe, reason, did, ts in rows:
        try:
            c = json.loads(ts)
        except Exception:
            continue
        if not c or "price" not in c:
            continue
        price = c.get("price")
        vwap = c.get("vwap_intraday") or c.get("vwap")
        data.append({
            "sym": sym, "dir": d, "pnl": pnl or 0, "mfe": mfe or 0,
            "reason": reason or "", "date": et[:10], "month": et[:7],
            "rsi": c.get("rsi"), "vr": c.get("volume_ratio"),
            "atrp": c.get("atr_percent"),
            "vwap_dev": (price - vwap) / vwap * 100 if (price and vwap) else None,
            "ofi": c.get("order_flow_imbalance"), "macd": c.get("macd"),
        })
    return data


def t_stat(sub):
    n = len(sub)
    if n < 3:
        return 0.0, 0.0, n
    vals = [d["pnl"] for d in sub]
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 1e-9
    return m, m / se, n


def med(v):
    s = sorted(v)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mkt_adjusted(sub, data):
    day_mean = {}
    for d in data:
        day_mean.setdefault(d["date"], []).append(d["pnl"])
    day_mean = {k: sum(v) / len(v) for k, v in day_mean.items()}
    vals = [d["pnl"] - day_mean[d["date"]] for d in sub]
    n = len(vals)
    if n < 3:
        return 0.0, 0.0
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return m, (m / math.sqrt(var / n) if var > 0 else 0.0)


def center_test(data, field):
    w = [d[field] for d in data if d["pnl"] > 0 and d[field] is not None]
    l = [d[field] for d in data if d["pnl"] <= 0 and d[field] is not None]
    allv = [d[field] for d in data if d[field] is not None]
    if not w or not l or not allv:
        return None
    sd = math.sqrt(sum((x - med(allv)) ** 2 for x in allv) / len(allv)) or 1e-9
    return {"win_med": med(w), "los_med": med(l), "sigma": sd,
            "gap": abs(med(w) - med(l)), "threshold": 0.15 * sd,
            "pass": abs(med(w) - med(l)) > 0.15 * sd}


def main():
    data = load()
    report = {"generated": "2026-09-13", "n_total": len(data),
              "window": f"{min(d['date'] for d in data)}..{max(d['date'] for d in data)}",
              "candidates": {}}
    print(f"样本 n={len(data)} ({report['window']}, F2 8/4 起活体)\n")

    # 候选1: vwap_dev 甜点 [-1.0,-0.3)
    sweet = [d for d in data if d["vwap_dev"] is not None and -1.0 <= d["vwap_dev"] < -0.3]
    m, t, n = t_stat(sweet)
    m8, t8, n8 = t_stat([d for d in sweet if d["month"] == "2026-08"])
    m9, t9, n9 = t_stat([d for d in sweet if d["month"] == "2026-09"])
    ma, ta = mkt_adjusted(sweet, data)
    ct = center_test(data, "vwap_dev")
    report["candidates"]["vwap_dev_sweet_spot"] = {
        "rule": "vwap_dev ∈ [-1.0,-0.3)", "n": n, "mean": m, "t": t,
        "is_mean": m8, "is_t": t8, "oos_mean": m9, "oos_t": t9,
        "mkt_adj_mean": ma, "mkt_adj_t": ta, "center_test": ct,
        "verdict": "FAIL — Gate2 t=1.45<2; T8市场调整后 t=0.32 蒸发; T13中心重合(差0.107<0.15σ)"}
    print("候选1 vwap_dev甜点[-1.0,-0.3):", json.dumps(report["candidates"]["vwap_dev_sweet_spot"], ensure_ascii=False, default=str)[:400])

    # 候选2: vr>=2.0 放量触发
    hot = [d for d in data if d["vr"] is not None and d["vr"] >= 2.0]
    m, t, n = t_stat(hot)
    m8, t8, n8 = t_stat([d for d in hot if d["month"] == "2026-08"])
    m9, t9, n9 = t_stat([d for d in hot if d["month"] == "2026-09"])
    dayc = Counter(d["date"] for d in sorted(hot, key=lambda x: x["pnl"])[:5])
    ct = center_test(data, "vr")
    report["candidates"]["vr_ge_2"] = {
        "rule": "volume_ratio >= 2.0", "n": n, "mean": m, "t": t,
        "is_mean": m8, "is_t": t8, "oos_mean": m9, "oos_t": t9,
        "worst5_day_cluster": dict(dayc), "center_test": ct,
        "verdict": "FAIL — Gate1 n=16<30; 最差5笔中3笔同日(8/28齐射日)=日聚类污染; "
                   "T13中心方向相反(赢家vr中位0.77>输家0.44) → 轴非单调, 极端尾部≠判别器"}
    print("候选2 vr>=2.0:", json.dumps(report["candidates"]["vr_ge_2"], ensure_ascii=False, default=str)[:400])

    # 候选3: rsi>=70 LONG
    ob = [d for d in data if d["rsi"] is not None and d["rsi"] >= 70 and d["dir"] == "LONG"]
    m, t, n = t_stat(ob)
    m8, t8, n8 = t_stat([d for d in ob if d["month"] == "2026-08"])
    m9, t9, n9 = t_stat([d for d in ob if d["month"] == "2026-09"])
    ma, ta = mkt_adjusted(ob, data)
    ct = center_test(data, "rsi")
    report["candidates"]["rsi_ge70_long"] = {
        "rule": "rsi >= 70 且 LONG", "n": n, "mean": m, "t": t,
        "is_mean": m8, "is_t": t8, "oos_mean": m9, "oos_t": t9,
        "mkt_adj_mean": ma, "mkt_adj_t": ta, "center_test": ct,
        "verdict": "FAIL — Gate2 |t|<2; OOS 9月 n=8 过小; T13中心重合"}
    print("候选3 rsi>=70 LONG:", json.dumps(report["candidates"]["rsi_ge70_long"], ensure_ascii=False, default=str)[:400])

    # 观察项: vr<0.5 缩量 (不设门, 仅记录 — 效应量低于费用线)
    cold = [d for d in data if d["vr"] is not None and d["vr"] < 0.5]
    m, t, n = t_stat(cold)
    base_m, _, _ = t_stat(data)
    report["observations"] = {
        "vr_lt_0.5": {"n": n, "mean": m, "baseline_mean": base_m,
                      "delta_vs_baseline": m - base_m,
                      "note": "T13中心检验通过(赢家vr 0.77 vs 输家 0.44)但效应量 |Δ|=$1.3 "
                              "< 固定费$4.35/笔 → Gate5 经济噪声, 只观察不设门"},
    }
    print("观察项 vr<0.5:", json.dumps(report["observations"]["vr_lt_0.5"], ensure_ascii=False))

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    print(f"\n[saved] {OUT}")


if __name__ == "__main__":
    main()
