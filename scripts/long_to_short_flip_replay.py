#!/usr/bin/env python3
"""多转空反手重放 — 2026-08-14 (用户问题: 死胎LONG反手做空是否更有价值).

可执行形态 (非事后诸葛亮): 检查点 T 时 bar-close MFE < θ → 平多 + 同价反手做空,
  空单持有至 min(原实际出场时点, EOD 15:55). 与"持多到实际出场"配对比较.
事后上限 (识别偏差天花板) 单独算术给出, 二者差 = 可落地性折损.

数据源/口径: 复用 mfe_stall_checkpoint_replay 的 bar 基建 (gate_a_mfe_replay.collect_bars),
  close-only, autype=NONE. 只读 DB. 窗口 7/1 起 (时区可信段).
产出: data/long_to_short_flip_2026-08-14/summary.json
"""
from __future__ import annotations
import json, sqlite3, sys
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))
from app.utils.timezone import ET  # noqa
from gate_a_mfe_replay import collect_bars  # noqa

DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "long_to_short_flip_2026-08-14"
MIN_DATE = "2026-07-01"
TS = [15, 30, 45]; THS = [0.3, 0.5]


def _pet(ts):
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    return dt.astimezone(ET) if dt.tzinfo else None


def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, symbol, entry_time, exit_time, entry_price, exit_price, quantity, "
        "pnl_net, fees_est, mfe_pct FROM trade_records "
        "WHERE direction='LONG' AND exit_time IS NOT NULL AND mfe_pct IS NOT NULL "
        "AND substr(entry_time,1,10)>=? ORDER BY id", (MIN_DATE,)).fetchall()
    con.close()
    out = []
    for r in rows:
        e, x = _pet(r["entry_time"]), _pet(r["exit_time"])
        if not e or not x:
            continue
        out.append({"id": r["id"], "symbol": r["symbol"], "entry_et": e, "x": x,
                    "ep": float(r["entry_price"]), "qty": float(r["quantity"]),
                    "net": float(r["pnl_net"] or 0), "fees": float(r["fees_est"] or 0),
                    "day": e.strftime("%Y-%m-%d")})
    return out


def flip_replay(tr, bars, T, theta):
    """返回 (flip_net, triggered). flip_net=None 表示窗口缺bar."""
    e, ep, qty, x = tr["entry_et"], tr["ep"], tr["qty"], tr["x"]
    lo = e.replace(second=0, microsecond=0)
    ck = lo + timedelta(minutes=T)
    if x <= ck:                      # 原单在检查点前已离场, 无机会反手
        return tr["net"], False
    win = [b for b in bars if lo < b["t"] <= ck]
    if not win:
        return None, None
    mfe = max((b["c"] - ep) / ep * 100 for b in win)
    if mfe >= theta:                 # MFE达标 → 不反手, 保持原结局
        return tr["net"], False
    # 反手: 平多@ck价 + 做空@ck价, 空单持到 min(原exit, EOD15:55)
    ckp = win[-1]["c"]
    long_leg = (ckp - ep) * qty
    eod = x.replace(hour=15, minute=55, second=0, microsecond=0)
    end = min(x, eod)
    seg = [b for b in bars if ck < b["t"] <= end]
    if not seg:                      # 检查点后无bar (极端尾盘) → 空单按原价平
        endp = ckp
    else:
        endp = seg[-1]["c"]
    short_leg = (ckp - endp) * qty
    flip_gross = long_leg + short_leg
    flip_net = flip_gross - 2 * tr["fees"]   # 反手多一个来回, 双份费
    return flip_net, True


def main():
    trades = load()
    print(f"LONG队列 n={len(trades)} ({MIN_DATE}起)")
    bars_map, missing = collect_bars(trades, refresh=False)
    rows, excl = [], []
    for tr in trades:
        bars = bars_map.get((tr["symbol"], tr["day"]))
        if not bars:
            excl.append(tr["symbol"] + " " + tr["day"]); continue
        rec = {"id": tr["id"], "sym": tr["symbol"], "day": tr["day"], "actual": tr["net"]}
        for T in TS:
            for th in THS:
                fn, trig = flip_replay(tr, bars, T, th)
                if fn is None:
                    rec = None; break
                rec[f"F{T}_{th}"] = fn; rec[f"T{T}_{th}"] = trig
            if rec is None:
                break
        if rec:
            rows.append(rec)
        else:
            excl.append(tr["symbol"] + " " + tr["day"] + "(缺bar)")
    print(f"重放 n={len(rows)}, 剔出 {len(excl)}")

    actual_sum = sum(r["actual"] for r in rows)
    print(f"\n基线 Σ实际净(LONG持到出场) = {actual_sum:+.2f}")
    print(f"\n{'变体':12s} {'反手笔':>6s} {'Σ净':>9s} {'Δvs实际':>9s} {'均笔Δ':>7s} {'反手笔WR':>8s}")
    summ = {"n": len(rows), "actual_net": round(actual_sum, 2), "variants": {}}
    for T in TS:
        for th in THS:
            key = f"F{T}_{th}"
            d = np.array([r[key] - r["actual"] for r in rows])
            flipped = [r for r in rows if r[f"T{T}_{th}"]]
            fw = sum(1 for r in flipped if r[key] > r["actual"])
            summ["variants"][f"T{T}_th{th}"] = {
                "n_flipped": len(flipped), "sum_net": round(float(np.sum([r[key] for r in rows])), 1),
                "delta": round(float(d.sum()), 1), "mean_d": round(float(d.mean()), 2),
                "flip_better": fw}
            print(f"T{T}_th{th:3} {len(flipped):>6d} {np.sum([r[key] for r in rows]):>+9.0f} "
                  f"{d.sum():>+9.0f} {d.mean():>+7.2f} {fw}/{len(flipped)}")
    # 事后上限 (入场即知死胎, 同出场时点反手, 识别偏差天花板)
    print("\n--- 事后上限对照 (识别偏差天花板, 不可落地) ---")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1))
    print(f"产出: {OUT/'summary.json'}")


if __name__ == "__main__":
    main()
