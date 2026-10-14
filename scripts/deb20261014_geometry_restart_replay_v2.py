#!/usr/bin/env python3
"""DEB-20261014 几何重启回放 v2 — 状态锚定续放 (仪器 v1 FIDELITY_FAIL 后切换).

判据预注册: docs/debates/DEB-20261014-geometry-restart-preregistration.md §六 + §10.2
  (v2 保真门 F1-F3 与 G1=+$770 修正均在变体读数产生前锁定).

原理: be/dd 参数改变结果的集合 ⊆ {live BE 退出} ∪ {live trail 退出} (未触发的线两臂恒等,
Δ≡0 构造性成立)。锚定人群从实盘记录状态 (exit_et + 实测 peak) 向后续放, 线数学全走
生产 SSOT (引擎 replay_trade + anchor 参数); 非锚定笔 arm 净 = live 净。

只读: DB mode=ro, bars 只用缓存, 产物 data/deb20261014_geometry_replay_v2.json
用法: PYTHONPATH=. venv/bin/python scripts/deb20261014_geometry_restart_replay_v2.py
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import scripts.exit_replay_backtest as eng  # noqa: E402

SINCE, UNTIL = "2026-09-17", "2026-10-13"
SLIPPAGE_BPS = 3.0
SIGMA_DELTA_GATE = 770.0        # G1 (§10.2 修正: 翻正额 369 + 余量 400, 公式钉死)
T_GATE = 2.0                    # G2
RR_GATE = 0.90                  # G5
OVERNIGHT_COST_PER_HOLD = 48.0  # G6 (3170/66 钉死)
ANCHOR_REASONS = {"SL_BREAKEVEN", "SL_TRAIL_LOCK"}
OUT = REPO / "data" / "deb20261014_geometry_replay_v2.json"

ARMS = [
    {"name": "live_baseline"},
    {"name": "be_1.5", "be": 1.5},
    {"name": "dd_1.2", "dd": 1.2},
    {"name": "pkg_be1.5_dd1.2", "be": 1.5, "dd": 1.2},
]


def daily_t(day_deltas: dict, all_days: set) -> float:
    vals = [day_deltas.get(d, 0.0) for d in sorted(all_days)]
    if len(vals) < 2:
        return 0.0
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    return m / (sd / math.sqrt(len(vals))) if sd > 0 else 0.0


def main() -> int:
    factor_rules, params = eng._load_configs()
    trades = eng.load_trades(SINCE, UNTIL)

    # ── A10 排除 + fees_est + exit_telemetry (按 id 回查) ──
    ids = [t["id"] for t in trades]
    fees: dict[int, float] = {}
    telem: dict[int, dict] = {}
    bad: set[int] = set()
    con = sqlite3.connect(f"file:{eng.DB_PATH}?mode=ro", uri=True)
    qm = ",".join(str(int(i)) for i in ids)
    for rid, dq, meta, fee in con.execute(
        f"SELECT id, data_quality, metadata_json, fees_est FROM trade_records WHERE id IN ({qm})"
    ):
        m = {}
        try:
            m = json.loads(meta or "{}") or {}
        except Exception:
            m = {}
        ex = m.get("exclusion_reason", "") or ""
        if (dq or "") not in ("clean", "real") or ex:
            bad.add(rid)
        fees[rid] = float(fee or 0.0)
        telem[rid] = m.get("exit_telemetry") or {}
    con.close()
    trades = [t for t in trades if t["id"] not in bad]
    print(f"n={len(trades)} (A10 剔 {len(bad)})")
    if not trades:
        return 2

    # ── bars (缓存 only) ──
    need: set[tuple[str, str]] = set()
    for tr in trades:
        d = tr["entry_et"].date()
        while d <= tr["exit_et"].date() and (d - tr["entry_et"].date()).days <= 6:
            if d.weekday() < 5:
                need.add((tr["symbol"], d.strftime("%Y-%m-%d")))
            d += timedelta(days=1)
    bars_map: dict = {}
    missing = []
    for sym, day in sorted(need):
        try:
            b = eng.fetch_bars(None, sym, day, refresh=False)
        except Exception:
            b = None
        (bars_map.__setitem__((sym, day), b) if b else missing.append((sym, day)))
    print(f"bars 缓存 {len(bars_map)}/{len(need)} (缺 {len(missing)})")

    # ── attach + 锚定构造 ──
    runnable, skipped, anchored, peak_mismatch = [], 0, 0, 0
    for tr in trades:
        seq = []
        d = tr["entry_et"].date()
        while d <= tr["exit_et"].date() and (d - tr["entry_et"].date()).days <= 6:
            db = bars_map.get((tr["symbol"], d.strftime("%Y-%m-%d")))
            if db:
                seq.extend(db)
            d += timedelta(days=1)
        seq = [b for b in seq if b["t"] > tr["entry_et"].replace(second=0, microsecond=0)]
        if len(seq) < 5:
            skipped += 1
            continue
        tr["_bars"] = seq
        tr["_exit_cfg"] = eng._get_factor_exit_config(tr["conditions"], factor_rules,
                                                      direction=tr["direction"])
        tr["_fees_est"] = fees.get(tr["id"], 0.0)
        tr["_sign"] = 1.0 if tr["direction"] == "LONG" else -1.0
        tr["_live_net"] = tr["live_pnl"] - tr["_fees_est"]
        tr["_day0_close"] = max((b for b in seq if b["t"].date() == tr["exit_et"].date()),
                                key=lambda b: b["t"])["c"]
        if tr["live_reason"] in ANCHOR_REASONS:
            tel = telem.get(tr["id"], {})
            pp = tel.get("peak_price")
            if pp:
                pk = (float(pp) - tr["entry"]) * tr["_sign"] / tr["entry"] * 100
                if abs(pk - (tr.get("live_mfe") or 0)) > 0.05:
                    peak_mismatch += 1
            else:
                pk = tr.get("live_mfe") or 0.0
            tr["_anchor"] = {"t": tr["exit_et"], "peak_pct": max(pk, 0.0),
                             "px": tr["live_exit"]}
            anchored += 1
        else:
            tr["_anchor"] = None
        runnable.append(tr)
    print(f"可回放 {len(runnable)} (缺bar跳过 {skipped}); 锚定人群 {anchored} "
          f"(BE {sum(1 for t in runnable if t['live_reason']=='SL_BREAKEVEN')} + "
          f"trail {sum(1 for t in runnable if t['live_reason']=='SL_TRAIL_LOCK')}); "
          f"peak 口径不一致 {peak_mismatch}")
    if not runnable or not anchored:
        return 2
    all_days = {tr["entry_et"].strftime("%Y-%m-%d") for tr in runnable}
    anchored_trades = [t for t in runnable if t["_anchor"]]

    def run_arm(var, ordering):
        """返回 {id: {net, reason, day0_close, entry, sign, refire_lag_bars}}; 非锚定=live."""
        out = {}
        for tr in runnable:
            if tr["_anchor"] is None:                     # F2 构造: Δ≡0
                out[tr["id"]] = {"net": tr["_live_net"], "reason": tr["live_reason"],
                                 "delta_zero": True}
                continue
            rp = eng.replay_trade(tr, tr["_bars"], tr["_exit_cfg"], var, params,
                                  ordering, SLIPPAGE_BPS, anchor=tr["_anchor"])
            gross = tr["qty"] * tr["_sign"] * (rp["exit_price"] - tr["entry"])
            out[tr["id"]] = {"net": gross - tr["_fees_est"], "reason": rp["reason"],
                             "exit_price": rp["exit_price"], "delta_zero": False,
                             "mfe": rp["mfe_pct"]}
        return out

    report = {"deb": "DEB-20261014 §10.2", "window": [SINCE, UNTIL],
              "n": len(runnable), "n_anchored": anchored, "peak_mismatch": peak_mismatch,
              "gates": {"G1": SIGMA_DELTA_GATE, "G2_t": T_GATE, "G5_rr": RR_GATE,
                        "G6_cost": OVERNIGHT_COST_PER_HOLD},
              "fidelity_v2": {}, "arms": {}, "verdict": None}

    # ── F1/F3 保真门: arm0 pess 锚定再现 ──
    base_p = run_arm(ARMS[0], "pess")
    refire_ok, price_devs = 0, []
    for tr in anchored_trades:
        r = base_p[tr["id"]]
        if r["reason"] == tr["live_reason"]:
            refire_ok += 1
        if r["reason"] != "ANCHOR_NO_BARS":
            price_devs.append(abs(r["exit_price"] - tr["live_exit"]) / tr["live_exit"] * 100)
    price_devs.sort()
    med_dev = price_devs[len(price_devs) // 2] if price_devs else 9.9
    refire_rate = refire_ok / anchored
    f_ok = refire_rate >= 0.90 and med_dev <= 0.20
    report["fidelity_v2"] = {"F1_refire_reason_rate": round(refire_rate, 3),
                             "F1_median_price_dev_pct": round(med_dev, 4),
                             "F2_delta_zero_by_construction": True, "PASS": f_ok}
    print(f"\n── v2 保真门 ── F1 再现率={refire_rate:.3f} (≥0.90)  再现价中位差={med_dev:.4f}% (≤0.20)")
    if not f_ok:
        report["verdict"] = "FIDELITY_FAIL_V2 — 锚定仪器仍不可用; 重启路径改纯前向 record-only 影子"
        print(f"===> {report['verdict']}")
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        return 3

    # ── 变体臂 ──
    base_o = run_arm(ARMS[0], "opt")
    bases = {"pess": base_p, "opt": base_o}
    for var in ARMS[1:]:
        arm_out = {}
        for ordering in ("pess", "opt"):
            arm = run_arm(var, ordering)
            base = bases[ordering]
            d_by_id = {tr["id"]: arm[tr["id"]]["net"] - base[tr["id"]]["net"] for tr in runnable}
            assert all(d_by_id[tr["id"]] == 0.0 for tr in runnable if tr["_anchor"] is None), "F2 违例"
            day_d = defaultdict(float)
            for tr in runnable:
                day_d[tr["entry_et"].strftime("%Y-%m-%d")] += d_by_id[tr["id"]]
            sigma = sum(d_by_id.values())
            better = sum(1 for v in d_by_id.values() if v > 0.01)
            worse = sum(1 for v in d_by_id.values() if v < -0.01)
            pnls = [arm[tr["id"]]["net"] for tr in runnable]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            aw = statistics.mean(wins) if wins else 0.0
            al = abs(statistics.mean(losses)) if losses else 0.0
            arm_out[ordering] = {
                "sigma_delta": round(sigma, 2),
                "t_daily_all_days": round(daily_t(day_d, all_days), 3),
                "better": better, "worse": worse, "flat": len(runnable) - better - worse,
                "arm_wr": round(len(wins) / len(runnable), 3),
                "arm_rr": round(aw / al, 3) if al > 0 else None,
                "cont_reasons": dict(sorted(
                    ((r, sum(1 for t in anchored_trades if arm[t["id"]]["reason"] == r))
                     for r in {arm[t["id"]]["reason"] for t in anchored_trades}),
                    key=lambda kv: -kv[1])),
            }
            if ordering == "pess":
                naked = []
                for tr in anchored_trades:
                    if tr["live_reason"] == "SL_BREAKEVEN" and arm[tr["id"]]["reason"] != "SL_BREAKEVEN":
                        if (tr["_day0_close"] - tr["entry"]) * tr["_sign"] < 0:
                            naked.append(tr["id"])
                arm_out["naked_underwater_n"] = len(naked)
                arm_out["sigma_delta_adj"] = round(
                    arm_out["pess"]["sigma_delta"] - len(naked) * OVERNIGHT_COST_PER_HOLD, 2)
        p, o = arm_out["pess"], arm_out["opt"]
        g = {"G1": p["sigma_delta"] >= SIGMA_DELTA_GATE,
             "G2": p["t_daily_all_days"] >= T_GATE,
             "G3": p["sigma_delta"] > 0 and o["sigma_delta"] > 0,
             "G4": p["worse"] <= p["better"],
             "G5": (p["arm_rr"] or 0) >= RR_GATE,
             "G6": arm_out["sigma_delta_adj"] >= SIGMA_DELTA_GATE}
        arm_out["gates"] = g
        arm_out["PASS"] = all(g.values())
        report["arms"][var["name"]] = arm_out
        print(f"\n── {var['name']} ──")
        print(f"  pess: ΣΔ=${p['sigma_delta']:+,.0f} (adj ${arm_out['sigma_delta_adj']:+,.0f}, "
              f"裸奔水下 {arm_out['naked_underwater_n']}) t={p['t_daily_all_days']:+.2f} "
              f"bet/wor={p['better']}/{p['worse']} WR={p['arm_wr']:.1%} R:R={p['arm_rr']}")
        print(f"  opt : ΣΔ=${o['sigma_delta']:+,.0f}  续放归宿={p['cont_reasons']}")
        print(f"  gates={g} ⇒ {'PASS' if arm_out['PASS'] else 'FAIL'}")

    # ── 裁决映射 (§六 冻结) ──
    passed = [nm for nm, a in report["arms"].items() if a["PASS"]]
    if not passed:
        verdict = ("ALL_FAIL — 轴再关: 全部臂未过预注册判据, 数字落档, 不得挪阈值重跑; "
                   "复活须新论据+新数据源")
    elif "pkg_be1.5_dd1.2" in passed:
        pkg = report["arms"]["pkg_be1.5_dd1.2"]["pess"]["sigma_delta"]
        solos = [nm for nm in passed if nm != "pkg_be1.5_dd1.2"]
        solo_max = max((report["arms"][nm]["pess"]["sigma_delta"] for nm in solos), default=None)
        verdict = (f"PASS_SOLO_PREFERRED — {solos} ΣΔ≥包 ⇒ 减法纪律: 单成分进 record-only 影子"
                   if solo_max is not None and solo_max >= pkg else
                   "PASS_PKG — 包进 record-only 实盘影子 (env 门控 RFC, 激活权=用户签字)")
    else:
        verdict = f"PASS_SOLO — {passed} 进 record-only 影子 (env 门控 RFC, 激活权=用户签字)"
    report["verdict"] = verdict
    print(f"\n===> {verdict}")
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
