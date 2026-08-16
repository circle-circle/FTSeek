#!/usr/bin/env python3
"""MFE 停滞早退检查点重放 — 2026-08-14 (用户批准).

问题: "进场后价格迟迟不朝有利方向动"的死胎单, 在检查点 T 分钟砍掉能挽回多少?
规则 (预注册): 检查点 T ∈ {15,30,45,60}min 时, 若 bar-close 口径 MFE < θ ∈ {0.3%,0.5%}
  → 按检查点 bar close 市价离场; 否则走实盘原结局 (未触发=实盘同价, d=0).

方法论对齐 exit-parameter-validation skill:
  * 臂0基线: 实盘 pnl_net 先复现 (Σd=0 恒等式)
  * 配对差 d_i = 变体净 − 实盘净 (费用同额配对消去)
  * 误杀组: 触发但实盘盈利 (pnl_net>0) 单独报告
  * 双窗口: 7月 vs 8月 排序一致性
  * close-only 口径 (峰值/触发按 close, 不用 bar high/low — 保守)
  * 识别精度≠干预收益: 报捕获率 (Σd_触发 / Σ实盘亏_触发), 不用检测率×总亏估算

数据源: Futu K_1M via gate_a_mfe_replay.fetch_bars (磁盘缓存 data/replay_bars/,
  autype=NONE, 限流 sleep 1.2s/次). 只读 DB. 不改任何交易链路.
窗口: entry_date >= 2026-07-01 (entry_time 时区格式可信段).
产出: data/mfe_stall_replay_2026-08-14/summary.json + per_trade.csv
用法: venv/bin/python scripts/mfe_stall_checkpoint_replay.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from app.utils.timezone import ET  # noqa: E402
from gate_a_mfe_replay import collect_bars  # noqa: E402  (bar获取三件套 SSOT)

DB_PATH = REPO / "data" / "ftseek_us.db"
OUT_DIR = REPO / "data" / "mfe_stall_replay_2026-08-14"
MIN_ENTRY_DATE = "2026-07-01"

CHECKPOINTS_MIN = [15, 30, 45, 60]
THRESHOLDS_PCT = [0.3, 0.5]
B_PERM = 10_000
RNG_SEED = 20260814


def _parse_et(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    if dt.tzinfo is None:
        return None  # naive (时区格式不可信段) → 剔除并计数
    return dt.astimezone(ET)


def load_trades(con: sqlite3.Connection) -> tuple[list[dict], int]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, symbol, direction, entry_time, exit_time, entry_price,
               exit_price, quantity, realized_pnl, pnl_net, fees_est, mfe_pct
        FROM trade_records
        WHERE exit_time IS NOT NULL AND substr(entry_time,1,10) >= ?
        ORDER BY id
        """, (MIN_ENTRY_DATE,)).fetchall()
    out, n_naive = [], 0
    for r in rows:
        entry_et = _parse_et(r["entry_time"])
        exit_et = _parse_et(r["exit_time"])
        if entry_et is None or exit_et is None:
            n_naive += 1
            continue
        sign = 1.0 if r["direction"].upper() == "LONG" else -1.0
        out.append({
            "trade_id": r["id"], "symbol": r["symbol"],
            "direction": r["direction"].upper(), "sign": sign,
            "entry_et": entry_et, "exit_et": exit_et,
            "entry": float(r["entry_price"]), "exit": float(r["exit_price"]),
            "qty": float(r["quantity"]),
            "pnl_net": float(r["pnl_net"] or 0.0),
            "fees_est": float(r["fees_est"] or 0.0),
            "mfe_ledger": float(r["mfe_pct"]) if r["mfe_pct"] is not None else None,
            "date": entry_et.strftime("%Y-%m-%d"),
        })
    return out, n_naive


def replay_variant(tr: dict, bars: list[dict], T: int, theta_pct: float) -> dict:
    """在 entry→entry+T 窗口的 bar close 上检查 MFE; 未达标则按检查点 close 离场."""
    entry_et, entry, qty, sign = tr["entry_et"], tr["entry"], tr["qty"], tr["sign"]
    lo = entry_et.replace(second=0, microsecond=0)
    ck = lo + timedelta(minutes=T)

    # 交易在检查点前已实际离场 → 规则无机会, 保持实盘 (d=0)
    if tr["exit_et"] <= ck:
        return {"triggered": False, "reason": "exited_before_checkpoint",
                "cf_net": tr["pnl_net"]}

    win = [b for b in bars if lo < b["t"] <= ck]
    if not win:
        return {"triggered": None, "reason": "no_bars_in_window", "cf_net": None}

    def fav(px: float) -> float:  # 有利幅度 % (方向感知)
        return (px - entry) / entry * 100.0 * sign

    mfe = max(fav(b["c"]) for b in win)
    if mfe >= theta_pct:
        return {"triggered": False, "reason": "mfe_ok", "mfe_ckpt": mfe,
                "cf_net": tr["pnl_net"]}

    # 触发: 按检查点窗口最后一根 bar close 离场
    cf_price = win[-1]["c"]
    cf_gross = (cf_price - entry) * qty * sign
    cf_net = cf_gross - tr["fees_est"]
    return {"triggered": True, "reason": "mfe_stall", "mfe_ckpt": mfe,
            "cf_price": cf_price, "cf_time": win[-1]["t"].isoformat(),
            "cf_net": cf_net, "n_bars": len(win)}


