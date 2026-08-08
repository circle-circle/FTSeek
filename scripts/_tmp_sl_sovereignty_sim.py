#!/usr/bin/env python3
"""方案1 轮询级对照模拟 (Qoder, DEB-20260808 §22.5-1 落地前置).

三臂对照 (同一样本, 同一轮询级语义 — 复用 trailing_polling_sim 内核):
  臂A 现状:      SL = max(0.3%, 0.20 × 指令SL%)        (β 收紧)
  臂B 提案:      SL = clamp(0.8 × ATR%, 1.2%, 3.5%)    (废β, 新桶公式; 锚=93%赢家MAE<1%)
  臂C 原桶对照:  SL = 指令SL% 原样                       (分离"废β"与"改桶"的各自贡献)

语义对齐实盘: 30s 轮询采样点触发 (非 bar 内触及); BE 0.5% 武装 + 费垫按日期;
trailing 0.8% 固定 (现状); 无冷却期 (禁触窗后 playbook 单不再 09:30-09:45 开仓,
冷却逻辑事实上死代码); 死信号/MFE_STALL 两臂皆同 (不建模, 等效抵消).
"""
import json, os, sqlite3

REPLAY_DIR = "/opt/ftseek/FTSeek-US/data/replay_bars"
DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def load_bars(symbol, date):
    p = f"{REPLAY_DIR}/{symbol.replace('.', '_')}_{date}.json"
    return json.load(open(p)) if os.path.exists(p) else None


