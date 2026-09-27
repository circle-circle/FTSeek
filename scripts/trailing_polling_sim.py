#!/usr/bin/env python3
"""轮询级 trailing/breakeven 模拟器 (DEB-20260808 §19.5-1)

对齐实盘语义:
- peak 用 bar high/low (实盘 bar 版 peak_price_bar 并行测量)
- 触发用轮询采样点价格 (30s 粒度: bar 内 (o+c)/2 与 c), 非 bar low/high 触及
- 冷却期: 前 15min SL 放宽到 4% (规则0)
- 费垫按日期: 8/6 前 0.15%, 8/6 后 0.4%
- breakeven 武装 + trailing 回撤锁利 (与 _compute_dynamic_effective_sl 同语义)
"""
import json, os, sqlite3

REPLAY_DIR = "/opt/ftseek/FTSeek-US/data/replay_bars"

def load_bars(symbol, date):
    name = symbol.replace(".", "_")
    p = f"{REPLAY_DIR}/{name}_{date}.json"
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)

def sim_polling(direction, entry_price, bars, retrace_pct, be_pct, fee_pct,
                entry_hm=None, cooldown_sl_pct=4.0, cooldown_min=15):
    """轮询级模拟. 返回 (exit_price, exit_ts, peak_pct) — exit_price None=持有到收盘"""
    peak = entry_price
    sl = 0.0
    be_armed = False
    started = False
    minutes_held = 0
    for bar in bars:
        ts, o, h, l, c = bar
        hm = ts[11:16]
        if entry_hm and not started:
            if hm >= entry_hm:
                started = True
            else:
                continue
        # peak 更新 (bar 级, 对齐实盘 bar 版 peak)
        if direction == "LONG":
            if h > peak:
                peak = h
        else:
            if l < peak:
                peak = l
        mfe_pct = (peak - entry_price) / entry_price * 100 if direction == "LONG" \
            else (entry_price - peak) / entry_price * 100
        # breakeven 武装 + trailing SL 演进
        if mfe_pct >= be_pct:
            if not be_armed:
                be_armed = True
                sl = entry_price * (1 + fee_pct / 100) if direction == "LONG" \
                    else entry_price * (1 - fee_pct / 100)
            if retrace_pct > 0:
                if direction == "LONG":
                    sl = max(sl, peak * (1 - retrace_pct / 100))
                else:
                    sl = min(sl, peak * (1 + retrace_pct / 100))
        # 冷却期: 前 15min SL 放宽
        if minutes_held < cooldown_min:
            cd_sl = entry_price * (1 - cooldown_sl_pct / 100) if direction == "LONG" \
                else entry_price * (1 + cooldown_sl_pct / 100)
            eff_sl = min(sl, cd_sl) if direction == "LONG" else max(sl, cd_sl) if sl > 0 else cd_sl
        else:
            eff_sl = sl
        # 轮询采样点: 30s = (o+c)/2, 60s = c
        for px in [(o + c) / 2, c]:
            if eff_sl > 0:
                if direction == "LONG" and px <= eff_sl:
                    return px, ts, (peak - entry_price) / entry_price * 100
                if direction == "SHORT" and px >= eff_sl:
                    return px, ts, (entry_price - peak) / entry_price * 100
        minutes_held += 1
    return None, None, (peak - entry_price) / entry_price * 100 if direction == "LONG" \
        else (entry_price - peak) / entry_price * 100


if __name__ == "__main__":
    con = sqlite3.connect("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute("""
        SELECT symbol, direction, entry_price, exit_price, mfe_pct,
               COALESCE(pnl_net, realized_pnl, 0) AS pnl_net, exit_reason, entry_time
        FROM trade_records
        WHERE date(entry_time) >= '2026-07-15' AND entry_price > 0 AND exit_price > 0
          AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason != 'ORPHAN_CLEANUP'
    """).fetchall()
    con.close()

    trades = []
    for r in rows:
        d = dict(r)
        bars = load_bars(d["symbol"], d["entry_time"][:10])
        if bars is None:
            continue
        trades.append((d, bars))
    print(f"有 bars: {len(trades)}/{len(rows)}")

    cur_total = sum(d["pnl_net"] for d, _ in trades)
    print(f"实际总净: {cur_total:+.2f}\n")

    print("=== 轮询级网格: breakeven触发线 × trailing回撤 ===")
    print(f"{'触发线':>6} {'回撤':>6} {'触发N':>6} {'模拟总净':>10} {'改善':>10} {'大赢家影响':>12}")
    for be in [0.5, 0.8]:
        for retrace in [0.3, 0.5, 0.8]:
            sim_total = 0.0
            triggers = 0
            big_impact = 0.0
            for d, bars in trades:
                fee = 0.4 if d["entry_time"][:10] >= "2026-08-06" else 0.15
                ex, ets, peak = sim_polling(d["direction"], d["entry_price"], bars,
                                            retrace, be, fee, entry_hm=d["entry_time"][11:16])
                if ex is None:
                    sim_net = d["pnl_net"]
                else:
                    triggers += 1
                    if d["direction"] == "LONG":
                        sim_pct = (ex - d["entry_price"]) / d["entry_price"] * 100 - fee
                    else:
                        sim_pct = (d["entry_price"] - ex) / d["entry_price"] * 100 - fee
                    real_pct = (d["exit_price"] - d["entry_price"]) / d["entry_price"] * 100
                    sim_net = d["pnl_net"] * sim_pct / real_pct if abs(real_pct) > 0.01 else 0
                    if d["mfe_pct"] >= 2.0:
                        big_impact += sim_net - d["pnl_net"]
                sim_total += sim_net
            print(f"{be:6.1f} {retrace:6.1f} {triggers:6d} {sim_total:+10.2f} {sim_total-cur_total:+10.2f} {big_impact:+12.2f}")

    # NVDA 8/3 复验
    d0 = next((t for t in trades if t[0]["symbol"] == "US.NVDA" and t[0]["entry_time"][:10] == "2026-08-03"), None)
    if d0:
        d, bars = d0
        print(f"\nNVDA 8/3 复验 (实际 +3.95% PRE_CLOSE):")
        for be in [0.5, 0.8]:
            for retrace in [0.3, 0.8]:
                ex, ets, peak = sim_polling("LONG", d["entry_price"], bars, retrace, be, 0.15,
                                            entry_hm=d["entry_time"][11:16])
                r_ = f"退={ex:.2f} @ {ets} ({(ex-d['entry_price'])/d['entry_price']*100:.2f}%)" if ex else "持有到收盘"
                print(f"  触发线{be} 回撤{retrace}: {r_}")