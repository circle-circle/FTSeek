#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持有期延长反事实回测 (2026-08-23, 只读零交易路径)。

═══ 被检验的假设 ═══
"thesis 是日级, 执行是小时级" — 系统结构性地在错误的时间尺度上收割。

支持该假设的既有事实:
  · 指令计划 SL 距离中位 4.66% / TP 距离 7.53% (R:R 1.62) —— 这是 ±5~8% 的多日交易计划
  · 实际 MFE 中位 0.61%, 中位持仓 1.8h, TAKE_PROFIT 在 321 笔里只触发 1 次
    → 计划的 TP 从未有机会被走到, 交易全被 trail_lock/breakeven/time 类规则在 ~1% 尺度平掉
  · 持仓越长的退出桶反而赚钱: EOD_FORCE_CLOSE(17.4h) 笔均 +$45 / PRE_CLOSE_EXIT(5.9h) +$13
    vs TIME_EXIT(4.0h) -$33 / MFE_STALL(2.5h) -$44
  · probe/pyramid 回测 (64 配置全不可分辨) 失败根因 = 赢单 0.8h 就 trail-lock,
    快到没有两段建仓的时间 → 反过来印证"尺度"才是根

═══ 预注册口径 (见数前写定) ═══
人群: PLAYBOOK_SIM 已平仓, trade_date>=2026-07-01, pnl_net/SL/TP 非空, 有 bar 覆盖。
基线: 实际 pnl_net (已验证重建 MAE $0.00, 直接用实盘记账值, 不可争议)。
变体: 撤销全部提前离场规则 (DEAD_SIGNAL / TIME_EXIT / MFE_STALL / trail_lock /
      breakeven), **只保留指令原始 SL 硬止损 + TP**, 持有至 T+N 收盘。N ∈ {0,1,3,5}。
      N=0 = 当日收盘前平 (不隔夜, 与隔夜政策解耦 — 最干净的单变量)。

判定顺序与数据源 (反乐观约定):
  Day 0 盘中: data/replay_bars/{SYM}_{date}.json 1min bar, 仅取 entry_time 之后。
    · SL 用 bar 内极值 (low for LONG / high for SHORT) —— 券商硬止损单是逐 tick 触发,
      用收盘价采样会系统性【低估】亏损, 故此处必须用极值 (对自己不利的那一侧)
    · TP 用收盘价采样 ((o+c)/2 与 c) —— 限价单未必在插针时成交, 保守
  Day 1..N: data/replay_bars_daily/{SYM}.json 日线 OHLC (100% 标的覆盖)
    · 先判跳空穿越: 开盘价已越过 SL → 成交于开盘价 (非 SL 价), 这是 SL_INITIAL
      笔均 -$193 的真实机制
    · 再判日内 SL (daily low/high) → 再判 TP (daily high/low)
    · 同日 SL 与 TP 都被触及时一律判 SL 先成交 (保守, 日线无法解析盘中先后)
  Day N: 收盘平仓。
费用: 逐腿 fee_model.estimate_fee (入场腿 + 出场腿)。

═══ 诚实披露 (见数前声明) ═══
① SHORT 多日持仓的借券费未建模 (历史费率不可得, fee_model 已注明) → 方向上【高估】
   SHORT 变体收益。报告输出受影响笔数。
② 未建模组合级约束: 多日持仓占用仓位额度 → 实际会挤掉后续新开仓 (机会成本),
   本回测把每笔当独立事件。这是本回测最大的局限 —— N 越大越严重。
③ 未建模隔夜 gap 的极端尾部之外的风险 (停牌/财报跳空已含在日线里, 但盘前盘后无数据)。
④ 单变量声明: 只改退出链, 入场/仓位/筛选完全不动。
⑤ 与 OKR-2026-09 KR2「9月退出参数冻结」冲突 → 即使为正也只能是 10 月候选, 9 月纯回测。

