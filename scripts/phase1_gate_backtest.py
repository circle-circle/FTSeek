#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 1 新门历史回测 (2026-08-23, 用户令"通过历史数据回测效果和质量")
=======================================================================
回测什么: 三个新门若 2026-07-01 起就在生产, 对【实际已成交】的样本会拦掉多少、
拦掉的那批质量如何 (真实结局, 非反事实)。

  门                          可回测性        方法
  P1-3 量能门 VWAP-relative   ✅ 完全可回放    成交分钟重算线性 vs p50 ratio
  P1-1 VWAP 斜率门            ✅ 完全可回放    成交分钟重算 5min 量加权价斜率
  P1-2 tripwire 核销          ❌ 不可回放      需指令未成交期的盘中实时价序列, 无历史源
  Phase0 UE 冷却              ❌ 不可回放      影响注入侧 (未生成指令), 无"本会生成"数据

⚠️ 本质局限 (预披露): 只能算"拦已成交单"的方向——被拦单若真更烂则门有价值;
但"额外拦掉多少本不会成交的坏单"无法计算 (无证伪门时代的触发遥测对照)。
因此本回测是门的【下限证据】: 若连已成交单里拦的都偏烂 → 门有价值;
若拦的偏赢 → 门有害。不能证明门的全部价值。

⚠️ 非对称披露: 结局用真实已实现净额 (费用已扣), 被拦组与放行组口径相同
(都是真实成交), 不存在反事实不对称。

