#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方向反向检验 (2026-08-23, 只读零交易路径) — 判定"白噪声 vs 反向真信号"。

═══ 为什么做这个 ═══
三次假设已被否决 (仓位不对称 / 持有期延长 / 同族板块动量), 且第三次连胜率都没动
(所有切片恒 41-43%)。这指向: 方向本身可能与价格结构无关 = 白噪声。
本检验是决定性实验: 若把全部指令反向执行,
  · 结局同样亏 (对称) → 方向是白噪声, 价格维度上无 edge 可捞, 问题在"文本→方向"这步
  · 反向显著更好      → 存在可利用的系统性反向偏差 (如"LLM 在利好上系统性追高")
  · 反向显著更差      → 原方向含真实但被执行链吃掉的 edge

═══ 方法要点: 为什么不能简单取 -pnl_net ═══
天真做法 (-1 × pnl_net) 是错的:
  ① SL/TP 距离不对称 (计划 SL 中位 4.66% / TP 7.53%) 且退出依赖价格路径 —
     LONG 在 -4.66% 触 SL 离场, 同一路径上的反向 SHORT 却可能走到 +7.53% 触 TP,
     毛额并不精确反号。
  ② 费用两向都付, 不随方向反号。
故必须用**同一套对称退出政策**分别模拟正反两向, 只让方向这一个变量变化。

对称退出政策 (预注册, 与 holding_extension_backtest 同一已验证机制):
  · SL/TP 取指令原始【距离】, 反向时以入场价镜像 (LONG SL 在下→反向 SHORT SL 在上, 距离相同)
  · Day0 盘中 1min bar: SL 用 bar 内极值 (不利侧, 硬止损逐 tick 语义) / TP 用收盘采样 (保守)
  · Day1..N 日线: 先判跳空穿越(成交于开盘价) → 再 SL → 再 TP; 同日双触判 SL 先 (保守)
  · 未触发则 T+N 收盘平仓。N ∈ {0, 1}
  · 费用逐腿 fee_model.estimate_fee

零假设的算术预期 (供对照): 无 SL/TP 时 gross_long + gross_short ≡ 0, 故
  net_orig + net_rev = -2 × 总费用。偏离该值的部分 = 结构性方向偏差 (SL/TP路径依赖 + 真信号)。

═══ 诚实披露 (见数前声明) ═══
① 反向执行的可行性未建模: SHORT 需券可借 (borrow_pool 门), 原 LONG 单反向成 SHORT
   可能根本借不到券; 反之 SHORT 反向成 LONG 无此问题。故反向组是【理论上界】。
② SHORT 多日借券费未建模 → 高估反向组 (原 LONG 占多数, 反向后变 SHORT)。
③ 未建模组合约束/滑点差异 (SHORT 滑点通常更差)。
④ 只做观测, 零生产路径改动。

输出: data/direction_reversal_report.json + 控制台
回滚: 删本脚本 + report
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "data" / "ftseek_us.db"
BARS_1M = REPO / "data" / "replay_bars"
BARS_D = REPO / "data" / "replay_bars_daily"
OUT = REPO / "data" / "direction_reversal_report.json"
SINCE = "2026-07-01"
HORIZONS = [0, 1]

_DC, _IC = {}, {}


def _fee(qty, price, side, date):
    from app.execution.fee_model import estimate_fee
    try:
        return float(estimate_fee(qty, price, side, date)["total"])
    except Exception:
        return 1.99


def daily_bars(sym):
    if sym not in _DC:
        p = BARS_D / f"{sym.replace('.', '_')}.json"
        try:
            _DC[sym] = json.load(open(p, encoding="utf-8")) if p.exists() else []
        except Exception:
            _DC[sym] = []
    return _DC[sym]


def intraday_bars(sym, date):
    k = (sym, date)
    if k not in _IC:
        p = BARS_1M / f"{sym.replace('.', '_')}_{date}.json"
        try:
            _IC[k] = json.load(open(p, encoding="utf-8")) if p.exists() else []
        except Exception:
            _IC[k] = []
    return _IC[k]