输出: data/holding_extension_backtest_report.json + 控制台
回滚: 删本脚本 + report
"""
import json
import math
import os
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
OUT = REPO / "data" / "holding_extension_backtest_report.json"
SINCE = "2026-07-01"
HORIZONS = [0, 1, 3, 5]


def _fee(qty, price, side, date):
    from app.execution.fee_model import estimate_fee
    try:
        return float(estimate_fee(qty, price, side, date)["total"])
    except Exception:
        return 1.99


_DCACHE = {}


def daily_bars(symbol):
    if symbol not in _DCACHE:
        p = BARS_D / f"{symbol.replace('.', '_')}.json"
        try:
            _DCACHE[symbol] = json.load(open(p, encoding="utf-8")) if p.exists() else []
        except Exception:
            _DCACHE[symbol] = []
    return _DCACHE[symbol]


def intraday_bars(symbol, date):
    p = BARS_1M / f"{symbol.replace('.', '_')}_{date}.json"
    if not p.exists():
        return []
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return []


def simulate_hold(tr, horizon):
    """返回 (exit_price, exit_kind, exit_date) 或 None (数据不足)。"""
    sym, direction, td, et, ep, qty, sl, tp = tr
    is_short = str(direction).upper() == "SHORT"
    ep = float(ep)
    sl = float(sl) if sl else None
    tp = float(tp) if tp else None

    # ── Day 0 盘中 (1min): SL 用极值, TP 用收盘采样 ──
    ib = intraday_bars(sym, td)
    if not ib:
        return None
    entry_hm = str(et)[11:19]
    for b in ib:
        bt = str(b[0])[11:19]
        if bt <= entry_hm:
            continue
        o, hi, lo, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        if sl:
            if (not is_short and lo <= sl) or (is_short and hi >= sl):
                return sl, "SL", td
        if tp:
            for s in ((o + c) / 2.0, c):
                if (not is_short and s >= tp) or (is_short and s <= tp):
                    return tp, "TP", td
    if horizon == 0:
        last = ib[-1]
        return float(last[4]), "EOD_N", td

    # ── Day 1..N (日线) ──
    db = daily_bars(sym)
    if not db:
        return None
    idx = next((i for i, r in enumerate(db) if str(r[0])[:10] == td), None)
    if idx is None:
        return None
    for k in range(1, horizon + 1):
        if idx + k >= len(db):
            return None  # 未来日不足 (窗口未走完)
        d, o, hi, lo, c = (str(db[idx + k][0])[:10], float(db[idx + k][1]),
                           float(db[idx + k][2]), float(db[idx + k][3]),
                           float(db[idx + k][4]))
        # 跳空穿越: 开盘已越 SL → 成交于开盘价
        if sl:
            if (not is_short and o <= sl) or (is_short and o >= sl):
                return o, "SL_GAP", d
        # 同日 SL 与 TP 都触及 → 判 SL 先 (保守)
        if sl and ((not is_short and lo <= sl) or (is_short and hi >= sl)):
            return sl, "SL", d
        if tp and ((not is_short and hi >= tp) or (is_short and lo <= tp)):
            return tp, "TP", d
        if k == horizon:
            return c, "EOD_N", d
    return None


def paired_stats(deltas, dates):
    n = len(deltas)
    if n == 0:
        return {}
    by_day = defaultdict(list)
    for d, day in zip(deltas, dates):
        by_day[day].append(d)
    dm = [sum(v) / len(v) for v in by_day.values()]
    nd = len(dm)
    res = {"n_trades": n, "delta_sum": round(sum(deltas), 1),
           "delta_mean_per_trade": round(sum(deltas) / n, 2), "n_days": nd}
    if nd >= 2:
        m = sum(dm) / nd
        sd = math.sqrt(sum((v - m) ** 2 for v in dm) / (nd - 1))
        res["day_clustered_mean"] = round(m, 2)
        if sd > 0:
            res["day_clustered_t"] = round(m / (sd / math.sqrt(nd)), 3)
            res["mde"] = round(2.8 * sd * math.sqrt(2 / nd), 2)
            res["distinguishable"] = bool(abs(m) > res["mde"])
        else:
            res.update({"day_clustered_t": None, "mde": None, "distinguishable": False})
    return res


def main():
    cc = sqlite3.connect(str(DB), timeout=10)
    raw = cc.execute(
        """SELECT symbol, direction, trade_date, entry_time, entry_price, quantity,
                  metadata_json, pnl_net, exit_reason
           FROM trade_records
           WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
             AND trade_date >= ? AND pnl_net IS NOT NULL
             AND entry_price > 0 AND quantity > 0""", (SINCE,)).fetchall()
    cc.close()

    trades = []
    for sym, d, td, et, ep, q, mj, net, er in raw:
        try:
            m = json.loads(mj or "{}")
        except Exception:
            m = {}
        sl, tp = m.get("stop_loss"), m.get("take_profit")
        if not sl or float(sl) <= 0:
            continue
        trades.append({"key": (sym, d, td, et, ep, q, sl, tp), "sym": sym,
                       "dir": d, "date": td, "ep": float(ep), "qty": float(q),
                       "actual_net": float(net), "exit_reason": er,
                       "is_short": str(d).upper() == "SHORT"})
    print(f"候选 {len(raw)} 笔 → 有 SL/TP {len(trades)} 笔")

    grid, per_horizon_detail = [], {}
    for N in HORIZONS:
        deltas, dates, kinds = [], [], defaultdict(int)
        v_net = 0.0
        base_net = 0.0
        n_short_multiday = 0
        skipped = 0
        for t in trades:
            r = simulate_hold(t["key"], N)
            if r is None:
                skipped += 1
                continue
            xp, kind, xdate = r
            sign = -1.0 if t["is_short"] else 1.0
            entry_side, exit_side = (("SELL", "BUY") if t["is_short"] else ("BUY", "SELL"))
            gross = sign * (xp - t["ep"]) * t["qty"]
            fee = (_fee(t["qty"], t["ep"], entry_side, t["date"])
                   + _fee(t["qty"], xp, exit_side, xdate))
            nv = gross - fee
            v_net += nv
            base_net += t["actual_net"]
            deltas.append(nv - t["actual_net"])
            dates.append(t["date"])
            kinds[kind] += 1
            if t["is_short"] and xdate != t["date"]:
                n_short_multiday += 1
        st = paired_stats(deltas, dates)
        row = {"horizon_days": N, "n_evaluated": len(deltas), "n_skipped": skipped,
               "variant_net": round(v_net, 1), "baseline_net_same_subset": round(base_net, 1),
               "exit_kind_mix": dict(kinds),
               "short_multiday_borrow_fee_unmodeled": n_short_multiday, **st}
        grid.append(row)
        per_horizon_detail[N] = row

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pre_registered": "2026-08-23 见数前写定 (口径/判定顺序/披露见脚本 docstring)",
        "hypothesis": "thesis是日级/执行是小时级 — 系统在错误时间尺度收割",
        "baseline": "实际 pnl_net (重建已验证 MAE $0.00)",
        "variant": "撤销提前离场链, 只留指令原始SL+TP, 持有至T+N收盘",
        "context": {"planned_sl_dist_pct_median": 4.66, "planned_tp_dist_pct_median": 7.53,
                    "planned_rr": 1.62, "actual_mfe_pct_median": 0.61,
                    "actual_hold_hours_median": 1.80,
                    "take_profit_fired_in_321": 1},
        "disclosures": [
            "SHORT多日借券费未建模 → 高估SHORT变体收益 (受影响笔数见 short_multiday_borrow_fee_unmodeled)",
            "未建模组合级仓位额度占用/机会成本 — N越大越严重, 本回测最大局限",
            "SL用bar内极值(不利侧)判定, TP用收盘采样判定, 同日双触判SL先 — 三处均反乐观",
            "只改退出链, 入场/仓位/筛选不动 (单变量)",
            "与OKR-2026-09 KR2退出参数冻结冲突 → 即使为正也只是10月候选",
        ],
        "grid": grid,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'持有至':>8} {'n':>5} {'跳过':>5} {'变体净':>10} {'基线净':>10} "
          f"{'δ合计':>9} {'δ/笔':>8} {'日聚t':>7} {'MDE':>7} {'可分辨':>6}")
    for r in grid:
        lbl = "当日收盘" if r["horizon_days"] == 0 else f"T+{r['horizon_days']}收盘"
        print(f"{lbl:>8} {r['n_evaluated']:>5} {r['n_skipped']:>5} "
              f"{r['variant_net']:>+10.1f} {r['baseline_net_same_subset']:>+10.1f} "
              f"{r['delta_sum']:>+9.1f} {r['delta_mean_per_trade']:>+8.2f} "
              f"{str(r.get('day_clustered_t')):>7} {str(r.get('mde')):>7} "
              f"{'是' if r.get('distinguishable') else '否':>6}")
    print("\n出场构成:")
    for r in grid:
        lbl = "当日收盘" if r["horizon_days"] == 0 else f"T+{r['horizon_days']}"
        print(f"  {lbl:>8}: {r['exit_kind_mix']}  (SHORT多日未计借券费 {r['short_multiday_borrow_fee_unmodeled']} 笔)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
