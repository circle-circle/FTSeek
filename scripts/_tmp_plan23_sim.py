#!/usr/bin/env python3
"""方案2/3 轮询级对照模拟 (Qoder, DEB-20260808 §23 后续, 周一前验证).

基线 = 已部署配置 (β=0.50 + 1.5% 下限), 全部臂在此基线上评估边际 (基线纪律):
  BASE  现状: trailing 固定 0.8% / BE 触发 0.5% / 费垫按日期 (8/6 前 0.15, 后 0.4)
  P2    方案2 吊灯: trailing = clamp(0.5×ATR%, 0.5%, 2.5%)   (因股而异回撤)
  P3a   方案3 触发线: BE 0.5→0.8%                              (死亡谷回避)
  P3b   方案3 完整: BE 0.8% + 费垫 0.15% (平)                  (触发晚+垫子薄)
  P3c   仅费垫: BE 0.5% + 费垫 0.15% (平)                      (分离费垫贡献)

语义: 30s 轮询采样点触发; 直接名义额记账; 174 笔全量 + ≥10:00 子集双口径。
"""
import json, os, sqlite3

REPLAY_DIR = "/opt/ftseek/FTSeek-US/data/replay_bars"
DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def load_bars(symbol, date):
    p = f"{REPLAY_DIR}/{symbol.replace('.', '_')}_{date}.json"
    return json.load(open(p)) if os.path.exists(p) else None


def sim(direction, entry_price, bars, init_sl_pct, be_pct, cushion_pct,
        retrace_fn, entry_hm):
    """retrace_fn(peak_pct_mfe) -> 当前允许的回撤%; 其余同 _tmp_sl_sovereignty_sim."""
    peak = entry_price
    base_sl = (entry_price * (1 - init_sl_pct / 100) if direction == "LONG"
               else entry_price * (1 + init_sl_pct / 100))
    evo_sl = 0.0
    started = False
    for bar in bars:
        ts, o, h, l, c = bar
        if entry_hm and not started:
            if ts[11:16] >= entry_hm:
                started = True
            else:
                continue
        peak = max(peak, h) if direction == "LONG" else min(peak, l)
        mfe = ((peak - entry_price) / entry_price * 100 if direction == "LONG"
               else (entry_price - peak) / entry_price * 100)
        if mfe >= be_pct:
            _be = (entry_price * (1 + cushion_pct / 100) if direction == "LONG"
                   else entry_price * (1 - cushion_pct / 100))
            evo_sl = max(evo_sl, _be) if direction == "LONG" else \
                (min(evo_sl, _be) if evo_sl > 0 else _be)
            rt = retrace_fn(mfe)
            if rt > 0:
                _tr = (peak * (1 - rt / 100) if direction == "LONG"
                       else peak * (1 + rt / 100))
                evo_sl = (max(evo_sl, _tr) if direction == "LONG"
                          else min(evo_sl, _tr))
        eff = (max(base_sl, evo_sl) if direction == "LONG"
               else (min(base_sl, evo_sl) if evo_sl > 0 else base_sl))
        for px in [(o + c) / 2, c]:
            if direction == "LONG" and px <= eff:
                return px, mfe
            if direction == "SHORT" and px >= eff:
                return px, mfe
    return None, None


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    trades = [dict(r) for r in con.execute("""
        SELECT symbol, direction, entry_price, exit_price, mfe_pct, pnl_net,
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

    sample = [(t, load_bars(t["symbol"], t["entry_time"][:10]))
              for t in trades]
    sample = [(t, b) for t, b in sample if b]
    print(f"样本: {len(sample)} 笔")

    def deployed_sl(t):   # 已部署: β=0.50 + 1.5% 下限
        return max(1.5, 0.50 * sl_map.get((t["trade_date"], t["symbol"]), 5.0))

    def atr_of(t):
        return atr_map.get((t["trade_date"], t["symbol"]))

    def cushion_hist(t):  # 现状: 8/6 前 0.15 / 后 0.4
        return 0.4 if t["entry_time"][:10] >= "2026-08-06" else 0.15

    chandelier = lambda t: (lambda mfe: min(max(0.5 * atr_of(t), 0.5), 2.5) if atr_of(t) else 0.8)

    arms = {
        "BASE_现状":       dict(be=0.5, cush=cushion_hist,            rt=lambda t: (lambda m: 0.8)),
        "P2_吊灯0.5ATR":   dict(be=0.5, cush=cushion_hist,            rt=chandelier),
        "P3a_BE0.8":       dict(be=0.8, cush=cushion_hist,            rt=lambda t: (lambda m: 0.8)),
        "P3b_BE0.8+垫0.15": dict(be=0.8, cush=lambda t: 0.15,         rt=lambda t: (lambda m: 0.8)),
        "P3c_仅垫0.15":    dict(be=0.5, cush=lambda t: 0.15,          rt=lambda t: (lambda m: 0.8)),
    }

    for label, filt in (("全样本", lambda t: True),
                        ("≥10:00 (禁触窗后)", lambda t: t["entry_time"][11:16] >= "10:00")):
        subset = [(t, b) for t, b in sample if filt(t)]
        base_total = sum(float(t["pnl_net"]) for t, _ in subset)
        print(f"\n=== {label}: {len(subset)} 笔, 实际总净 {base_total:+.2f} ===")
        print(f"{'臂':>18} {'触发':>5} {'模拟净':>10} {'vs实际':>9} {'大赢家影响':>10} {'vs BASE':>9}")
        results = {}
        for name, p in arms.items():
            sim_total, trig, big_impact = 0.0, 0, 0.0
            for t, bars in subset:
                ex, mfe = sim(t["direction"], t["entry_price"], bars, deployed_sl(t),
                              p["be"], p["cush"](t), p["rt"](t), t["entry_time"][11:16])
                if ex is None:
                    sim_net = float(t["pnl_net"])
                else:
                    trig += 1
                    gpct = ((ex - t["entry_price"]) / t["entry_price"] * 100
                            if t["direction"] == "LONG"
                            else (t["entry_price"] - ex) / t["entry_price"] * 100)
                    fees = float(t.get("fees_est") or 3.98)
                    sim_net = gpct / 100 * t["entry_price"] * t["quantity"] - fees
                    if float(t["mfe_pct"] or 0) >= 2.0:
                        big_impact += sim_net - float(t["pnl_net"])
                sim_total += sim_net
            results[name] = sim_total
            print(f"{name:>18} {trig:>5} {sim_total:+10.2f} {sim_total - base_total:+9.2f} "
                  f"{big_impact:+10.2f} {sim_total - results.get('BASE_现状', sim_total):+9.2f}")


if __name__ == "__main__":
    main()