def paired_test(d: np.ndarray) -> dict:
    n = len(d)
    rng = np.random.default_rng(RNG_SEED)
    s_obs = float(d.sum())
    signs = rng.choice(np.array([-1.0, 1.0]), size=(B_PERM, n))
    s_perm = signs @ d
    p_two = float((np.abs(s_perm) >= abs(s_obs)).sum() + 1) / (B_PERM + 1)
    idx = rng.integers(0, n, size=(B_PERM, n))
    boot = d[idx]
    ci = np.percentile(boot.mean(axis=1), [2.5, 97.5])
    return {"n": n, "sum_d": round(s_obs, 2), "mean_d": round(float(d.mean()), 3),
            "ci95": [round(float(ci[0]), 3), round(float(ci[1]), 3)],
            "p_two": round(p_two, 5),
            "pos_zero_neg": [int((d > 0).sum()), int((d == 0).sum()), int((d < 0).sum())]}


def main() -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    trades, n_naive = load_trades(con)
    con.close()
    print(f"队列: n={len(trades)} ({MIN_ENTRY_DATE}起), 剔除 naive 时区 {n_naive} 笔")

    bars_map, missing = collect_bars(trades, refresh=False)

    results, excluded = [], []
    for tr in trades:
        day = tr["entry_et"].strftime("%Y-%m-%d")
        bars = bars_map.get((tr["symbol"], day))
        if not bars:
            excluded.append(f"{tr['symbol']} {day}")
            continue
        row = {"trade_id": tr["trade_id"], "symbol": tr["symbol"],
               "direction": tr["direction"], "date": tr["date"],
               "live_net": tr["pnl_net"]}
        ok = True
        for T in CHECKPOINTS_MIN:
            for th in THRESHOLDS_PCT:
                r = replay_variant(tr, bars, T, th)
                key = f"T{T}_th{th}"
                if r["cf_net"] is None:
                    ok = False
                    break
                row[key + "_trig"] = r["triggered"]
                row[key + "_net"] = r["cf_net"]
                row[key + "_mfe"] = r.get("mfe_ckpt")
            if not ok:
                break
        if ok:
            results.append(row)
        else:
            excluded.append(f"{tr['symbol']} {day} (窗口缺bar)")

    print(f"重放 n={len(results)}, 剔出 {len(excluded)}: {excluded[:8] or '无'}")
    if not results:
        return

    live_sum = sum(r["live_net"] for r in results)
    print(f"\n臂0基线: Σ实盘净 = {live_sum:+.2f} (n={len(results)})")

    print("\n=== 网格 (配对差 d = 变体净 − 实盘净) ===")
    print(f"{'变体':14s} {'触发':>5s} {'误杀':>4s} {'Σd':>9s} {'均笔d':>8s} "
          f"{'CI95':>17s} {'p':>8s} {'捕获率':>7s}")
    summary = {"arm0_live_net": round(live_sum, 2), "n": len(results),
               "excluded": excluded, "variants": {}}
    for T in CHECKPOINTS_MIN:
        for th in THRESHOLDS_PCT:
            key = f"T{T}_th{th}"
            d = np.array([r[key + "_net"] - r["live_net"] for r in results])
            trig = [r for r in results if r[key + "_trig"] is True]
            false_kill = [r for r in trig if r["live_net"] > 0]
            # 捕获率: Σd_触发 / |Σ实盘亏_触发| (skill: 识别精度≠干预收益)
            trig_live_loss = sum(min(r["live_net"], 0) for r in trig)
            trig_d = sum(r[key + "_net"] - r["live_net"] for r in trig)
            capture = (trig_d / abs(trig_live_loss) * 100) if trig_live_loss < 0 else float("nan")
            t = paired_test(d)
            # 双窗口: 7月 vs 8月
            w1 = d[[i for i, r in enumerate(results) if r["date"] < "2026-08-01"]]
            w2 = d[[i for i, r in enumerate(results) if r["date"] >= "2026-08-01"]]
            w1s = f"{w1.sum():+.0f}(n{len(w1)})" if len(w1) else "n/a"
            w2s = f"{w2.sum():+.0f}(n{len(w2)})" if len(w2) else "n/a"
            print(f"{key:14s} {len(trig):>5d} {len(false_kill):>4d} {t['sum_d']:>+9.2f} "
                  f"{t['mean_d']:>+8.3f} [{t['ci95'][0]:>+7.2f},{t['ci95'][1]:>+7.2f}] "
                  f"{t['p_two']:>8.4f} {capture:>6.0f}%")
            print(f"{'':14s} 双窗口Σd: 7月{w1s} / 8月{w2s} | "
                  f"误杀Σ实盘盈 {sum(r['live_net'] for r in false_kill):+.0f}")
            summary["variants"][key] = {
                "n_triggered": len(trig), "n_false_kill": len(false_kill),
                "false_kill_live_profit": round(sum(r["live_net"] for r in false_kill), 2),
                "sum_d": t["sum_d"], "mean_d": t["mean_d"], "ci95": t["ci95"],
                "p_two": t["p_two"], "capture_pct": round(capture, 1) if capture == capture else None,
                "window_jul": w1s, "window_aug": w2s,
                "triggered_trades": [
                    {"id": r["trade_id"], "sym": r["symbol"], "date": r["date"],
                     "live": round(r["live_net"], 2), "cf": round(r[key + "_net"], 2),
                     "mfe_ckpt": r.get(key + "_mfe")} for r in trig][:40]}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    cols = ["trade_id", "symbol", "direction", "date", "live_net"] + \
           [f"T{T}_th{th}{s}" for T in CHECKPOINTS_MIN for th in THRESHOLDS_PCT
            for s in ("_trig", "_net")]
    with open(OUT_DIR / "per_trade.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\n产出: {OUT_DIR / 'summary.json'}\n      {OUT_DIR / 'per_trade.csv'}")


if __name__ == "__main__":
    main()