def simulate(sym, td, entry_hm, ep, is_short, sl, tp, horizon):
    """对称退出政策模拟。返回 (exit_price, kind, exit_date) 或 None。"""
    ib = intraday_bars(sym, td)
    if not ib:
        return None
    for b in ib:
        if str(b[0])[11:19] <= entry_hm:
            continue
        o, hi, lo, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        if sl and ((not is_short and lo <= sl) or (is_short and hi >= sl)):
            return sl, "SL", td
        if tp:
            for s in ((o + c) / 2.0, c):
                if (not is_short and s >= tp) or (is_short and s <= tp):
                    return tp, "TP", td
    if horizon == 0:
        return float(ib[-1][4]), "EOD_N", td

    db = daily_bars(sym)
    idx = next((i for i, r in enumerate(db) if str(r[0])[:10] == td), None)
    if idx is None:
        return None
    for k in range(1, horizon + 1):
        if idx + k >= len(db):
            return None
        d, o, hi, lo, c = (str(db[idx + k][0])[:10], float(db[idx + k][1]),
                           float(db[idx + k][2]), float(db[idx + k][3]),
                           float(db[idx + k][4]))
        if sl and ((not is_short and o <= sl) or (is_short and o >= sl)):
            return o, "SL_GAP", d
        if sl and ((not is_short and lo <= sl) or (is_short and hi >= sl)):
            return sl, "SL", d
        if tp and ((not is_short and hi >= tp) or (is_short and lo <= tp)):
            return tp, "TP", d
        if k == horizon:
            return c, "EOD_N", d
    return None


def pnl(ep, xp, qty, is_short, td, xdate):
    sign = -1.0 if is_short else 1.0
    entry_side, exit_side = (("SELL", "BUY") if is_short else ("BUY", "SELL"))
    gross = sign * (xp - ep) * qty
    fee = _fee(qty, ep, entry_side, td) + _fee(qty, xp, exit_side, xdate)
    return gross, fee, gross - fee


def clustered(deltas, dates):
    by = defaultdict(list)
    for v, d in zip(deltas, dates):
        by[d].append(v)
    dm = [sum(v) / len(v) for v in by.values()]
    n = len(dm)
    if n < 2:
        return {"n_days": n}
    m = sum(dm) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in dm) / (n - 1))
    r = {"n_days": n, "day_clustered_mean": round(m, 2)}
    if sd > 0:
        r["t"] = round(m / (sd / math.sqrt(n)), 3)
        r["mde"] = round(2.8 * sd * math.sqrt(2 / n), 2)
        r["distinguishable"] = bool(abs(m) > r["mde"])
    else:
        r.update({"t": None, "mde": None, "distinguishable": False})
    return r


