#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""试探仓/加码 (probe & pyramid) 仓位不对称反事实回测 (2026-08-23, 只读零交易路径)。

═══ 问题 ═══
7/1 起 321 笔实测: 胜率 42.1% / 盈亏比 1.07 / 净 -$1426。收益分布是双峰的:
  · 赢单 (SL_TRAIL_LOCK n=71 +$2886): MFE 中位 1.56%, 持仓 0.8h — 进场很快就赢
  · 死单 (SL_INITIAL/MFE_STALL/TIME_EXIT/DEAD_SIGNAL n=90 -$4432): MFE 中位
    0.07~0.24% — 从来没往有利方向走过
即"要么快速работает, 要么根本不работает"。当前固定仓位 → 死单和赢单赌注一样大,
42% 胜率下数学上必亏。利弗莫尔的不对称来自仓位 (试探→证实后加码), 不来自胜率。

═══ 预注册口径 (见数前写定) ═══
人群: PLAYBOOK_SIM 已平仓, trade_date>=2026-07-01, pnl_net 非空, 且有 bar 覆盖。
机制:
  BASELINE 全仓 qty 于 entry_price 进, exit_price 出 (= 现状)
  VAR_A    "试探+补满" 仓位中性: probe=f×qty 现在进, 剩余 (1-f)×qty 待确认后进
  VAR_B    "满仓+加码" 仓位增量: base=qty 现在进, 确认后再加 m×qty
确认定义: 自 entry_time 后的 bar, 采样价达到 entry×(1±θ) 即确认, 加码成交于该采样价。
  采样约定 (对齐 settle_shadow_portfolio, 反乐观): 每根 bar 只取 (o+c)/2 与 c 两点,
  **不用 bar 内极值 high/low** — 否则用瞬时插针价成交 = 系统性高估。
  LONG 确认价 >= entry×(1+θ); SHORT 确认价 <= entry×(1-θ)。
费用: 逐腿 fee_model.estimate_fee (非往返近似) — 加码多一腿, 未确认单只出场 probe 腿。
统计: 同批交易两政策 = 天然配对。报逐笔配对 δ 均值 + 宪章第9律按日聚类 t + MDE。

═══ 诚实披露 (见数前声明) ═══
① 退出时点/价格假设不变: SL/TP 锚定指令原始 entry/SL 且全为 % 口径, 仓位不进退出规则
   → 出场时点与价格与实际一致。**但未建模**组合级约束 (max_position_pct / 日亏损帽 /
   水坝单轮限 3 笔), VAR_B 加码会放大敞口, 实盘可能撞这些闸。
② 未建模加码腿的滑点与部分成交: 采样价即成交价。实盘限价追单可能不成交 (漏单),
   影响方向是**高估** VAR 收益。
③ 确认阈值 θ 与 probe 比例 f 是扫描出来的 → 存在过拟合风险。故同时报全参数网格,
   不只报最优点; 并以 |δ| > MDE 为可分辨门槛。
④ 本回测只改仓位, 不改入场筛选/退出链 → 与 OKR-2026-09 KR2 退出参数冻结不冲突。