统计: 按日聚类 + MDE (宪章第 9 律); n<30 只报方向。
输出: data/phase1_gate_backtest_report.json + 控制台。
"""
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "ftseek_us.db"
BARS_DIR = BASE / "data" / "replay_bars_1m"
OUT = BASE / "data" / "phase1_gate_backtest_report.json"
SINCE = "2026-07-01"
VOL_THRESHOLD = 0.8     # chase_vol_ratio_threshold (config 实值)
GRACE_MIN = 15          # intraday_vol_gate_grace_min
SLOPE_TOL = 0.001       # falsification_gate_vwap_slope_tol
OPEN_MIN = 9 * 60 + 30


def load_bars(symbol):
    """symbol -> {date: [(minute_of_day, o, h, l, c, vol), ...]} 按分钟排序。"""
    fname = symbol.replace(".", "_") + ".json"
    p = BARS_DIR / fname
    if not p.exists():
        return None
    try:
        raw = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    days = defaultdict(list)
    for b in raw:
        try:
            t = str(b[0])  # 'YYYY-MM-DD HH:MM:SS'
            day, hm = t[:10], t[11:16]
            minute = int(hm[:2]) * 60 + int(hm[3:])
            days[day].append((minute, float(b[1]), float(b[2]),
                              float(b[3]), float(b[4]), float(b[5])))
        except (IndexError, TypeError, ValueError):
            continue
    return {d: sorted(v) for d, v in days.items()}


def p50_cum_curve(hist_days, days=20):
    """近 days 个历史日的逐分钟累积量 p50 曲线。返回 [cum_p50...] (index=开盘后第i+1分钟)。"""
    take = sorted(hist_days)[-days:]
    if len(take) < 10:
        return None
    cums = []
    for d in take:
        acc, run = [], 0.0
        for _, _, _, _, _, v in hist_days[d]:
            run += v
            acc.append(run)
        cums.append(acc)
    curve, maxlen = [], max(len(c) for c in cums)
    for i in range(maxlen):
        vals = sorted(c[i] for c in cums if i < len(c))
        if not vals:
            break
        n = len(vals)
        curve.append(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0)
    return curve or None


def bar_at(bars_day, minute):
    """该日 <= minute 的最后一根 bar (bar 覆盖该分钟)。"""
    hit = None
    for b in bars_day:
        if b[0] <= minute:
            hit = b
        else:
            break
    return hit


def vwap_slope(bars_day, minute, window=5):
    """近 window 根 1min bar 量加权均价相对前 window 根的变化率。"""
    idx = [i for i, b in enumerate(bars_day) if b[0] <= minute]
    if len(idx) < window * 2:
        return None
    def vwap(seg):
        num = den = 0.0
        for _, o, h, l, c, v in seg:
            if v <= 0:
                continue
            num += ((h + l + c) / 3.0) * v  # bars 无 turnover, 用典型价 (与生产回落路径同源)
            den += v
        return num / den if den > 0 else None
    recent = vwap([bars_day[i] for i in idx[-window:]])
    prior = vwap([bars_day[i] for i in idx[-window * 2:-window]])
    if not recent or not prior or prior <= 0:
        return None
    return (recent - prior) / prior


def main():
    cc = sqlite3.connect(str(DB), timeout=5)
    rows = cc.execute(
        """SELECT t.symbol, t.direction, t.entry_time, t.trade_date, t.entry_price,
                  t.quantity, COALESCE(t.pnl_net, t.realized_pnl) AS net,
                  t.exit_reason, i.entry_price AS anchor
           FROM trade_records t
           LEFT JOIN playbook_instructions i
             ON i.id = json_extract(t.metadata_json, '$.playbook_instruction_id')
           WHERE t.source_type='PLAYBOOK_SIM' AND t.exit_time IS NOT NULL
             AND t.trade_date >= ?""", (SINCE,)).fetchall()
    cc.close()

    cache, trades = {}, []
    for sym, direction, entry_time, trade_date, ep, qty, net, exit_reason, anchor in rows:
        if sym not in cache:
            cache[sym] = load_bars(sym)
        bars = cache[sym]
        if not bars or trade_date not in bars:
            continue  # 无覆盖 → 如实排除, 计入披露
        try:
            hm = entry_time[11:16]  # entry_time 带 -04:00 后缀, 即 ET
            minute = int(hm[:2]) * 60 + int(hm[3:])
        except Exception:
            continue
        trades.append({"symbol": sym, "direction": direction, "date": trade_date,
                       "minute": minute, "ep": float(ep), "qty": float(qty),
                       "net": float(net), "exit_reason": exit_reason,
                       "anchor": float(anchor) if anchor else None,
                       "bars_day": bars[trade_date],
                       "bars": bars})

    n_total = len(rows)
    n_cov = len(trades)
    print(f"样本: {n_total} 笔成交 (7/1 起) | 有 1min bar 覆盖可回放: {n_cov} "
          f"({n_cov / n_total:.0%}) | 排除 {n_total - n_cov} 笔 (无覆盖)")

    vol_blocked, vol_passed = [], []
    slope_blocked, slope_passed = [], []
    vol_na = slope_na = 0

    for t in trades:
        bars_day, minute = t["bars_day"], t["minute"]
        is_short = t["direction"] == "SHORT"
        elapsed = minute - OPEN_MIN

        # ── P1-3 量能门: 仅 chase 路径 + 非豁免窗 ──
        chase = False
        if t["anchor"]:
            chase = (t["ep"] > t["anchor"]) if not is_short else (t["ep"] < t["anchor"])
        if chase and elapsed >= GRACE_MIN:
            hist_days = {d: v for d, v in t["bars"].items() if d < t["date"]}
            curve = p50_cum_curve(hist_days)
            hit = bar_at(bars_day, minute)
            if curve and hit:
                idx = min(max(elapsed - 1, 0), len(curve) - 1)
                today_cum = sum(b[5] for b in bars_day if b[0] <= minute)
                if curve[idx] > 0:
                    ratio_p50 = today_cum / curve[idx]
                    # 线性口径对照
                    day_vols = [sum(b[5] for b in v) for d, v in sorted(hist_days.items())][-5:]
                    ratio_lin = (today_cum / (sum(day_vols) / len(day_vols)
                               * max(elapsed / 390, 0.02))) if day_vols else None
                    t["vol_ratio_p50"] = round(ratio_p50, 3)
                    t["vol_ratio_lin"] = round(ratio_lin, 3) if ratio_lin else None
                    (vol_blocked if ratio_p50 < VOL_THRESHOLD else vol_passed).append(t)
                else:
                    vol_na += 1
            else:
                vol_na += 1

        # ── P1-1 VWAP 斜率门: 全部成交均适用 ──
        slope = vwap_slope(bars_day, minute)
        if slope is not None:
            t["slope"] = round(slope, 5)
            against = (slope > SLOPE_TOL) if is_short else (slope < -SLOPE_TOL)
            (slope_blocked if against else slope_passed).append(t)
        else:
            slope_na += 1

    def summarize(name, blocked, passed, na):
        nb, np_ = len(blocked), len(passed)
        def daymean_by_day(recs):
            m = defaultdict(list)
            for r in recs:
                m[r["date"]].append(r["net"])
            return {d: sum(v) / len(v) for d, v in m.items()}
        b_days, p_days = daymean_by_day(blocked), daymean_by_day(passed)
        paired = sorted(set(b_days) & set(p_days))
        diffs = [b_days[d] - p_days[d] for d in paired]
        bm = daymean_by_day(blocked)
        pooled_b = sum(r["net"] for r in blocked) / nb if nb else None
        pooled_p = sum(r["net"] for r in passed) / np_ if np_ else None
        wr_b = sum(1 for r in blocked if r["net"] > 0) / nb if nb else None
        wr_p = sum(1 for r in passed if r["net"] > 0) / np_ if np_ else None

        def tstat(vals):
            n = len(vals)
            if n < 2:
                return None
            m = sum(vals) / n
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
            return m / (sd / math.sqrt(n)) if sd > 0 else None
        ct = tstat(diffs)
        mde = None
        if len(diffs) >= 2:
            m = sum(diffs) / len(diffs)
            sd = math.sqrt(sum((v - m) ** 2 for v in diffs) / (len(diffs) - 1))
            mde = round(2.8 * sd * math.sqrt(2 / len(diffs)), 2)
        if nb < 30:
            verdict = f"n={nb}<30, 仅方向性"
        elif ct is None or mde is None:
            verdict = "日聚类自由度不足"
        elif ct <= -2.0 and abs(sum(diffs) / len(diffs)) > mde:
            verdict = "被拦组显著更烂 → 门有价值"
        elif ct >= 2.0 and abs(sum(diffs) / len(diffs)) > mde:
            verdict = "⚠️ 被拦组显著更好 → 门在拦赢家"
        else:
            verdict = "不可分辨 (效应不显著或未超 MDE)"
        return {"n_blocked": nb, "n_passed": np_, "n_na": na,
                "blocked_sum_net": round(sum(r["net"] for r in blocked), 1) if nb else 0,
                "blocked_pooled_mean": round(pooled_b, 2) if pooled_b is not None else None,
                "passed_pooled_mean": round(pooled_p, 2) if pooled_p is not None else None,
                "blocked_wr": round(wr_b, 3) if wr_b is not None else None,
                "passed_wr": round(wr_p, 3) if wr_p is not None else None,
                "daily_clustered_t": round(ct, 3) if ct is not None else None,
                "mde": mde, "n_paired_days": len(paired), "verdict": verdict}

    report = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "window": f">={SINCE}", "n_total_trades": n_total, "n_replayable": n_cov,
        "coverage_pct": round(n_cov / n_total, 3),
        "limitation": "只测'拦已成交单'方向; '额外拦未成交坏单'无历史对照不可测 (下限证据)",
        "P1_3_volume_gate": summarize("vol", vol_blocked, vol_passed, vol_na),
        "P1_1_vwap_slope_gate": summarize("slope", slope_blocked, slope_passed, slope_na),
    }
    # 交易量影响: 被拦笔数 / 周
    weeks = max(len({t["date"] for t in trades}) / 5.0, 1)
    report["volume_impact"] = {
        "P1_3_blocked_per_week": round(len(vol_blocked) / weeks, 1),
        "P1_1_blocked_per_week": round(len(slope_blocked) / weeks, 1),
        "baseline_trades_per_week": round(n_cov / weeks, 1),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
