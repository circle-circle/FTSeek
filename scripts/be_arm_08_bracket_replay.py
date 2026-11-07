#!/usr/bin/env python3
"""武装线 0.4 → 0.8 的**受限区间**估计 (record-only 分析, 不下单不写库)。

为什么不是"再跑一次全量回放": 官方引擎三次保真失败 (arm0 偏差 34%/53%/182%),
失真来自 trailing 状态/费用垫/出场构成。本脚本**故意缩小问题** —— 只回答一句:

    对 MFE∈[0.4,0.8) 的仓 (0.8 规则下永不武装、0.4 规则下被 BE 剪掉),
    如果不武装, 它们的结局是什么?

因此只模拟两件事: 初始 SL + (可选) 武装在 0.8 的保本线; **不模拟 trailing**(右尾不动),
给出 pess (完全不武装) / opt (0.8 武装) 两条边, 区间才是结论形态。

保真自检: 用 MFE<0.4 的仓 (两条规则下都不武装 ⇒ 本脚本的模拟应与实盘同结局) 当对照组,
比对"模拟出场 vs 实盘出场"的价差; 偏差过大则本脚本读数同样作废。
"""
from __future__ import annotations

import argparse
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
BAR_DIR = REPO / "data" / "replay_bars"
ET = ZoneInfo("America/New_York")
OUT = REPO / "data" / "be_arm08_bracket.json"

ARM_OLD, ARM_NEW = 0.4, 0.8
BE_CUSHION_PCT = 0.31      # 实盘 BE 净线 ≈ entry ±(费用+滑点垫); 与 production 同量级
HOLD_CAP_H = 8.0           # 无事件则按当日收盘/最长持有封顶


def bar_path(sym: str, day: str) -> Path:
    # 缓存文件名用下划线: US.JD → US_JD_<day>.json (带点会 100% miss 且静默返回空)
    return BAR_DIR / f"{sym.replace('.', '_')}_{day}.json"


def load_bars(sym: str, days: list[str]) -> list[dict]:
    out = []
    for d in days:
        p = bar_path(sym, d)
        if not p.exists():
            continue
        for b in json.loads(p.read_text()):
            ts = datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            out.append({"t": ts, "o": b[1], "h": b[2], "l": b[3], "c": b[4]})
    out.sort(key=lambda x: x["t"])
    return out


def pnl_bps(direction: str, entry: float, px: float) -> float:
    d = (px - entry) if direction == "LONG" else (entry - px)
    return d / entry * 1e4