def sim_with_sl(direction, entry_price, bars, init_sl_pct, be_pct, fee_pct,
                retrace_pct, entry_hm):
    """轮询级模拟 (含初始SL). 返回 (exit_price, exit_ts, exit_kind, peak_pct)."""
    peak = entry_price
    base_sl = (entry_price * (1 - init_sl_pct / 100) if direction == "LONG"
               else entry_price * (1 + init_sl_pct / 100))
    be_trail_sl = 0.0
    started = False
    for bar in bars:
        ts, o, h, l, c = bar
        if entry_hm and not started:
            if ts[11:16] >= entry_hm:
                started = True
            else:
                continue
        if direction == "LONG":
            peak = max(peak, h)
        else:
            peak = min(peak, l)
        mfe = ((peak - entry_price) / entry_price * 100 if direction == "LONG"
               else (entry_price - peak) / entry_price * 100)
        if mfe >= be_pct:
            _be = (entry_price * (1 + fee_pct / 100) if direction == "LONG"
                   else entry_price * (1 - fee_pct / 100))
            be_trail_sl = max(be_trail_sl, _be) if direction == "LONG" else \
                (min(be_trail_sl, _be) if be_trail_sl > 0 else _be)
            if retrace_pct > 0:
                _tr = (peak * (1 - retrace_pct / 100) if direction == "LONG"
                       else peak * (1 + retrace_pct / 100))
                be_trail_sl = (max(be_trail_sl, _tr) if direction == "LONG"
                               else min(be_trail_sl, _tr))
        # 有效 SL: 初始与BE/trailing取更紧 (LONG 取高者)
        eff = (max(base_sl, be_trail_sl) if direction == "LONG"
               else (min(base_sl, be_trail_sl) if be_trail_sl > 0 else base_sl))
        for px in [(o + c) / 2, c]:           # 30s 轮询采样点
            if direction == "LONG" and px <= eff:
                kind = "SL" if px <= base_sl and base_sl >= (be_trail_sl or 0) else "BE/TRAIL"
                return px, ts, kind, mfe
            if direction == "SHORT" and px >= eff:
                kind = "SL" if px >= base_sl and (be_trail_sl == 0 or base_sl <= be_trail_sl) else "BE/TRAIL"
                return px, ts, kind, mfe
    return None, None, None, ((peak - entry_price) / entry_price * 100 if direction == "LONG"
                              else (entry_price - peak) / entry_price * 100)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    trades = [dict(r) for r in con.execute("""
        SELECT symbol, direction, entry_price, exit_price, mfe_pct, pnl_net, exit_reason,
               entry_time, trade_date, quantity, fees_est
        FROM trade_records
        WHERE date(entry_time) >= '2026-07-15' AND entry_price > 0 AND exit_price > 0
          AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason != 'ORPHAN_CLEANUP'
    """)]
    sl_map, atr_map = {}, {}
    for r in con.execute("""SELECT date, symbol, entry_price, stop_loss, atr_pct
                            FROM playbook_instructions WHERE entry_price > 0"""):
        d = dict(r)
        if d["stop_loss"]:
            sl_map[(d["date"], d["symbol"])] = abs(d["entry_price"] - d["stop_loss"]) / d["entry_price"] * 100
        if d["atr_pct"]:
            atr_map[(d["date"], d["symbol"])] = float(d["atr_pct"])
    con.close()

    sample = []
    for t in trades:
        bars = load_bars(t["symbol"], t["entry_time"][:10])
        if bars:
            sample.append((t, bars))
    print(f"样本: {len(sample)}/{len(trades)} 笔有 bars")

    def sl_pct_of(t):
        return sl_map.get((t["trade_date"], t["symbol"]), 5.0)

    def atr_of(t):
        return atr_map.get((t["trade_date"], t["symbol"]))

    arms = {
        "A_现状_β0.20": lambda t: max(0.3, 0.20 * sl_pct_of(t)),
        "A2_剂量_β0.50": lambda t: max(0.3, 0.50 * sl_pct_of(t)),
        "B_提案_0.8ATR": lambda t: min(max(0.8 * atr_of(t), 1.2), 3.5) if atr_of(t) else 1.8,
        "C_原桶无β": lambda t: sl_pct_of(t),
    }

    for label, filt in (("全样本", lambda t: True),
                        ("≥10:00入场 (禁触窗后世界)", lambda t: t["entry_time"][11:16] >= "10:00")):
        subset = [(t, b) for t, b in sample if filt(t)]
        base_total = sum(float(t["pnl_net"]) for t, _ in subset)
        print(f"\n=== {label}: {len(subset)} 笔, 实际总净 {base_total:+.2f} ===")
        print(f"{'臂':>18} {'SL触发':>6} {'模拟净':>10} {'改善':>9} {'杀赢家':>12}")
        for name, sl_fn in arms.items():
            sim_total, sl_hits, killed_winners = 0.0, 0, 0
            for t, bars in subset:
                fee = 0.4 if t["entry_time"][:10] >= "2026-08-06" else 0.15
                ex, ets, kind, mfe = sim_with_sl(
                    t["direction"], t["entry_price"], bars, sl_fn(t),
                    0.5, fee, 0.8, t["entry_time"][11:16])
                if ex is None:
                    sim_total += float(t["pnl_net"])
                    continue
                if kind == "SL":
                    sl_hits += 1
                # 直接名义额记账 (不用比例缩放 — 缩放法在 |real_pct| 小时符号翻转失真):
                #   sim_net = 模拟毛利% × 名义额 − 实际费用 (同腿数)
                sim_gross_pct = ((ex - t["entry_price"]) / t["entry_price"] * 100
                                 if t["direction"] == "LONG"
                                 else (t["entry_price"] - ex) / t["entry_price"] * 100)
                fees = float(t.get("fees_est") or 3.98)
                sim_net = sim_gross_pct / 100 * t["entry_price"] * t["quantity"] - fees
                if float(t["pnl_net"]) > 0 and kind == "SL":
                    killed_winners += 1
                sim_total += sim_net
            print(f"{name:>18} {sl_hits:>6} {sim_total:+10.2f} {sim_total - base_total:+9.2f} "
                  f"{killed_winners:>6} 笔")


if __name__ == "__main__":
    main()
