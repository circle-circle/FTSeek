#!/usr/bin/env python3
"""DEB-20261014 几何变更轴重启回放驱动 — 判据预注册于
docs/debates/DEB-20261014-geometry-restart-preregistration.md (读数前写死, 禁挪阈值).

数学 SSOT: import scripts/exit_replay_backtest 的 replay_trade + 生产
_compute_dynamic_effective_sl / _get_factor_exit_config — 本文件零退出数学, 只装配与聚合。
臂 (冻结): live_baseline / be_1.5 / dd_1.2 / pkg(be1.5+dd1.2), pess+opt 双序。
门 (冻结): 保真门 reason_match≥0.80 ∧ BE桶∈live±25% ∧ 出场%中位差≤0.35pp, 不过即停不读变体;
          G1 ΣΔ(pess)≥+$1,200  G2 日聚类t≥+2.0(全日分母)  G3 双序同签  G4 worse≤better
          G5 臂R:R(pess,净)≥0.90  G6 隔夜偏差校正后 ΣΔ_adj≥+$1,200 (裸奔水下×$48)
只读: DB mode=ro, bars 只用缓存 (ctx=None, 缺缓存计数上报不静默抓), 产物写 data/deb20261014_geometry_replay.json。

用法: PYTHONPATH=. venv/bin/python scripts/deb20261014_geometry_restart_replay.py
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

SINCE, UNTIL = "2026-09-17", "2026-10-13"     # 单一行为参数时代窗 (DEB §四)
SLIPPAGE_BPS = 3.0                              # 引擎默认
OVERNIGHT_COST_PER_HOLD = 48.0                  # G6: 3170/66, 公式预注册钉死
SIGMA_DELTA_GATE = 1200.0                       # G1/G6: 828(翻正) + 400(余量), 钉死
T_GATE = 2.0                                    # G2
RR_GATE = 0.90                                  # G5
OUT = REPO / "data" / "deb20261014_geometry_replay.json"

ARMS = [
    {"name": "live_baseline"},
    {"name": "be_1.5", "be": 1.5},
    {"name": "dd_1.2", "dd": 1.2},
    {"name": "pkg_be1.5_dd1.2", "be": 1.5, "dd": 1.2},
]


def _sign(direction: str) -> float:
    return 1.0 if direction == "LONG" else -1.0


def daily_t(day_deltas: dict, all_days: set) -> float:
    """日聚类 t, 分母=全部有样本交易日 (零Δ日计 0, skill 陷阱6: 不得只取分歧日)."""
    vals = [day_deltas.get(d, 0.0) for d in sorted(all_days)]
    if len(vals) < 2:
        return 0.0
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return 0.0
    return m / (sd / math.sqrt(len(vals)))


def main() -> int:
    factor_rules, params = eng._load_configs()
    trades = eng.load_trades(SINCE, UNTIL)
    print(f"loader: {len(trades)} 笔 ({SINCE}..{UNTIL}, PLAYBOOK_SIM/已平仓/RTH入场)")

    # ── A10 排除 (loader 不带 data_quality/exclusion_reason, 按 id 回查) ──
    ids = [t["id"] for t in trades]
    fees: dict[int, float] = {}
    bad: set[int] = set()
    if ids:
        con = sqlite3.connect(f"file:{eng.DB_PATH}?mode=ro", uri=True)
        qmarks = ",".join(str(int(i)) for i in ids)
        for rid, dq, meta, fee in con.execute(
            f"SELECT id, data_quality, metadata_json, fees_est FROM trade_records WHERE id IN ({qmarks})"
        ):
            ex = ""
            try:
                ex = (json.loads(meta or "{}") or {}).get("exclusion_reason", "") or ""
            except Exception:
                ex = ""
            if (dq or "") not in ("clean", "real") or ex:
                bad.add(rid)
            fees[rid] = float(fee) if fee is not None else None
        con.close()
    trades = [t for t in trades if t["id"] not in bad]
    print(f"A10 排除后: {len(trades)} 笔 (剔 {len(bad)})")
    if not trades:
        return 2

    # ── bars: 只用缓存 (ctx=None; miss 即上报, 不静默抓) ──
    need: set[tuple[str, str]] = set()
    for tr in trades:
        d = tr["entry_et"].date()
        while d <= tr["exit_et"].date() and (d - tr["entry_et"].date()).days <= 6:
            if d.weekday() < 5:
                need.add((tr["symbol"], d.strftime("%Y-%m-%d")))
            d += timedelta(days=1)
    bars_map: dict[tuple[str, str], list] = {}
    missing: list[tuple[str, str]] = []
    for sym, day in sorted(need):
        try:
            b = eng.fetch_bars(None, sym, day, refresh=False)
        except Exception:
            b = None
        if b:
            bars_map[(sym, day)] = b
        else:
            missing.append((sym, day))
    print(f"bars 缓存: {len(bars_map)}/{len(need)} (缺 {len(missing)}: {missing[:5]})")

    # ── attach (复刻引擎 main(), e_days=0: 几何臂不涉及隔夜延伸) ──
    runnable, skipped, skipped_fee_unknown = [], 0, 0
    for tr in trades:
        seq: list = []
        d = tr["entry_et"].date()
        while d <= tr["exit_et"].date() and (d - tr["entry_et"].date()).days <= 6:
            day_bars = bars_map.get((tr["symbol"], d.strftime("%Y-%m-%d")))
            if day_bars:
                seq.extend(day_bars)
            d += timedelta(days=1)
        seq = [b for b in seq if b["t"] > tr["entry_et"].replace(second=0, microsecond=0)]
        if len(seq) < 5:
            skipped += 1
            continue
        tr["_bars"] = seq
        tr["_exit_cfg"] = eng._get_factor_exit_config(tr["conditions"], factor_rules,
                                                      direction=tr["direction"])
        tr["_fee"] = eng._fee_of(tr)
        tr["_fees_est"] = fees.get(tr["id"])
        if tr["_fee"] is None or tr["_fees_est"] is None:
            skipped_fee_unknown += 1
            continue
        runnable.append(tr)
    print(f"可回放: {len(runnable)} 笔 (缺bar跳过 {skipped}; 费用未知跳过 {skipped_fee_unknown})")
    if not runnable:
        return 2
    all_days = {tr["entry_et"].strftime("%Y-%m-%d") for tr in runnable}

    def run_arm(var: dict, ordering: str) -> dict:
        out: dict[int, dict] = {}
        for tr in runnable:
            rp = eng.replay_trade(tr, tr["_bars"], tr["_exit_cfg"], var, params,
                                  ordering, SLIPPAGE_BPS)
            s = _sign(tr["direction"])
            gross = tr["qty"] * s * (rp["exit_price"] - tr["entry"])
            out[tr["id"]] = {
                "gross": gross, "net": gross - tr["_fees_est"], "reason": rp["reason"],
                "exit_price": rp["exit_price"], "day": tr["entry_et"].strftime("%Y-%m-%d"),
                "day0_close": tr["_bars"][-1]["c"], "entry": tr["entry"], "sign": s,
            }
        return out

    report: dict = {
        "deb": "DEB-20261014-geometry-restart-preregistration",
        "window": [SINCE, UNTIL], "n_runnable": len(runnable), "n_skipped": skipped,
        "bars_missing": len(missing), "slippage_bps": SLIPPAGE_BPS,
        "gates": {"G1_sigma_delta": SIGMA_DELTA_GATE, "G2_t": T_GATE, "G5_rr": RR_GATE,
                  "G6_overnight_cost_per_hold": OVERNIGHT_COST_PER_HOLD},
        "fidelity": {}, "arms": {}, "verdict": None,
    }

    # ── 保真门 (arm0 pess vs live; 不过即停, 变体读数不产生) ──
    base_p = run_arm(ARMS[0], "pess")
    n = len(runnable)
    reason_match = sum(1 for tr in runnable if base_p[tr["id"]]["reason"] == tr["live_reason"])
    rm_pct = reason_match / n
    pct_deltas = []
    for tr in runnable:
        s = _sign(tr["direction"])
        rp_pct = (base_p[tr["id"]]["exit_price"] - tr["entry"]) * s / tr["entry"] * 100
        lv_pct = (tr["live_exit"] - tr["entry"]) * s / tr["entry"] * 100
        pct_deltas.append(abs(rp_pct - lv_pct))
    pct_deltas.sort()
    med_delta = pct_deltas[n // 2]
    be_replay = sum(1 for tr in runnable if base_p[tr["id"]]["reason"] == "SL_BREAKEVEN")
    be_live = sum(1 for tr in runnable if tr["live_reason"] == "SL_BREAKEVEN")
    be_lo, be_hi = round(be_live * 0.75), round(be_live * 1.25)
    base_net_sum = sum(base_p[tr["id"]]["net"] for tr in runnable)
    live_net_sum = sum(tr["live_net"] for tr in runnable)
    fid = {
        "reason_match_pct": round(rm_pct, 3), "gate": 0.80,
        "exit_pct_delta_median_pp": round(med_delta, 4), "gate_median": 0.35,
        "be_bucket_replay": be_replay, "be_bucket_live": be_live, "gate_range": [be_lo, be_hi],
        "base_net_sum": round(base_net_sum, 2), "live_net_sum": round(live_net_sum, 2),
        "replay_vs_live_dev_pct": round((base_net_sum - live_net_sum) / abs(live_net_sum) * 100, 1)
        if live_net_sum else None,
    }
    fid_ok = (rm_pct >= 0.80 and be_lo <= be_replay <= be_hi and med_delta <= 0.35)
    fid["PASS"] = fid_ok
    report["fidelity"] = fid
    print(f"\n── 保真门 ──\n  reason_match={rm_pct:.3f} (≥0.80)  BE桶 {be_replay} vs live {be_live} "
          f"([{be_lo},{be_hi}])  出场%中位差={med_delta:.3f}pp (≤0.35)")
    print(f"  arm0净 ${base_net_sum:,.0f} vs live净 ${live_net_sum:,.0f} "
          f"(偏差 {fid['replay_vs_live_dev_pct']}%)")
    if not fid_ok:
        report["verdict"] = "FIDELITY_FAIL — 仪器不可用, 变体读数不产生; 重启路径改纯前向 record-only 影子 (DEB §五)"
        print(f"\n===> FIDELITY_FAIL: {report['verdict']}")
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        return 3

    # ── 变体臂 ──
    base_o = run_arm(ARMS[0], "opt")
    bases = {"pess": base_p, "opt": base_o}
    naked_uw_cost_total = 0.0
    for var in ARMS[1:]:
        arm_out: dict = {}
        for ordering in ("pess", "opt"):
            arm = run_arm(var, ordering)
            base = bases[ordering]
            d_by_id = {tr["id"]: arm[tr["id"]]["net"] - base[tr["id"]]["net"] for tr in runnable}
            day_d = defaultdict(float)
            for tr in runnable:
                day_d[arm[tr["id"]]["day"]] += d_by_id[tr["id"]]
            sigma = sum(d_by_id.values())
            better = sum(1 for v in d_by_id.values() if v > 0.01)
            worse = sum(1 for v in d_by_id.values() if v < -0.01)
            flat = n - better - worse
            pnls = [arm[tr["id"]]["net"] for tr in runnable]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            aw = statistics.mean(wins) if wins else 0.0
            al = abs(statistics.mean(losses)) if losses else 0.0
            arm_out[ordering] = {
                "sigma_delta": round(sigma, 2), "t_daily_all_days": round(daily_t(day_d, all_days), 3),
                "better": better, "worse": worse, "flat": flat,
                "arm_net_sum": round(sum(pnls), 2),
                "arm_wr": round(len(wins) / n, 3),
                "arm_rr": round(aw / al, 3) if al > 0 else None,
                "reasons": dict(sorted(
                    ((r, sum(1 for tr in runnable if arm[tr["id"]]["reason"] == r))
                     for r in {arm[tr["id"]]["reason"] for tr in runnable}),
                    key=lambda kv: -kv[1])),
            }
            if ordering == "pess":
                # G6: 裸奔水下 = live是BE而该臂非BE, 且日0收盘水下 (方向感知)
                naked = []
                for tr in runnable:
                    if tr["live_reason"] == "SL_BREAKEVEN" and arm[tr["id"]]["reason"] != "SL_BREAKEVEN":
                        c0, e, s = arm[tr["id"]]["day0_close"], arm[tr["id"]]["entry"], arm[tr["id"]]["sign"]
                        if (c0 - e) * s < 0:
                            naked.append(tr["id"])
                arm_out["naked_underwater_n"] = len(naked)
                arm_out["sigma_delta_adj"] = round(
                    arm_out["pess"]["sigma_delta"] - len(naked) * OVERNIGHT_COST_PER_HOLD, 2)
                arm_out["per_trade_delta"] = {str(k): round(v, 2) for k, v in d_by_id.items() if abs(v) > 0.01}
        p, o = arm_out["pess"], arm_out["opt"]
        g = {
            "G1_sigma>=1200": p["sigma_delta"] >= SIGMA_DELTA_GATE,
            "G2_t>=2": p["t_daily_all_days"] >= T_GATE,
            "G3_dual_sign": p["sigma_delta"] > 0 and o["sigma_delta"] > 0,
            "G4_worse<=better": p["worse"] <= p["better"],
            "G5_rr>=0.90": (p["arm_rr"] or 0) >= RR_GATE,
            "G6_adj>=1200": arm_out["sigma_delta_adj"] >= SIGMA_DELTA_GATE,
        }
        arm_out["gates"] = g
        arm_out["PASS"] = all(g.values())
        report["arms"][var["name"]] = arm_out
        print(f"\n── {var['name']} ──")
        print(f"  pess: ΣΔ=${p['sigma_delta']:+,.0f} t={p['t_daily_all_days']:+.2f} "
              f"better/worse/flat={p['better']}/{p['worse']}/{p['flat']} WR={p['arm_wr']:.1%} R:R={p['arm_rr']}")
        print(f"  opt : ΣΔ=${o['sigma_delta']:+,.0f} t={o['t_daily_all_days']:+.2f}")
        print(f"  G6: 裸奔水下 {arm_out['naked_underwater_n']} 笔 × $48 ⇒ ΣΔ_adj=${arm_out['sigma_delta_adj']:+,.0f}")
        print(f"  gates: {g} ⇒ {'PASS' if arm_out['PASS'] else 'FAIL'}")

    # ── 裁决映射 (DEB §六, 冻结) ──
    passed = [nm for nm, a in report["arms"].items() if a["PASS"]]
    if not passed:
        verdict = ("ALL_FAIL — 轴再关: 三个臂均未过预注册判据, 数字落档, 不得挪阈值重跑; "
                   "复活须新论据+新数据源 (DEB §六)")
    elif "pkg_be1.5_dd1.2" in passed:
        pkg = report["arms"]["pkg_be1.5_dd1.2"]["pess"]["sigma_delta"]
        solo = max((report["arms"][nm]["pess"]["sigma_delta"]
                    for nm in passed if nm != "pkg_be1.5_dd1.2"), default=None)
        if solo is not None and solo >= pkg:
            verdict = (f"PASS_SOLO_PREFERRED — 更简臂 {[nm for nm in passed if nm != 'pkg_be1.5_dd1.2']} "
                       f"ΣΔ≥包 ⇒ 减法纪律: 单成分进 record-only 影子, 包不立项")
        else:
            verdict = "PASS_PKG — 包进 record-only 实盘影子 (env 门控 RFC, 激活权=用户签字; C4/A7/A8 后转正)"
    else:
        verdict = (f"PASS_SOLO — {passed} 单独过线且包未过 ⇒ 更简臂进 record-only 影子 "
                   f"(env 门控 RFC, 激活权=用户签字)")
    report["verdict"] = verdict
    print(f"\n===> {verdict}")
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