输出: data/probe_pyramid_backtest_report.json + 控制台
回滚: 删本脚本 + report (零生产路径)
"""
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "data" / "ftseek_us.db"
BARS = REPO / "data" / "replay_bars"
OUT = REPO / "data" / "probe_pyramid_backtest_report.json"
SINCE = "2026-07-01"

# 扫描网格 (预注册)
THETAS = [0.3, 0.5, 0.8, 1.0]       # 确认阈值 %
PROBE_FRACS = [1 / 3, 1 / 2]         # VAR_A probe 比例
PYRAMID_MULTS = [0.5, 1.0]           # VAR_B 加码倍数
# 确认时限 (分钟): None=全路径(原口径, 已实测无判别力), 15=E2 已验证窗口
WINDOWS = [None, 15, 30, 60]


def _fee(qty, price, side, date):
    from app.execution.fee_model import estimate_fee
    try:
        return float(estimate_fee(qty, price, side, date)["total"])
    except Exception:
        return 1.99


def load_trades():
    cc = sqlite3.connect(str(DB), timeout=10)
    rows = cc.execute(
        """SELECT symbol, direction, trade_date, entry_time, exit_time,
                  entry_price, exit_price, quantity, pnl_net, mfe_pct, exit_reason
           FROM trade_records
           WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
             AND trade_date >= ? AND pnl_net IS NOT NULL
             AND entry_price > 0 AND quantity > 0""", (SINCE,)).fetchall()
    cc.close()
    return rows


def load_bar_samples(symbol, start_date, end_date):
    """返回 [(dt, sample_price), ...] 按时间排序。每 bar 两个采样点 ((o+c)/2, c)。"""
    out = []
    d0 = datetime.strptime(start_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(end_date, "%Y-%m-%d").date()
    day = d0
    while day <= d1:
        p = BARS / f"{symbol.replace('.', '_')}_{day.isoformat()}.json"
        if p.exists():
            try:
                for b in json.load(open(p, encoding="utf-8")):
                    dt = datetime.strptime(str(b[0])[:19], "%Y-%m-%d %H:%M:%S")
                    o, c = float(b[1]), float(b[4])
                    out.append((dt, (o + c) / 2.0))
                    out.append((dt, c))
            except Exception:
                pass
        day += timedelta(days=1)
    out.sort(key=lambda x: x[0])
    return out


def find_confirm(samples, entry_dt, exit_dt, entry_price, is_short, theta_pct,
                 window_min=None):
    """首个达到确认阈值的采样价。返回 (price, dt) 或 (None, None)。

    window_min: 确认时限 (分钟)。仅在 entry 后 window_min 内达阈值才算确认 —
      对齐 E2 已验证判别器 (15min 窗口 MFE≥θ)。None = 无时限 (全路径口径)。
      加时限的理由 (无时限版实测): θ=0.5% 确认率 57% — 亏损单大多也先往有利方向
      漂过, 全路径"确认"几乎不含判别力; 时间约束才是 E2 有效的关键。
    """
    tgt = (entry_price * (1 - theta_pct / 100.0) if is_short
           else entry_price * (1 + theta_pct / 100.0))
    deadline = (entry_dt + timedelta(minutes=window_min)) if window_min else exit_dt
    limit = min(deadline, exit_dt)
    for dt, px in samples:
        if dt <= entry_dt or dt > limit:
            continue
        if (px <= tgt) if is_short else (px >= tgt):
            return px, dt
    return None, None


def simulate(tr, samples, theta, mode, f=None, m=None, window_min=None):
    """返回该笔在给定政策下的净额。mode: 'base' | 'A' | 'B'。"""
    (sym, direction, td, et, xt, ep, xp, qty, actual_net, mfe, er) = tr
    is_short = str(direction).upper() == "SHORT"
    ep, xp, qty = float(ep), float(xp), float(qty)
    entry_side, exit_side = ("SELL", "BUY") if is_short else ("BUY", "SELL")
    sign = -1.0 if is_short else 1.0
    xdate = str(xt)[:10]

    def gross(q, cost):
        return sign * (xp - cost) * q

    if mode == "base":
        g = gross(qty, ep)
        fee = _fee(qty, ep, entry_side, td) + _fee(qty, xp, exit_side, xdate)
        return g - fee, {"confirmed": None, "qty_final": qty}

    entry_dt = datetime.strptime(str(et)[:19], "%Y-%m-%dT%H:%M:%S")
    exit_dt = datetime.strptime(str(xt)[:19], "%Y-%m-%dT%H:%M:%S")
    p_add, _ = find_confirm(samples, entry_dt, exit_dt, ep, is_short, theta, window_min)
    confirmed = p_add is not None

    if mode == "A":
        q_p = max(1.0, round(qty * f))
        q_a = max(0.0, qty - q_p)
        if confirmed and q_a > 0:
            g = gross(q_p, ep) + gross(q_a, p_add)
            fee = (_fee(q_p, ep, entry_side, td) + _fee(q_a, p_add, entry_side, td)
                   + _fee(qty, xp, exit_side, xdate))
            qf = qty
        else:
            g = gross(q_p, ep)
            fee = _fee(q_p, ep, entry_side, td) + _fee(q_p, xp, exit_side, xdate)
            qf = q_p
        return g - fee, {"confirmed": confirmed, "qty_final": qf}

    # mode == "B"
    q_a = round(qty * m)
    if confirmed and q_a > 0:
        g = gross(qty, ep) + gross(q_a, p_add)
        fee = (_fee(qty, ep, entry_side, td) + _fee(q_a, p_add, entry_side, td)
               + _fee(qty + q_a, xp, exit_side, xdate))
        qf = qty + q_a
    else:
        g = gross(qty, ep)
        fee = _fee(qty, ep, entry_side, td) + _fee(qty, xp, exit_side, xdate)
        qf = qty
    return g - fee, {"confirmed": confirmed, "qty_final": qf}


def paired_stats(deltas, dates):
    """逐笔配对 δ + 按日聚类 t/MDE (宪章第9律)。"""
    n = len(deltas)
    if n == 0:
        return {}
    mean = sum(deltas) / n
    by_day = defaultdict(list)
    for d, day in zip(deltas, dates):
        by_day[day].append(d)
    day_means = [sum(v) / len(v) for v in by_day.values()]
    nd = len(day_means)
    res = {"n_trades": n, "delta_sum": round(sum(deltas), 1),
           "delta_mean_per_trade": round(mean, 2), "n_days": nd}
    if nd >= 2:
        m = sum(day_means) / nd
        sd = math.sqrt(sum((v - m) ** 2 for v in day_means) / (nd - 1))
        res["day_clustered_mean"] = round(m, 2)
        if sd > 0:
            res["day_clustered_t"] = round(m / (sd / math.sqrt(nd)), 3)
            res["mde"] = round(2.8 * sd * math.sqrt(2 / nd), 2)
            res["distinguishable"] = bool(abs(m) > res["mde"])
        else:
            res["day_clustered_t"] = None
            res["mde"] = None
            res["distinguishable"] = False
    return res


def main():
    trades = load_trades()
    print(f"候选 {len(trades)} 笔")

    # bar 缓存 (按 symbol+日期范围)
    usable, sample_cache = [], {}
    for tr in trades:
        sym, _, td, et, xt = tr[0], tr[1], tr[2], tr[3], tr[4]
        key = (sym, td, str(xt)[:10])
        if key not in sample_cache:
            sample_cache[key] = load_bar_samples(sym, td, str(xt)[:10])
        if sample_cache[key]:
            usable.append(tr)
    print(f"有 bar 覆盖可回放: {len(usable)} ({len(usable)/len(trades):.0%})")

    # ── 基线自校验: 重建净额 vs 实际 pnl_net ──
    base_net, actual_sum = 0.0, 0.0
    recon_err = []
    for tr in usable:
        key = (tr[0], tr[2], str(tr[4])[:10])
        nb, _ = simulate(tr, sample_cache[key], 0, "base")
        base_net += nb
        actual_sum += float(tr[8])
        recon_err.append(nb - float(tr[8]))
    mae = sum(abs(e) for e in recon_err) / max(len(recon_err), 1)
    print(f"\n基线自校验: 重建净额 ${base_net:+.1f} vs 实际 pnl_net ${actual_sum:+.1f} "
          f"(差 ${base_net-actual_sum:+.1f}, 逐笔MAE ${mae:.2f})")
    if abs(base_net - actual_sum) > max(200.0, abs(actual_sum) * 0.25):
        print("⚠️ 重建偏差过大 — 结论仅在【重建口径内部】可比, 不可与实盘绝对值混用")

    results, grid = {}, []
    for win in WINDOWS:
        for theta in THETAS:
            # 确认率 (只取决于 θ 与 window)
            n_conf = 0
            for tr in usable:
                key = (tr[0], tr[2], str(tr[4])[:10])
                _, meta = simulate(tr, sample_cache[key], theta, "A", f=0.5,
                                   window_min=win)
                n_conf += 1 if meta["confirmed"] else 0
            conf_rate = n_conf / len(usable)

            for f in PROBE_FRACS:
                deltas, dates = [], []
                v_net = 0.0
                for tr in usable:
                    key = (tr[0], tr[2], str(tr[4])[:10])
                    nb, _ = simulate(tr, sample_cache[key], theta, "base")
                    nv, _ = simulate(tr, sample_cache[key], theta, "A", f=f,
                                     window_min=win)
                    v_net += nv
                    deltas.append(nv - nb)
                    dates.append(tr[2])
                st = paired_stats(deltas, dates)
                grid.append({"variant": "A_probe_fill", "window_min": win,
                             "theta_pct": theta,
                             "probe_frac": round(f, 3), "confirm_rate": round(conf_rate, 3),
                             "variant_net": round(v_net, 1), "baseline_net": round(base_net, 1),
                             **st})
            for m in PYRAMID_MULTS:
                deltas, dates = [], []
                v_net = 0.0
                for tr in usable:
                    key = (tr[0], tr[2], str(tr[4])[:10])
                    nb, _ = simulate(tr, sample_cache[key], theta, "base")
                    nv, _ = simulate(tr, sample_cache[key], theta, "B", m=m,
                                     window_min=win)
                    v_net += nv
                    deltas.append(nv - nb)
                    dates.append(tr[2])
                st = paired_stats(deltas, dates)
                grid.append({"variant": "B_pyramid_add", "window_min": win,
                             "theta_pct": theta,
                             "pyramid_mult": m, "confirm_rate": round(conf_rate, 3),
                             "variant_net": round(v_net, 1), "baseline_net": round(base_net, 1),
                             **st})

    grid.sort(key=lambda r: -r["delta_sum"])
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pre_registered": "2026-08-23 见数前写定 (口径/网格/披露见脚本 docstring)",
        "population": f"PLAYBOOK_SIM 已平仓 >= {SINCE}",
        "n_candidates": len(trades), "n_replayable": len(usable),
        "baseline_reconstructed_net": round(base_net, 1),
        "baseline_actual_pnl_net_sum": round(actual_sum, 1),
        "reconstruction_mae_per_trade": round(mae, 2),
        "sampling_convention": "每bar两采样点((o+c)/2, c), 不用bar内极值 (反乐观)",
        "disclosures": [
            "退出时点/价格假设不变 (SL/TP锚原始entry且全%口径); 未建模组合级约束(max_position_pct/日亏帽/水坝3笔)",
            "未建模加码腿滑点与漏单 → 方向上高估VAR收益",
            "θ与f为扫描参数, 存在过拟合风险; 故全网格披露并以|δ|>MDE为可分辨门槛",
            "只改仓位, 不改入场筛选与退出链 → 与KR2退出参数冻结不冲突",
        ],
        "grid": grid,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'变体':<14} {'窗口':>5} {'θ%':>5} {'参数':>6} {'确认率':>7} {'变体净':>10} "
          f"{'δ合计':>9} {'δ/笔':>7} {'日聚t':>7} {'MDE':>7} {'可分辨':>6}")
    for r in grid:
        param = r.get("probe_frac") or r.get("pyramid_mult")
        win = r.get("window_min") or "全路径"
        print(f"{r['variant']:<14} {str(win):>5} {r['theta_pct']:>5.1f} {param:>6.2f} "
              f"{r['confirm_rate']*100:>6.0f}% {r['variant_net']:>+10.1f} "
              f"{r['delta_sum']:>+9.1f} {r['delta_mean_per_trade']:>+7.2f} "
              f"{str(r.get('day_clustered_t')):>7} {str(r.get('mde')):>7} "
              f"{'是' if r.get('distinguishable') else '否':>6}")
    print(f"\n基线净额 (重建口径): ${base_net:+.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
