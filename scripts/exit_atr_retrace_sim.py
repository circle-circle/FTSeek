#!/usr/bin/env python3
"""ATR相对回撤离线回放 (修尺子评估, 零行为变更, 不触碰生产)

问题: 高波股(≥3% ATR)盈利单捕获率 40.5% vs 低中波 78.2% —
固定 retrace 0.8% 对高波股是否系统性过紧?

基线声明 (skill exit-parameter-validation 第1步):
  现状 = breakeven 触发 0.5% / trailing 回撤 0.8% (冻结参数, 轮询级模拟验证最优)
  本回放所有变体均在此基线之上比较残存效应。

对照臂 (skill 第2步, 防归因污染):
  臂U = 全体统一放宽 retrace (零判别/无ATR分桶) — 更简替代
  臂A = retrace = clamp(k×ATR%, floor, ceil) — ATR 分桶
  臂A − 臂U 的差值 = "ATR 判别"本身的边际价值;
  若臂U 已追平/优于臂A, 则 ATR 分桶无增量价值, 方案否决。

模拟语义: 复用 scripts/trailing_polling_sim.sim_polling (轮询级,
30s采样, bar级peak, 冷却期15min SL放宽4%, 费垫按日期)。
持仓到收盘的仓用末根 close 结算。
"""
import json, os, sqlite3, sys

sys.path.insert(0, "/opt/ftseek/FTSeek-US")
os.chdir("/opt/ftseek/FTSeek-US")
from scripts.trailing_polling_sim import sim_polling, load_bars

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
_SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-07-15"   # 可选: 窗口起点
rows = con.execute(f"""
    SELECT symbol, direction, entry_price, exit_price, quantity, fees_est,
           mfe_pct, pnl_net, exit_reason, entry_time,
           CAST(json_extract(metadata_json,'$.atr') AS REAL)/entry_price*100 AS atr_pct
    FROM trade_records
    WHERE date(entry_time) >= '{_SINCE}' AND entry_price > 0 AND exit_price > 0
      AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason != 'ORPHAN_CLEANUP'
""").fetchall()
con.close()

trades = []
for r in rows:
    d = dict(r)
    bars = load_bars(d["symbol"], d["entry_time"][:10])
    if bars is None or not d["atr_pct"] or d["atr_pct"] <= 0:
        continue
    trades.append((d, bars))

print(f"有 bars+ATR: {len(trades)}/{len(rows)}")
actual_total = sum(d["pnl_net"] for d, _ in trades)
print(f"实际总净: {actual_total:+.2f}\n")


def settle(d, bars, be, retrace):
    """单臂结算: 返回 (sim_net, exit_kind)。

    诚实口径 (修正基线复现缺陷): 模拟器只建模 breakeven/trailing 退出,
    不建模 DEAD_SIGNAL/TIME_EXIT/PRE_CLOSE 等其他退出路径。因此:
      - 模拟触发 (ex 非 None): 用模拟净 PnL (新参数的反事实出场)
      - 模拟未触发 (持有到收盘): 回退实际 pnl_net — 真实系统中其他退出路径
        会接管该仓, 不能用末根 close 错误结算 (这是上一版基线 -901 vs 实际
        +360 失真的根因)。
    """
    fee = 0.4 if d["entry_time"][:10] >= "2026-08-06" else 0.15
    ex, ets, peak = sim_polling(d["direction"], d["entry_price"], bars,
                                retrace, be, fee, entry_hm=d["entry_time"][11:16])
    if ex is None:
        return float(d["pnl_net"]), "hold_actual"
    kind = "sim_exit"
    if d["direction"] == "LONG":
        sim_pct = (ex - d["entry_price"]) / d["entry_price"] * 100
    else:
        sim_pct = (d["entry_price"] - ex) / d["entry_price"] * 100
    sim_net = d["quantity"] * d["entry_price"] * sim_pct / 100.0 - float(d["fees_est"] or 0)
    return sim_net, kind


def run_arm(name, be_fn, rt_fn):
    tot = 0.0
    n_exit = 0
    bucket = {"low": 0.0, "high": 0.0}
    bucket_n = {"low": 0, "high": 0}
    for d, bars in trades:
        net, kind = settle(d, bars, be_fn(d["atr_pct"]), rt_fn(d["atr_pct"]))
        tot += net
        if kind == "sim_exit":
            n_exit += 1
        b = "high" if d["atr_pct"] >= 3.0 else "low"
        bucket[b] += net
        bucket_n[b] += 1
    print(f"{name:<42} 总净 {tot:+9.2f} (vs实际{tot-actual_total:+8.2f}) "
          f"模拟触发{n_exit:3d} | 低中波 {bucket['low']:+8.2f}(n={bucket_n['low']}) "
          f"高波 {bucket['high']:+8.2f}(n={bucket_n['high']})")
    return tot


clamp = lambda v, lo, hi: min(max(v, lo), hi)

print("=== 臂0: 基线 (现状冻结参数) ===")
base = run_arm("固定 be0.5 / retrace0.8 (现状)",
               lambda a: 0.5, lambda a: 0.8)

print("\n=== 臂U: 全体统一放宽 (无ATR判别, 更简替代) ===")
for rt in (1.0, 1.2, 1.5):
    run_arm(f"固定 be0.5 / retrace{rt}", lambda a: 0.5, lambda a: rt)

print("\n=== 臂A: ATR相对回撤 (分桶判别) — retrace=clamp(k×ATR%, 0.5, 1.5) ===")
for k in (0.20, 0.25, 0.30, 0.35, 0.40):
    run_arm(f"be0.5 / retrace=clamp({k}×ATR,0.5,1.5)",
            lambda a: 0.5,
            lambda a, k=k: clamp(k * a, 0.5, 1.5))

print("\n=== 臂B: 触发线+回撤双ATR相对 (全尺子换单位) ===")
for kb, kr in ((0.15, 0.25), (0.15, 0.30), (0.20, 0.30), (0.20, 0.35)):
    run_arm(f"be=clamp({kb}×ATR,0.5,1.2) / rt=clamp({kr}×ATR,0.5,1.5)",
            lambda a, kb=kb: clamp(kb * a, 0.5, 1.2),
            lambda a, kr=kr: clamp(kr * a, 0.5, 1.5))

print("\n说明: 费垫按日期(8/6前0.15%/后0.4%), 冷却期15min SL放宽4%,")
print("      峰值=bar high, 触发=轮询点((o+c)/2与c), 与实盘语义对齐。")
print("      绝对金额不作承诺; 排序稳健性需第二口径复核后才可定稿。")