def main():
    cc = sqlite3.connect(str(DB), timeout=10)
    raw = cc.execute(
        """SELECT t.symbol, t.direction, t.trade_date, t.entry_time, t.entry_price,
                  t.quantity, t.metadata_json, t.pnl_net, i.decision_id
           FROM trade_records t
           LEFT JOIN playbook_instructions i
             ON i.id = json_extract(t.metadata_json, '$.playbook_instruction_id')
           WHERE t.source_type='PLAYBOOK_SIM' AND t.exit_time IS NOT NULL
             AND t.trade_date >= ? AND t.pnl_net IS NOT NULL
             AND t.entry_price > 0 AND t.quantity > 0""", (SINCE,)).fetchall()
    cc.close()

    trades = []
    for sym, d, td, et, ep, q, mj, net, did in raw:
        try:
            m = json.loads(mj or "{}")
        except Exception:
            m = {}
        sl, tp = m.get("stop_loss"), m.get("take_profit")
        if not sl or float(sl) <= 0 or not tp or float(tp) <= 0:
            continue
        ep = float(ep)
        is_short = str(d).upper() == "SHORT"
        ds = abs(ep - float(sl)) / ep      # SL 距离
        dt = abs(float(tp) - ep) / ep      # TP 距离
        src = ("DA" if str(did or "").startswith("DA-") else
               "UE" if str(did or "").startswith("universe_expansion") else
               "reentry" if str(did or "").startswith("intraday_reentry") else "other")
        trades.append({"sym": sym, "td": td, "hm": str(et)[11:19], "ep": ep,
                       "qty": float(q), "is_short": is_short, "ds": ds, "dt": dt,
                       "actual_net": float(net), "src": src})
    print(f"候选 {len(raw)} → 有 SL/TP {len(trades)} 笔")

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "pre_registered": "2026-08-23 见数前写定 (对称退出政策/披露见 docstring)",
              "hypothesis": "反向执行是否显著优于原方向 → 判白噪声 vs 反向真信号",
              "disclosures": [
                  "反向组是理论上界: 原LONG反向成SHORT可能借不到券(borrow_pool门未建模)",
                  "SHORT多日借券费未建模 → 高估反向组",
                  "未建模SHORT滑点劣化与组合约束",
                  "SL/TP用指令原始距离镜像, Day0极值判SL/收盘判TP, 双触判SL先 (三处反乐观)",
              ], "by_horizon": {}}

    for N in HORIZONS:
        o_g = o_f = o_n = r_g = r_f = r_n = 0.0
        deltas, dates = [], []
        o_wins = r_wins = cnt = 0
        by_src = defaultdict(lambda: {"n": 0, "orig": 0.0, "rev": 0.0})
        kinds_o, kinds_r = defaultdict(int), defaultdict(int)
        skipped = 0
        for t in trades:
            ep, short = t["ep"], t["is_short"]
            # 原方向 SL/TP
            sl_o = ep * (1 + t["ds"]) if short else ep * (1 - t["ds"])
            tp_o = ep * (1 - t["dt"]) if short else ep * (1 + t["dt"])
            # 反向: 方向翻转, 距离镜像
            sl_r = ep * (1 - t["ds"]) if short else ep * (1 + t["ds"])
            tp_r = ep * (1 + t["dt"]) if short else ep * (1 - t["dt"])
            a = simulate(t["sym"], t["td"], t["hm"], ep, short, sl_o, tp_o, N)
            b = simulate(t["sym"], t["td"], t["hm"], ep, not short, sl_r, tp_r, N)
            if a is None or b is None:
                skipped += 1
                continue
            cnt += 1
            g1, f1, n1 = pnl(ep, a[0], t["qty"], short, t["td"], a[2])
            g2, f2, n2 = pnl(ep, b[0], t["qty"], not short, t["td"], b[2])
            o_g += g1; o_f += f1; o_n += n1
            r_g += g2; r_f += f2; r_n += n2
            o_wins += 1 if n1 > 0 else 0
            r_wins += 1 if n2 > 0 else 0
            kinds_o[a[1]] += 1
            kinds_r[b[1]] += 1
            deltas.append(n2 - n1)
            dates.append(t["td"])
            s = by_src[t["src"]]
            s["n"] += 1; s["orig"] += n1; s["rev"] += n2
        st = clustered(deltas, dates)
        null_pred = -(o_n) - (o_f + r_f)   # 零假设: net_rev ≈ -net_orig - 2×fees
        row = {"n_evaluated": cnt, "n_skipped": skipped,
               "orig": {"gross": round(o_g, 1), "fees": round(o_f, 1), "net": round(o_n, 1),
                        "win_rate": round(o_wins / max(cnt, 1), 3), "exit_mix": dict(kinds_o)},
               "reversed": {"gross": round(r_g, 1), "fees": round(r_f, 1), "net": round(r_n, 1),
                            "win_rate": round(r_wins / max(cnt, 1), 3), "exit_mix": dict(kinds_r)},
               "gross_sum_orig_plus_rev": round(o_g + r_g, 1),
               "null_predicted_reversed_net": round(null_pred, 1),
               "reversed_minus_null": round(r_n - null_pred, 1),
               "paired_delta_rev_minus_orig": {"sum": round(sum(deltas), 1),
                                              "mean": round(sum(deltas) / max(len(deltas), 1), 2),
                                              **st},
               "by_source": {k: {"n": v["n"], "orig_net": round(v["orig"], 1),
                                 "rev_net": round(v["rev"], 1)} for k, v in by_src.items()}}
        report["by_horizon"][N] = row

        print(f"\n{'='*68}\n持有至 {'当日收盘' if N==0 else f'T+{N}收盘'}  (n={cnt}, 跳过 {skipped})\n{'='*68}")
        print(f"  原方向: 毛 ${o_g:+9.1f}  费 ${o_f:8.1f}  净 ${o_n:+9.1f}  胜率 {o_wins/max(cnt,1)*100:4.1f}%")
        print(f"  反 向 : 毛 ${r_g:+9.1f}  费 ${r_f:8.1f}  净 ${r_n:+9.1f}  胜率 {r_wins/max(cnt,1)*100:4.1f}%")
        print(f"  毛额之和 (无SL/TP时应≡0): ${o_g + r_g:+.1f}")
        print(f"  零假设预测反向净额 = -原净-2×费 = ${null_pred:+.1f}  实际 ${r_n:+.1f}  "
              f"偏离 ${r_n - null_pred:+.1f}")
        print(f"  配对 δ(反向-原向): 合计 ${sum(deltas):+.1f} 笔均 ${sum(deltas)/max(len(deltas),1):+.2f} "
              f"日聚t={st.get('t')} MDE={st.get('mde')} 可分辨={st.get('distinguishable')}")
        print(f"  按源: " + " | ".join(
            f"{k} n={v['n']} 原${v['orig']:+.0f}→反${v['rev']:+.0f}" for k, v in by_src.items()))

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