def simulate(tr: dict, bars: list[dict], arm: float | None) -> dict:
    """arm=None ⇒ 完全不加 BE(只有初始 SL); arm=0.8 ⇒ 峰值过 0.8 后抬到净保本线。"""
    d, entry, sl = tr["direction"], tr["entry_price"], tr["sl"]
    t0, t1 = tr["entry_et"], tr["exit_et"]
    # 观测窗必须延伸到"实盘出场之后": 否则未武装的仓在出场时刻就被截断,
    # 会系统性低估"不武装"的下行 (那正是本问题唯一的成本来源)。
    hard_end = datetime.combine(t1.date(), datetime.min.time(), tzinfo=ET).replace(hour=16)
    hard_end = min(hard_end, t0 + timedelta(hours=HOLD_CAP_H))
    use = [b for b in bars if t0 <= b["t"] <= hard_end]
    if not use:
        return {"ok": False}
    peak = entry
    armed = False
    for b in use:
        hi, lo = b["h"], b["l"]
        if d == "LONG":
            peak = max(peak, hi)
            peak_pct = (peak - entry) / entry * 100
            # 先判 SL(同一根内保守: 先打止损)
            if lo <= sl:
                return {"ok": True, "px": sl, "why": "SL", "t": b["t"], "armed": armed}
            if arm is not None and peak_pct >= arm:
                armed = True
            if armed and lo <= entry * (1 + BE_CUSHION_PCT / 100):
                return {"ok": True, "px": entry * (1 + BE_CUSHION_PCT / 100), "why": "BE",
                        "t": b["t"], "armed": True}
        else:
            peak = min(peak, lo)
            peak_pct = (entry - peak) / entry * 100
            if hi >= sl:
                return {"ok": True, "px": sl, "why": "SL", "t": b["t"], "armed": armed}
            if arm is not None and peak_pct >= arm:
                armed = True
            if armed and hi >= entry * (1 - BE_CUSHION_PCT / 100):
                return {"ok": True, "px": entry * (1 - BE_CUSHION_PCT / 100), "why": "BE",
                        "t": b["t"], "armed": True}
    return {"ok": True, "px": use[-1]["c"], "why": "END", "t": use[-1]["t"], "armed": armed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("""
        SELECT t.symbol, t.direction, t.quantity, t.entry_price, t.exit_price,
               t.realized_pnl, t.fees_est, t.pnl_net, t.mfe_pct, t.mae_pct,
               t.exit_reason, t.entry_time, t.exit_time, t.trade_date, t.decision_id,
               (SELECT stop_loss FROM playbook_instructions p
                 WHERE p.decision_id=t.decision_id AND p.stop_loss IS NOT NULL
                 ORDER BY p.id DESC LIMIT 1) AS sl
        FROM trade_records t
        WHERE t.trade_date >= '2026-07-01' AND t.source_type='PLAYBOOK_SIM'
          AND COALESCE(t.strategy_id,'playbook_sim')='playbook_sim'
          AND COALESCE(t.exit_price,0)>0 AND t.mfe_pct IS NOT NULL
          AND t.direction IN ('LONG','SHORT')
        ORDER BY t.trade_date
    """)]
    if args.limit:
        rows = rows[: args.limit]

    res = {"A_mfe_lt_04": [], "B_mfe_04_08": [], "C_mfe_ge_08": []}
    skipped = 0
    why = {"ts": 0, "no_sl": 0, "no_bars": 0, "sim_fail": 0}
    for r in rows:
        try:
            from app.utils.timezone import to_et
            r["entry_et"] = to_et(datetime.fromisoformat(r["entry_time"]))
            r["exit_et"] = to_et(datetime.fromisoformat(r["exit_time"]))
        except Exception:
            skipped += 1; why["ts"] += 1
            continue
        if not r.get("sl"):
            skipped += 1; why["no_sl"] += 1
            continue
        days = []
        d0 = r["entry_et"].date()
        for k in range(3):
            dd = d0 + timedelta(days=k)
            if dd.weekday() < 5 and dd <= r["exit_et"].date():
                days.append(dd.isoformat())
        bars = load_bars(r["symbol"], days)
        if not bars:
            skipped += 1; why["no_bars"] += 1
            continue
        base = simulate(r, bars, ARM_OLD)   # 同引擎基线: 把模型偏差抵掉再谈边际
        pess = simulate(r, bars, None)
        opt = simulate(r, bars, ARM_NEW)
        if not (pess["ok"] and opt["ok"] and base["ok"]):
            skipped += 1; why["sim_fail"] += 1
            continue
        live_bps = pnl_bps(r["direction"], r["entry_price"], r["exit_price"])
        rec = {"symbol": r["symbol"], "day": r["trade_date"], "dir": r["direction"],
               "mfe": r["mfe_pct"], "live_reason": r["exit_reason"],
               "live_bps": round(live_bps, 1),
               "base04_bps": round(pnl_bps(r["direction"], r["entry_price"], base["px"]), 1),
               "pess_bps": round(pnl_bps(r["direction"], r["entry_price"], pess["px"]), 1),
               "opt_bps": round(pnl_bps(r["direction"], r["entry_price"], opt["px"]), 1),
               "pess_why": pess["why"], "opt_why": opt["why"],
               "n": r["quantity"], "notional": round(r["quantity"] * r["entry_price"], 0)}
        mfe = r["mfe_pct"] or 0.0
        key = "A_mfe_lt_04" if mfe < ARM_OLD else ("B_mfe_04_08" if mfe < ARM_NEW else "C_mfe_ge_08")
        res[key].append(rec)

    def agg(name, items):
        if not items:
            return {"n": 0}
        dl = [i["base04_bps"] for i in items]
        dpe = [i["pess_bps"] for i in items]
        do = [i["opt_bps"] for i in items]
        notional = sum(i["notional"] for i in items)
        real = [i["live_bps"] for i in items]
        return {"n": len(items),
                "live_bps_mean": round(st.mean(real), 1),
                "base04_bps_mean": round(st.mean(dl), 1),
                "model_bias_bps": round(st.mean(dl) - st.mean(real), 1),
                "pess_bps_mean": round(st.mean(dpe), 1),
                "opt_bps_mean": round(st.mean(do), 1),
                "d_pess": round(st.mean([p - l for p, l in zip(dpe, dl)]), 1),
                "d_opt": round(st.mean([o - l for o, l in zip(do, dl)]), 1),
                "usd_pess": round(sum((i["pess_bps"] - i["live_bps"]) / 1e4 * i["notional"] for i in items), 0),
                "usd_opt": round(sum((i["opt_bps"] - i["live_bps"]) / 1e4 * i["notional"] for i in items), 0),
                "pess_sl_share": round(100 * sum(1 for i in items if i["pess_why"] == "SL") / len(items), 1)}

    out = {"skipped": skipped, "skip_reasons": why, "buckets": {k: agg(k, v) for k, v in res.items()},
           "sample": {k: v[:400] for k, v in res.items()}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"== 武装线 0.4→0.8 受限区间估计 (跳过 {skipped}) ==")
    for k in ("A_mfe_lt_04", "B_mfe_04_08", "C_mfe_ge_08"):
        a = out["buckets"][k]
        if not a.get("n"):
            print(f"{k}: 无样本")
            continue
        print(f"{k}: n={a['n']} live {a['live_bps_mean']:+.1f}bps | "
              f"pess {a['pess_bps_mean']:+.1f} (Δ{a['d_pess']:+.1f}, ${a['usd_pess']:+,.0f}) | "
              f"opt {a['opt_bps_mean']:+.1f} (Δ{a['d_opt']:+.1f}, ${a['usd_opt']:+,.0f}) | "
              f"pess 打 SL 占 {a['pess_sl_share']}%")
    print(f"产物: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())